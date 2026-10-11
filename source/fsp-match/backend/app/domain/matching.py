"""Evidence-based shortlist. Scores are rule indices, not hiring probabilities."""

from __future__ import annotations

import copy
import math
import re

VERSION = "matching-1.0.0"
WEIGHTS = {"required_coverage": 60, "preferred_coverage": 10, "test_quality": 25, "fsp": 5}
GRADE_ORDER = {"junior": 1, "middle": 2, "senior": 3}
ALIASES = {"py": "python", "ts": "typescript", "js": "javascript", "postgres": "postgresql", "postgresql": "postgresql",
           "rest": "api", "rest api": "api", "react.js": "react", "reactjs": "react", "tests": "testing"}
SAFE_PROFILE_FIELDS = frozenset({"id", "display_name", "headline", "summary", "specialization", "declared_grade", "verified_grade",
                                "skills", "verified_skills", "salary_min", "salary_max", "work_format", "location", "visible",
                                "test_score", "tested_at", "category", "years_experience"})


def _normal(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value).strip().lower())
    return ALIASES.get(text, text)


def _skills(value: object) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, (list, tuple, set)) or len(value) > 100 or any(not isinstance(x, str) or len(x) > 80 for x in value):
        raise ValueError("Навыки должны быть ограниченным списком строк")
    return {_normal(x) for x in value if x.strip()}


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _salary_overlap(need: dict, profile: dict) -> tuple[bool, bool]:
    nlow, nhigh = _number(need.get("salary_min")), _number(need.get("salary_max"))
    plow, phigh = _number(profile.get("salary_min")), _number(profile.get("salary_max"))
    if any(x is None for x in (nlow, nhigh, plow, phigh)):
        return True, False
    if nlow > nhigh or plow > phigh:
        return False, True
    return max(nlow, plow) <= min(nhigh, phigh), True


def _achievement(raw: object, specialization: str) -> dict | None:
    if not isinstance(raw, dict) or raw.get("verified") is not True or raw.get("source") not in {"demo_registry", "fsp_registry"}:
        return None
    if raw.get("specialization") not in (None, "", specialization):
        return None
    # Whitelist prevents a mock registry payload from carrying private contacts.
    return {k: copy.deepcopy(raw[k]) for k in ("id", "title", "event", "place", "year", "verified", "source", "specialization", "synthetic") if k in raw}


def rank_candidates(need: dict, profiles: list[dict]) -> list[dict]:
    if not isinstance(need, dict) or not isinstance(profiles, list) or len(profiles) > 10000:
        raise ValueError("Некорректная потребность или слишком большой банк")
    specialization = need.get("specialization")
    requested_grade = _normal(need.get("grade", "junior"))
    if specialization not in {"backend_python", "frontend_ts", "data_sql"} or requested_grade not in GRADE_ORDER:
        raise ValueError("Укажите поддерживаемую специализацию и грейд")
    required = _skills(need.get("required_skills", []))
    preferred = _skills(need.get("preferred_skills", [])) - required
    strict_verified = need.get("require_verified_skills") is True
    results = []
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("Некорректный профиль")
        # Visibility and confirmed category are eligibility, never compensable by
        # a higher self-described Senior label, keyword stuffing or FSP points.
        verified_grade = _normal(profile.get("verified_grade", ""))
        if profile.get("visible") is not True or profile.get("specialization") != specialization:
            continue
        if verified_grade not in GRADE_ORDER or GRADE_ORDER[verified_grade] < GRADE_ORDER[requested_grade]:
            continue
        try:
            verified = _skills(profile.get("verified_skills"))
        except ValueError:
            continue  # Bad imported evidence cannot silently become verified.
        skill_evidence = profile.get("skill_evidence") or {}
        if not isinstance(skill_evidence, dict):
            continue
        failed = {_normal(skill) for skill, evidence in skill_evidence.items()
                  if evidence == "failed" or isinstance(evidence, dict) and evidence.get("status") == "failed"}
        if required & failed:
            continue
        unknown = required - verified
        if strict_verified and unknown:
            continue
        salary_compatible, salary_known = _salary_overlap(need, profile)
        if not salary_compatible:
            continue
        work_required, work_profile = _normal(need.get("work_format", "")), _normal(profile.get("work_format", ""))
        if work_required not in {"", "any", "любой"} and work_profile not in {"", "any", "любой", work_required}:
            continue
        location_required, location_profile = _normal(need.get("location", "")), _normal(profile.get("location", ""))
        if work_required != "remote" and location_required and location_profile and location_required != location_profile:
            continue
        coverage = len(required & verified) / len(required) if required else 1.0
        preferred_coverage = len(preferred & verified) / len(preferred) if preferred else 1.0
        test_score = _number(profile.get("test_score"))
        quality = min(test_score, 100) / 100 if test_score is not None else 0.0
        raw_achievements = profile.get("fsp_achievements") or []
        if not isinstance(raw_achievements, list):
            raw_achievements = []
        achievements = [safe for raw in raw_achievements[:100] if (safe := _achievement(raw, specialization)) is not None]
        # No achievement bonus may fill a required evidence gap. Nonmembers with
        # stronger relevant tests can rank first; absent history has no penalty.
        bonus = WEIGHTS["fsp"] if achievements and not unknown else 0
        components = {"required_coverage": round(WEIGHTS["required_coverage"] * coverage, 2),
                      "preferred_coverage": round(WEIGHTS["preferred_coverage"] * preferred_coverage, 2),
                      "test_quality": round(WEIGHTS["test_quality"] * quality, 2), "fsp": bonus}
        explanation = [f"Подтверждена категория {specialization}/{verified_grade}; требуется {requested_grade}."]
        if required:
            explanation.append(f"Подтверждено обязательных навыков: {len(required & verified)} из {len(required)}.")
        if required & verified:
            explanation.append("Измерены навыки: " + ", ".join(sorted(required & verified)) + ".")
        if unknown:
            explanation.append("Нет проверенного покрытия: " + ", ".join(sorted(unknown)) + "; требуется дополнительная проверка.")
        explanation.append(f"Результат теста: {test_score:g}/100." if test_score is not None else "Баллы теста не предоставлены; качество по баллам неизвестно.")
        explanation.append("Зарплатные диапазоны пересекаются." if salary_known else "Совместимость зарплаты требует уточнения.")
        if work_required and not work_profile:
            explanation.append("Формат работы кандидата не указан.")
        if location_required and not location_profile and work_required != "remote":
            explanation.append("Местоположение кандидата не указано.")
        if bonus:
            demo = any(a["source"] == "demo_registry" for a in achievements)
            explanation.append("Бонус подтвержденной истории ФСП +5; источник – демонстрационный реестр." if demo else "Бонус подтвержденной истории ФСП +5.")
        elif not achievements:
            explanation.append("Истории ФСП нет; это не штраф и не препятствие подбору.")
        result = {k: copy.deepcopy(value) for k, value in profile.items() if k in SAFE_PROFILE_FIELDS}
        result["fsp_achievements"] = achievements
        result["score"] = round(sum(components.values()), 2)
        result["explanation"] = explanation
        result["matching"] = {"version": VERSION, "eligible": True, "coverage": round(coverage, 4),
                              "exact_required_match": not unknown, "unknown_requirements": sorted(unknown),
                              "salary_compatibility_known": salary_known, "components": components,
                              "score_meaning": "Индекс правил, не вероятность успешного найма"}
        results.append(result)
    return sorted(results, key=lambda profile: (-profile["score"], str(profile.get("id", ""))))
