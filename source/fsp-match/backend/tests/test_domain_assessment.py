import copy
import json
import unittest

from app.domain.assessment import GRADES, SPECIALIZATIONS, _rows, build_form, grade_form, public_form


def references(form):
    return {qid: copy.deepcopy(oracle["expected"]) for qid, oracle in form["_oracle"].items()}


class AssessmentTests(unittest.TestCase):
    def test_bank_all_six_categories_and_parameterized_forms(self):
        for specialization in SPECIALIZATIONS:
            for grade in GRADES:
                payloads = set()
                blueprints = set()
                for seed in range(32):
                    form = build_form(specialization, grade, seed)
                    result = grade_form(form, references(form))
                    self.assertEqual(result["score"], 100, (specialization, grade, seed, result))
                    self.assertTrue(result["passed"])
                    self.assertEqual(sum(q["max_points"] for q in form["questions"]), 100)
                    payloads.add(json.dumps(public_form(form), sort_keys=True))
                    blueprints.add(json.dumps(form["blueprint"], sort_keys=True))
                self.assertGreaterEqual(len(payloads), 2)
                self.assertEqual(len(blueprints), 1)

    def test_reproducible_seed_and_version(self):
        self.assertEqual(build_form("backend_python", "middle", "attempt-A"), build_form("backend_python", "middle", "attempt-A"))
        self.assertNotEqual(build_form("backend_python", "middle", "attempt-A")["form_id"], build_form("backend_python", "middle", "attempt-B")["form_id"])

    def test_private_projection_does_not_leak_nested_oracle(self):
        form = build_form("data_sql", "middle", 42)
        form["answers"] = "secret"
        form["blueprint"]["_private"] = "secret"
        form["rubric"]["correct_answer"] = "secret"
        form["questions"][0]["dataset"]["answers"] = "secret"
        form["questions"][0]["dataset"]["secret_hidden_cases"] = "secret"
        form["questions"][2]["options"][0]["expected"] = "secret"
        safe = public_form(form)
        self.assertNotIn("secret", json.dumps(safe))
        self.assertNotIn("_oracle", safe)
        self.assertNotIn('"fixtures":', json.dumps(safe))

    def test_no_input_no_grade_and_weak_competency_cannot_hide(self):
        form = build_form("backend_python", "junior", 5)
        self.assertEqual(grade_form(form, {})["score"], 0)
        answers = references(form)
        answers["q5"] = "wrong"
        result = grade_form(form, answers)
        self.assertEqual(result["score"], 80)
        self.assertFalse(result["passed"], "80 overall cannot confirm an unmeasured API competency")

    def test_multi_select_all_options_does_not_earn_full_marks(self):
        form = build_form("frontend_ts", "junior", 1)
        answers = references(form)
        answers["q4"] = [o["id"] for o in form["questions"][3]["options"]]
        result = grade_form(form, answers)
        self.assertLess(result["evidence"][3]["score"], 20)
        answers["q4"] = answers["q4"] * 2
        self.assertEqual(grade_form(form, answers)["evidence"][3]["score"], 0)

    def test_trace_boolean_is_not_integer(self):
        form = build_form("backend_python", "junior", 1)
        answers = references(form)
        answers["q2"] = [False, *answers["q2"][1:]]
        self.assertEqual(grade_form(form, answers)["evidence"][1]["score"], 0)

    def test_backend_mutant_repeat_and_conflict_answers_rejected(self):
        form = build_form("backend_python", "middle", 777)
        answers = references(form)
        answers["q2"][1] = "ok"
        self.assertEqual(grade_form(form, answers)["evidence"][1]["score"], 0)
        answers = references(form)
        answers["q2"][2] = "replayed"
        self.assertEqual(grade_form(form, answers)["evidence"][1]["score"], 0)

    def test_frontend_stale_response_mutant_rejected(self):
        form = build_form("frontend_ts", "middle", 777)
        answers = references(form)
        answers["q2"] = ["B", "A", "B"]
        self.assertEqual(grade_form(form, answers)["evidence"][1]["score"], 0)

    def test_python_mutable_default_fresh_list_mutant_rejected(self):
        form = build_form("backend_python", "middle", 777)
        answers = references(form)
        answers["q4"][1] = [answers["q4"][1][-1]]
        self.assertEqual(grade_form(form, answers)["evidence"][3]["score"], 0)

    def test_sql_order_irrelevant_but_column_order_matters(self):
        form = build_form("data_sql", "middle", 77)
        answers = references(form)
        answers["q1"] += " ORDER BY o.customer DESC"
        self.assertEqual(grade_form(form, answers)["evidence"][0]["score"], 20)
        answers["q1"] = "SELECT COUNT(*), o.customer, SUM(o.amount) FROM orders o WHERE EXISTS (SELECT 1 FROM items i WHERE i.order_id=o.id) GROUP BY o.customer"
        self.assertEqual(grade_form(form, answers)["evidence"][0]["score"], 0)

    def test_sql_independent_expected_fixture_and_mutants(self):
        form = build_form("data_sql", "middle", 111)
        fixture = form["_oracle"]["q1"]["fixtures"][0]
        orders = fixture["tables"]["orders"]["rows"]
        relevant = {item[1] for item in fixture["tables"]["items"]["rows"]}
        expected = {}
        for oid, customer, amount in orders:
            if oid in relevant:
                count, total = expected.get(customer, (0, 0))
                expected[customer] = (count + 1, total + amount)
        expected_rows = [[customer, *result] for customer, result in sorted(expected.items())]
        correct = sorted(_rows(form["_oracle"]["q1"]["expected"], fixture))
        self.assertEqual(correct, expected_rows)
        for query in (
            "SELECT o.customer, COUNT(*), SUM(o.amount) FROM orders o JOIN items i ON i.order_id=o.id GROUP BY o.customer",
            "SELECT o.customer, COUNT(DISTINCT o.id), SUM(DISTINCT o.amount) FROM orders o JOIN items i ON i.order_id=o.id GROUP BY o.customer",
            "SELECT customer, COUNT(*), SUM(amount) FROM orders GROUP BY customer",
        ):
            answers = references(form)
            answers["q1"] = query
            self.assertLess(grade_form(form, answers)["evidence"][0]["score"], 20, query)

    def test_hardcoded_visible_sql_rows_cannot_pass_hidden_cases(self):
        form = build_form("data_sql", "middle", 111)
        oracle = form["_oracle"]["q1"]
        rows = _rows(oracle["expected"], oracle["fixtures"][0])
        query = " UNION ALL ".join("SELECT " + ",".join(repr(value) for value in row) for row in rows)
        answers = references(form)
        answers["q1"] = query
        self.assertLess(grade_form(form, answers)["evidence"][0]["score"], 20)

    def test_sql_side_effects_external_reads_and_resource_abuse_rejected(self):
        form = build_form("data_sql", "junior", 77)
        fixture = form["_oracle"]["q1"]["fixtures"][0]
        for query in (
            "DROP TABLE sales", "DELETE FROM sales", "INSERT INTO sales VALUES (100,'evil',1,'paid')",
            "UPDATE sales SET amount=0", "ATTACH DATABASE '/tmp/domain-side-effect.db' AS evil", "PRAGMA database_list",
            "SELECT name FROM sqlite_master", "SELECT load_extension('/tmp/evil')", "SELECT randomblob(1000000000)",
            "SELECT * FROM sales; SELECT * FROM sales", "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT * FROM n",
            "SELECT count(*) FROM sales a,sales b,sales c,sales d,sales e,sales f,sales g,sales h",
        ):
            with self.assertRaises(ValueError, msg=query):
                _rows(query, fixture)

    def test_bad_submission_limits_and_unknown_category(self):
        form = build_form("backend_python", "junior", 0)
        for answers in ({"q999": 1}, {"q1": "x" * 21000}, {"q2": [float("nan")]}, [1]):
            with self.assertRaises(ValueError):
                grade_form(form, answers)
        for spec, grade, seed in (("unknown", "junior", 0), ("data_sql", "senior", 0), ("data_sql", "middle", "x" * 201)):
            with self.assertRaises(ValueError):
                build_form(spec, grade, seed)

    def test_sql_intermediate_cte_size_and_parser_limits(self):
        form = build_form("data_sql", "junior", "memory-bound")
        fixture = form["_oracle"]["q1"]["fixtures"][0]
        # Only request 1 MiB, not a dangerous GiB. The old checker allowed this
        # 1 KiB query; the new 64 KiB value limit must reject it before allocation.
        pieces = ["t0 AS MATERIALIZED (SELECT 'x' AS v)"]
        pieces += [f"t{i} AS MATERIALIZED (SELECT v||v AS v FROM t{i-1})" for i in range(1, 21)]
        query = "WITH " + ",".join(pieces) + " SELECT length(v) FROM t20"
        self.assertLess(len(query), 4000)
        with self.assertRaises(ValueError):
            _rows(query, fixture)
        for query in (
            "SELECT " + "abs(" * 60 + "1" + ")" * 60,
            " UNION ALL ".join("SELECT 1" for _ in range(11)),
            "SELECT " + ",".join("1" for _ in range(101)),
            "SELECT ?101",
        ):
            with self.assertRaises(ValueError, msg=query):
                _rows(query, fixture)
        self.assertEqual(_rows("SELECT length('bounded')", fixture), [[7]])

        # Non-materialized CTEs can expand the prepared bytecode exponentially;
        # the opcode limit also applies before older SQLite VM hooks run.
        pieces = ["t0 AS (SELECT 'x' AS v)"]
        pieces += [f"t{i} AS (SELECT v||v AS v FROM t{i-1})" for i in range(1, 16)]
        with self.assertRaises(ValueError):
            _rows("WITH " + ",".join(pieces) + " SELECT length(v) FROM t15", fixture)

    def test_sql_aggregate_result_budget_and_non_json_values(self):
        form = build_form("data_sql", "junior", "result-bound")
        fixture = form["_oracle"]["q1"]["fixtures"][0]
        pieces = ["t0 AS MATERIALIZED (SELECT 'x' AS v)"]
        pieces += [f"t{i} AS MATERIALIZED (SELECT v||v AS v FROM t{i-1})" for i in range(1, 16)]
        query = "WITH " + ",".join(pieces) + " SELECT v,v,v FROM t15"
        with self.assertRaises(ValueError):
            _rows(query, fixture)
        for query in ("SELECT CAST('x' AS BLOB)", "SELECT 1e999"):
            with self.assertRaises(ValueError):
                _rows(query, fixture)
            answers = references(form)
            answers["q1"] = query
            self.assertEqual(grade_form(form, answers)["evidence"][0]["score"], 0)

    def test_feedback_does_not_publish_expected_answers(self):
        form = build_form("data_sql", "middle", 1)
        failed = grade_form(form, {})
        dumped = json.dumps(failed)
        self.assertNotIn(form["_oracle"]["q1"]["expected"], dumped)
        self.assertNotIn("fixtures", dumped)

    def test_copied_full_answer_does_not_pass_alternate_form(self):
        for spec in SPECIALIZATIONS:
            for grade in GRADES:
                first = build_form(spec, grade, "form-A")
                scores = [grade_form(build_form(spec, grade, f"form-B-{i}"), references(first))["score"] for i in range(12)]
                self.assertLess(sum(scores) / len(scores), 65, (spec, grade, scores))


if __name__ == "__main__":
    unittest.main()
