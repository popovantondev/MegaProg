import unittest
import os
from pathlib import Path

from ai_dev.stress import run_stress
from ai_dev.windows_check import audit


class StressAndWindowsTests(unittest.TestCase):
    def test_fake_stress_is_parallel_safe_and_does_not_use_live_adapter(self):
        result = run_stress(3)
        self.assertEqual(result["mode"], "fake")
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["cross_project_contamination"], [])
        self.assertEqual(result["chatgpt_advisory"]["imports"], "ACCEPTED_AS_HINT")
        self.assertEqual(result["account_guard_probe"], "no_free_global_slot")

    def test_live_stress_is_blocked_without_side_effects(self):
        result = run_stress(2, live=True)
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["blocked"], 1)

    def test_windows_audit_is_honest_off_windows(self):
        result = audit(Path(__file__).parents[1])
        self.assertEqual(result["overall"], "PASS" if os.name == "nt" else "NOT_RUN")
        self.assertIn("windows-launcher-present", [item["name"] for item in result["checks"]])
        if os.name != "nt":
            self.assertTrue(any(item["status"] == "NOT_RUN" for item in result["checks"]))


if __name__ == "__main__":
    unittest.main()
