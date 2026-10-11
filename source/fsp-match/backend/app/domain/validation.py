"""Reproducible synthetic benchmark; labels are loaded, never derived by scorer.

This is authored fixture evidence, not independent human validation. CLI:
python -m app.domain.validation fixture.json report.json
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
from pathlib import Path

from .matching import VERSION, rank_candidates


def _metrics(needs: list[dict], ranked: dict[str, list[dict]], labels: dict[str, dict], k: int) -> dict:
    results = []
    for need in needs:
        query_labels = labels[need["id"]]
        ranking = ranked[need["id"]][:k]
        grades = [query_labels.get(str(profile["id"]), 0) for profile in ranking]
        ideal = sorted(query_labels.values(), reverse=True)[:k]
        dcg = sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(grades))
        idcg = sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(ideal))
        results.append({"need_id": need["id"], "returned": len(ranking),
                        "precision_at_k": sum(grade > 0 for grade in grades) / k,
                        "precision_of_returned": sum(grade > 0 for grade in grades) / len(ranking) if ranking else None,
                        "ndcg_at_k": dcg / idcg if idcg else None,
                        "critical_mismatches_in_top_k": sum(grade == 0 for grade in grades),
                        "profile_ids": [profile["id"] for profile in ranking]})
    nonempty = [row["ndcg_at_k"] for row in results if row["ndcg_at_k"] is not None]
    return {"k": k, "queries": results,
            "macro_precision_at_k": round(sum(row["precision_at_k"] for row in results) / len(results), 4),
            "macro_precision_of_returned": round(sum(row["precision_of_returned"] for row in results if row["precision_of_returned"] is not None) / max(1, sum(row["precision_of_returned"] is not None for row in results)), 4),
            "macro_ndcg_at_k": round(sum(nonempty) / len(nonempty), 4) if nonempty else None,
            "critical_mismatches_in_top_k": sum(row["critical_mismatches_in_top_k"] for row in results),
            "queries_without_positive_gold": sum(row["ndcg_at_k"] is None for row in results),
            "definition": "Precision denominator fixed k; missing results count as zero. nDCG excludes no-positive-gold queries."}


def _baseline(need: dict, profiles: list[dict]) -> list[dict]:
    required = set(need.get("required_skills", []))
    preferred = set(need.get("preferred_skills", []))
    candidates = []
    for profile in profiles:
        if profile.get("visible") is not True or profile.get("specialization") != need["specialization"]:
            continue
        self_described = set(profile.get("skills", []))
        score = 60 * len(required & self_described) / max(1, len(required)) + 10 * len(preferred & self_described) / max(1, len(preferred))
        score += {"junior": 0, "middle": 5, "senior": 10}.get(profile.get("declared_grade"), 0)
        candidates.append({**profile, "score": score})
    return sorted(candidates, key=lambda profile: (-profile["score"], str(profile["id"])))


def evaluate_fixture(fixture: dict) -> dict:
    needs, profiles, labels = fixture["needs"], fixture["profiles"], fixture["labels"]
    ids = {str(profile["id"]) for profile in profiles}
    if len(ids) != len(profiles) or set(labels) != {need["id"] for need in needs}:
        raise ValueError("Некорректные benchmark IDs")
    if any(set(labelset) != ids or any(value not in {0, 1, 2} for value in labelset.values()) for labelset in labels.values()):
        raise ValueError("Gold labels должны покрывать все пары")
    modes = {}
    for mode in ("evidence", "keyword_baseline", "without_fsp", "without_assessment"):
        ranked = {}
        for need in needs:
            candidates = copy.deepcopy(profiles)
            if mode == "keyword_baseline":
                ranked[need["id"]] = _baseline(need, candidates)
                continue
            if mode == "without_fsp":
                for candidate in candidates:
                    candidate["fsp_achievements"] = []
            if mode == "without_assessment":
                for candidate in candidates:
                    candidate["verified_grade"] = candidate.get("declared_grade")
                    candidate["verified_skills"] = candidate.get("skills", [])
                    candidate["skill_evidence"] = {}
                    candidate["test_score"] = 100
            ranked[need["id"]] = rank_candidates(need, candidates)
        modes[mode] = {
            split: {f"at_{k}": _metrics([n for n in needs if n["split"] == split], ranked, labels, k) for k in (5, 10)}
            for split in ("development", "holdout")
        }
    return {"benchmark_version": fixture["version"], "matching_version": VERSION,
            "fixture_sha256": hashlib.sha256(json.dumps(fixture, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "candidate_count": len(profiles), "need_count": len(needs), "labeled_pairs": len(profiles) * len(needs),
            "gold_provenance": fixture["provenance"], "human_validation": "NOT_CONDUCTED",
            "status": "SYNTHETIC_COMPUTATION_COMPLETED_NOT_EXTERNAL_VALIDATION", "modes": modes,
            "limitations": ["Авторская синтетическая разметка, не проверенная независимыми людьми.",
                            "Веса scorer не подбирались по этому holdout; диапазон примеров узкий.",
                            "Benchmark не доказывает качество найма или психометрическую равноценность тестов."]}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python -m app.domain.validation fixture.json report.json")
    fixture = json.loads(Path(sys.argv[1]).read_text())
    result = evaluate_fixture(fixture)
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
