import unittest

from ai_dev.verification_evidence import compare, summarize


def check(name="check", code=1, tail="AssertionError: bad"):
    return {"command": ["python", "-m", name], "exit_code": code, "tail": tail}


class VerificationEvidenceTests(unittest.TestCase):
    def test_malformed_and_ok_alone_are_unknown(self):
        self.assertEqual(summarize({"ok": True})["status"], "unknown")
        self.assertEqual(summarize({"ok": True, "checks": "bad"})["status"], "unknown")
        self.assertEqual(summarize({"ok": True, "checks": [check(code=True)]})["status"], "unknown")

    def test_concrete_pass_and_missing_counts(self):
        self.assertEqual(summarize({"ok": True, "checks": [check(code=0, tail="ok")]})["status"], "pass")
        item = summarize({"ok": False, "checks": [check(tail="FAILED")]})
        self.assertNotIn(item["command_ids"][0], item["failing_tests"])

    def test_timing_only_variations_keep_fingerprint(self):
        a = {"ok": False, "checks": [check(tail="FAILED (failures=1, errors=0) in 1.0s") ]}
        b = {"ok": False, "checks": [check(tail="FAILED (failures=1, errors=0) in 9.0s") ]}
        self.assertEqual(compare(a, b)["outcome"], "unchanged")

    def test_equal_counts_but_changed_failure_is_unknown(self):
        a = {"ok": False, "checks": [check(tail="FAILED (failures=1, errors=0): first") ]}
        b = {"ok": False, "checks": [check(tail="FAILED (failures=1, errors=0): second") ]}
        self.assertEqual(compare(a, b)["outcome"], "unknown")

    def test_subset_and_count_reduction_improve(self):
        a = {"ok": False, "checks": [check("a"), check("b") ]}
        b = {"ok": False, "checks": [check("a", tail="AssertionError: bad") ]}
        self.assertEqual(compare(a, b)["outcome"], "unknown")  # coverage changed
        a = {"ok": False, "checks": [check(tail="FAILED (failures=2, errors=0)")]}
        b = {"ok": False, "checks": [check(tail="FAILED (failures=1, errors=0)")]}
        self.assertEqual(compare(a, b)["outcome"], "improved")

    def test_regression_and_different_command(self):
        a = {"ok": False, "checks": [check(tail="FAILED (failures=1, errors=0)")]}
        b = {"ok": False, "checks": [check(tail="FAILED (failures=2, errors=0)")]}
        self.assertEqual(compare(a, b)["outcome"], "regressed")
        c = {"ok": False, "checks": [check("other")]}
        self.assertEqual(compare(a, c)["outcome"], "unknown")

    def test_exchanged_failed_commands_are_unknown(self):
        a = {"ok": False, "checks": [check("a"), check("b", code=0)]}
        b = {"ok": False, "checks": [check("a", code=0), check("b")]}
        self.assertEqual(compare(a, b)["outcome"], "unknown")

    def test_actual_subset_and_superset(self):
        a = {"ok": False, "checks": [check("a"), check("b")]}
        b = {"ok": False, "checks": [check("a"), check("b", code=0)]}
        self.assertEqual(compare(a, b)["outcome"], "improved")
        self.assertEqual(compare(b, a)["outcome"], "regressed")

    def test_pytest_counts_and_parser_compatibility(self):
        def record(tail):
            return {"ok": False, "checks": [check(tail=tail)]}
        a = record("=== 3 failed, 4 passed, 1 error in 0.12s ===")
        b = record("=== 1 failed, 4 passed in 0.50s ===")
        self.assertEqual(compare(a, b)["outcome"], "improved")
        self.assertEqual(compare(a, record("FAILED (failures=1, errors=0)"))["outcome"], "unknown")
        self.assertEqual(compare(a, record("=== 3 failed, 4 passed, 1 error in 9.23s ==="))["outcome"], "unchanged")

    def test_invalid_and_oversized_evidence_cannot_prove_progress(self):
        for value in (None, [], {}, {"ok": True, "checks": []},
                      {"ok": False, "checks": [check(code=0)]},
                      {"ok": True, "checks": [check()]},
                      {"ok": False, "checks": [check(), check()]},
                      {"ok": False, "checks": [check(tail="x" * 6001)]}):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(summarize(value)["status"], "unknown")
                self.assertEqual(compare(value, value)["outcome"], "unknown")
        partial = summarize({"ok": False, "checks": [check(), None]})
        self.assertEqual(len(partial["failed_command_ids"]), 1)
        bounded = summarize({"ok": False, "checks": [check(str(i)) for i in range(40)]})
        self.assertLessEqual(len(bounded["command_ids"]), 32)
        self.assertFalse(bounded["complete"])

    def test_unrecognized_or_ambiguous_counts_are_absent(self):
        for tail in ("3 failed because a socket closed", "FAILED (failures=1, errors=0) junk",
                     "FAILED (failures=1, errors=0)\nFAILED (failures=2, errors=0)",
                     "=== 2 failed, 1 failed in 1.0s ===", "no summary"):
            self.assertEqual(summarize({"ok": False, "checks": [check(tail=tail)]})["failing_tests"], {})
        huge_count = '9' * 4500 + ' failed in 1.0s'
        self.assertEqual(summarize({"ok": False, "checks": [check(tail=huge_count)]})['failing_tests'], {})

    def test_real_unittest_timing_and_meaningful_numbers(self):
        a = {"ok": False, "checks": [check(tail="value 10\nRan 2 tests in 0.001s\nFAILED (failures=1, errors=0)")]}
        b = {"ok": False, "checks": [check(tail="value 10\nRan 2 tests in 9.501s\nFAILED (failures=1, errors=0)")]}
        self.assertEqual(compare(a, b)["outcome"], "unchanged")
        b["checks"][0]["tail"] = b["checks"][0]["tail"].replace("value 10", "value 11")
        self.assertEqual(compare(a, b)["outcome"], "unknown")

    def test_distinct_unicode_diagnostics_are_not_collapsed(self):
        a = {"ok": False, "checks": [check(tail="\ud800")]}
        b = {"ok": False, "checks": [check(tail="?")]}
        self.assertEqual(compare(a, b)["outcome"], "unknown")

    def test_standard_unittest_omitted_zero_categories(self):
        def record(tail):
            return {"ok": False, "checks": [check(tail=tail)]}
        self.assertEqual(compare(record('FAILED (failures=3)'), record('FAILED (failures=1)'))['outcome'], 'improved')
        self.assertEqual(compare(record('FAILED (errors=1)'), record('FAILED (errors=2)'))['outcome'], 'regressed')


if __name__ == "__main__":
    unittest.main()
