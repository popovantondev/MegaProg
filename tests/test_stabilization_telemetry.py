import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_dev import phase_telemetry, supervisor, test_recommendation


class StabilizationTelemetryTests(unittest.TestCase):
    def test_phase_has_wall_timestamps_duration_and_separate_category_totals(self):
        state = {}
        with phase_telemetry.measure(state, 'worker_execution', 'model'):
            pass
        row = state['phase_telemetry'][0]
        self.assertEqual(row['phase'], 'worker_execution')
        self.assertGreaterEqual(row['duration_ms'], 0)
        self.assertIsNotNone(row['started_at'])
        self.assertIsNotNone(row['finished_at'])
        self.assertEqual(state['wall_time']['model_wall_time_ms'], row['duration_ms'])
        self.assertTrue(state['wall_time']['category_totals_overlap_possible'])

    def test_verify_marks_unscoped_unittest_discovery_as_full_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {}
            with patch('ai_dev.supervisor.run', return_value=(0, 'OK')):
                result = supervisor.verify(root, {'verify': [['python3', '-m', 'unittest', 'discover']],
                    'verify_timeout_seconds': 10}, root, state=state)
            self.assertTrue(result['ok'])
            row = state['phase_telemetry'][0]
            self.assertEqual(row['phase'], 'full_test_suite')
            self.assertEqual(row['category'], 'test')
            self.assertIn('duration_ms', row)

    def test_changed_python_file_maps_to_existing_test_without_running_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'tests' / 'test_router.py').write_text('')
            result = test_recommendation.recommend(root, ['ai_dev/router.py'])
            self.assertEqual(result['candidate_tests'], ['tests/test_router.py'])
            self.assertFalse(result['automatic_execution'])
            self.assertEqual(result['unmapped_files'], [])


if __name__ == '__main__':
    unittest.main()
