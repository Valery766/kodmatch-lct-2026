"""Optional model selection of trusted matching facts, never a model score.

The model may select/order fact identifiers. All displayed sentences are
rendered by the server from deterministic evidence. Free text, profile data
and model-invented qualifications cannot enter an explanation.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import math
import threading
import time

import httpx
from pydantic import BaseModel, ConfigDict, Field
from .parsing import ModelBusy, request_json_content

VERSION = "grounded-explanation-1.0.0"
CACHE_LIMIT = 256
CACHE_SECONDS = 600
_cache = OrderedDict()
_cache_lock = threading.Lock()
SPEC_LABELS = {"backend_python": "Backend/Python", "frontend_ts": "Frontend/TypeScript", "data_sql": "Data/SQL"}
GRADE_LABELS = {"junior": "Junior", "middle": "Middle", "senior": "Senior"}


class ExplanationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_fact_ids: list[str] = Field(min_length=1, max_length=5)


def _finite_number(value, minimum=0, maximum=100):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("invalid trusted numeric evidence")
    return float(value)


def facts_from_rank(need, ranked):
    """Only fixed labels/numeric facts; no arbitrary text or skill/FSP IDs."""
    matching = ranked["matching"]
    if matching.get("eligible") is not True:
        raise ValueError("ineligible candidate")
    spec, actual, requested = ranked["specialization"], ranked["verified_grade"], need["grade"]
    if spec not in SPEC_LABELS or actual not in GRADE_LABELS or requested not in GRADE_LABELS:
        raise ValueError("unsupported category")
    score = _finite_number(ranked["score"])
    coverage = _finite_number(matching["coverage"], maximum=1) * 100
    unknown = matching.get("unknown_requirements", [])
    if not isinstance(unknown, list) or len(unknown) > 100:
        raise ValueError("invalid coverage evidence")
    facts = [
        {"id": "f_category", "text": f"Тест подтвердил категорию {SPEC_LABELS[spec]} / {GRADE_LABELS[actual]}; потребность – {GRADE_LABELS[requested]}."},
        {"id": "f_coverage", "text": f"Проверенное покрытие обязательных навыков: {coverage:g}% по измеренным компетенциям."},
        {"id": "f_score", "text": f"Детерминированный индекс соответствия: {score:g}/100."},
        {"id": "f_score_meaning", "text": "Индекс правил не является вероятностью найма; решение принимает работодатель."},
        {"id": "f_salary", "text": "Указанные зарплатные диапазоны пересекаются." if matching.get("salary_compatibility_known") is True else "Зарплатные ожидания кандидата не подтверждены полностью; совместимость требует уточнения."},
    ]
    mandatory = {"f_category", "f_coverage", "f_score_meaning", "f_salary"}
    if unknown:
        facts.append({"id": "f_gaps", "text": f"Для {len(unknown)} обязательных навыков нет проверенного покрытия. Это неизвестность, а не доказанное отсутствие навыка; требуется дополнительная проверка."})
        mandatory.add("f_gaps")
    test_score = ranked.get("test_score")
    if test_score is not None:
        facts.append({"id": "f_test_result", "text": f"Результат профильного теста: {_finite_number(test_score):g}/100."})
    else:
        facts.append({"id": "f_test_result", "text": "Баллы профильного теста не предоставлены; качество по баллам неизвестно."})
    bonus = _finite_number(matching.get("components", {}).get("fsp", 0), maximum=5)
    if bonus:
        achievements = ranked.get("fsp_achievements") or []
        synthetic = any(a.get("source") == "demo_registry" or a.get("synthetic") is True for a in achievements if isinstance(a, dict))
        facts.append({"id": "f_fsp", "text": f"Ограниченный бонус ФСП: +{bonus:g} к индексу; источник – синтетический демонстрационный реестр, внешняя проверка ФСП не выполнена." if synthetic else f"Ограниченный бонус подтвержденного источника ФСП: +{bonus:g} к индексу; он не заменяет обязательные навыки."})
        mandatory.add("f_fsp")
    elif not ranked.get("fsp_achievements"):
        facts.append({"id": "f_fsp", "text": "Истории ФСП нет; отсутствие истории не является штрафом."})
    return facts, mandatory


def fingerprint(need, ranked):
    facts, mandatory = facts_from_rank(need, ranked)
    value = {"facts": facts, "mandatory": sorted(mandatory), "formula_version": ranked["matching"]["version"], "version": VERSION}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cache_key(scope, need, ranked, settings):
    # Internal identifiers never enter the model request or public explanation.
    provider = hashlib.sha256((settings.llm_base_url + "\0" + settings.llm_model).encode()).hexdigest()
    return (*scope, fingerprint(need, ranked), provider)


def cached_result(key):
    now = time.monotonic()
    with _cache_lock:
        expired = [k for k, (expiry, _) in _cache.items() if expiry <= now]
        for expired_key in expired:
            del _cache[expired_key]
        value = _cache.get(key)
        if value is None:
            return None
        _cache.move_to_end(key)
        result = deepcopy(value[1])
        result["cached"] = True
        return result


def save_result(key, result):
    # Transient upstream failures have a short cache; successful/model-missing
    # responses remain bounded for ten minutes, invalidated by evidence changes.
    ttl = CACHE_SECONDS if result["source"] == "llm_grounded" or not result["warnings"] or "не подключена" in result["warnings"][0] else 30
    with _cache_lock:
        _cache[key] = (time.monotonic() + ttl, deepcopy(result))
        _cache.move_to_end(key)
        while len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)


def explain(need, ranked, settings):
    facts, mandatory = facts_from_rank(need, ranked)
    allowed = {fact["id"] for fact in facts}
    selected = [fact["id"] for fact in facts]
    source, provenance = "deterministic_fallback", "trusted_matching_rules"
    warnings = []
    if not settings.llm_base_url or not settings.llm_model:
        warnings.append("LLM не подключена: показаны пояснения доверенных правил подбора.")
    else:
        prompt = {"role": "system", "content": "Select and order 1 to 5 relevant IDs from the supplied trusted matching facts. Return only JSON {\"selected_fact_ids\":[\"f_category\",...]}. Never generate prose, scores, personal information or new facts. IDs absent from facts are forbidden. Mandatory limitations are enforced by the server. The model does not grade or rank candidates."}
        outgoing = json.dumps({"facts": facts, "mandatory_fact_ids": sorted(mandatory)}, ensure_ascii=False)
        try:
            selection = FactSelection.model_validate_json(request_json_content(settings, prompt, outgoing)).selected_fact_ids
            if len(set(selection)) != len(selection) or any(not isinstance(identifier, str) or identifier not in allowed for identifier in selection):
                raise ValueError("unsupported selected fact")
            selected = selection + [fact["id"] for fact in facts if fact["id"] in mandatory and fact["id"] not in selection]
            source, provenance = "llm_grounded", "llm_fact_selection"
        except ModelBusy:
            warnings.append("Лимит параллельных AI-запросов: показаны пояснения доверенных правил подбора.")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            warnings.append("LLM недоступна или предложила неподтвержденные факты: показаны пояснения доверенных правил подбора.")
    by_id = {fact["id"]: fact["text"] for fact in facts}
    return {"source": source, "provenance": provenance, "explanation": [by_id[identifier] for identifier in selected],
            "selected_fact_ids": selected, "facts": facts, "warnings": warnings, "formula_version": ranked["matching"]["version"],
            "score": ranked["score"], "changes_matching": False, "cached": False}
