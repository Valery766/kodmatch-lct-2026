import copy
import unittest

from app.domain.matching import rank_candidates


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.need = {"specialization": "backend_python", "grade": "middle", "required_skills": ["python", "api"],
                     "preferred_skills": ["testing"], "salary_min": 100, "salary_max": 200, "work_format": "remote"}
        self.profile = {"id": 1, "specialization": "backend_python", "declared_grade": "middle", "verified_grade": "middle",
                        "skills": ["python", "api"], "verified_skills": ["python", "api", "testing"], "visible": True,
                        "test_score": 100, "salary_min": 150, "salary_max": 190, "work_format": "remote", "fsp_achievements": []}

    def test_strong_nonmember_can_rank_first_and_tied_fsp_improves(self):
        weak = {**self.profile, "id": 2, "test_score": 60, "fsp_achievements": [{"verified": True, "source": "demo_registry", "synthetic": True}]}
        result = rank_candidates(self.need, [weak, self.profile])
        self.assertEqual([p["id"] for p in result], [1, 2])
        tied = {**weak, "test_score": 100}
        self.assertEqual(rank_candidates(self.need, [self.profile, tied])[0]["id"], 2)
        self.assertIn("демонстрационный", " ".join(rank_candidates(self.need, [tied])[0]["explanation"]))

    def test_self_report_keyword_stuffing_or_unconfirmed_grade_never_wins(self):
        fake = {**self.profile, "id": 2, "declared_grade": "senior", "verified_grade": None, "skills": ["python", "api"] * 30}
        junior = {**self.profile, "id": 3, "declared_grade": "senior", "verified_grade": "junior"}
        self.assertEqual([p["id"] for p in rank_candidates(self.need, [fake, junior, self.profile])], [1])

    def test_failed_required_evidence_and_invisible_profile_excluded(self):
        failed = {**self.profile, "skill_evidence": {"api": "failed"}}
        hidden = {**self.profile, "visible": False}
        self.assertEqual(rank_candidates(self.need, [failed, hidden]), [])

    def test_unknown_evidence_is_gap_not_falsely_confirmed(self):
        gap = {**self.profile, "verified_skills": ["python"], "fsp_achievements": [{"verified": True, "source": "demo_registry"}]}
        result = rank_candidates(self.need, [gap])[0]
        self.assertEqual(result["matching"]["unknown_requirements"], ["api"])
        self.assertFalse(result["matching"]["exact_required_match"])
        self.assertEqual(result["matching"]["components"]["fsp"], 0)
        self.assertEqual(rank_candidates({**self.need, "require_verified_skills": True}, [gap]), [])

    def test_salary_format_location_and_grade_constraints(self):
        profiles = [{**self.profile, "salary_min": 300, "salary_max": 400}, {**self.profile, "work_format": "office"},
                    {**self.profile, "specialization": "data_sql"}, {**self.profile, "salary_min": 200, "salary_max": 100}]
        self.assertEqual(rank_candidates(self.need, profiles), [])
        self.assertEqual(rank_candidates({**self.need, "work_format": "office", "location": "Москва"}, [{**self.profile, "work_format": "office", "location": "Казань"}]), [])
        remote = {**self.profile, "location": "Казань"}
        self.assertEqual(len(rank_candidates({**self.need, "location": "Москва"}, [remote])), 1)

    def test_unverified_fsp_and_irrelevant_achievement_no_bonus(self):
        for achievement in ({"source": "demo_registry", "verified": False}, {"source": "self_report", "verified": True},
                            {"source": "fsp_registry", "verified": True, "specialization": "data_sql"}):
            result = rank_candidates(self.need, [{**self.profile, "fsp_achievements": [achievement]}])[0]
            self.assertEqual(result["matching"]["components"]["fsp"], 0)

    def test_no_contacts_or_private_nested_achievements_in_shortlist(self):
        profile = {**self.profile, "email": "private@example.invalid", "phone": "secret", "resume_text": "private resume",
                   "fsp_achievements": [{"source": "demo_registry", "verified": True, "email": "secret"}]}
        safe = rank_candidates(self.need, [profile])[0]
        self.assertNotIn("email", safe)
        self.assertNotIn("phone", safe)
        self.assertNotIn("resume_text", safe)
        self.assertNotIn("email", safe["fsp_achievements"][0])

    def test_deterministic_order_no_input_mutation_and_aliases(self):
        profiles = [{**self.profile, "id": "b"}, {**self.profile, "id": "a", "verified_skills": ["py", "rest api", "tests"]}]
        before = copy.deepcopy(profiles)
        first = rank_candidates(self.need, profiles)
        self.assertEqual(first, rank_candidates(self.need, profiles[::-1]))
        self.assertEqual([p["id"] for p in first], ["a", "b"])
        self.assertEqual(profiles, before)

    def test_missing_scores_are_unknown_not_100_and_false_boolean_ignored(self):
        for score in (None, True, float("nan"), float("inf")):
            profile = {**self.profile, "test_score": score}
            self.assertEqual(rank_candidates(self.need, [profile])[0]["matching"]["components"]["test_quality"], 0)


if __name__ == "__main__":
    unittest.main()
