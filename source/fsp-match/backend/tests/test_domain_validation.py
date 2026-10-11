import json
import math
import unittest
from pathlib import Path

from app.domain.validation import _metrics, evaluate_fixture


class ValidationTests(unittest.TestCase):
    def test_metrics_fixed_denominator_order_and_no_positive_query(self):
        needs = [{"id": "q1"}, {"id": "empty"}]
        rankings = {"q1": [{"id": "irrelevant"}, {"id": "relevant"}], "empty": []}
        labels = {"q1": {"irrelevant": 0, "relevant": 2}, "empty": {"irrelevant": 0, "relevant": 0}}
        metrics = _metrics(needs, rankings, labels, 5)
        self.assertEqual(metrics["queries"][0]["precision_at_k"], 0.2)
        self.assertEqual(metrics["queries"][0]["precision_of_returned"], 0.5)
        self.assertAlmostEqual(metrics["queries"][0]["ndcg_at_k"], 1 / math.log2(3))
        self.assertIsNone(metrics["queries"][1]["ndcg_at_k"])
        self.assertEqual(metrics["critical_mismatches_in_top_k"], 1)

    def test_frozen_authored_fixture_has_480_labels_and_honest_status(self):
        root = Path(__file__).resolve().parents[2]
        fixture = json.loads((root / "validation/benchmarks/matching-v1.json").read_text())
        before = json.dumps(fixture, sort_keys=True)
        result = evaluate_fixture(fixture)
        self.assertEqual(result["labeled_pairs"], 480)
        self.assertEqual(result["candidate_count"], 40)
        self.assertEqual(result["human_validation"], "NOT_CONDUCTED")
        self.assertEqual(result["gold_provenance"]["independent_human_review"], "PENDING")
        self.assertEqual(json.dumps(fixture, sort_keys=True), before)
        self.assertEqual(fixture["labels"]["b-payments"]["b05"], 0)
        self.assertFalse(any(fixture["labels"]["f-unmeasured"].values()))
        self.assertEqual(result["modes"]["evidence"]["holdout"]["at_5"]["critical_mismatches_in_top_k"], 0)


if __name__ == "__main__":
    unittest.main()
