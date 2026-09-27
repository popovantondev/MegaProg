import json
import tempfile
import unittest
from pathlib import Path

from ai_dev.report import build_report, render_markdown, write_report


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = {
            'id': 'run-1',
            'prompt': 'Исправить задачу',
            'status': 'COMPLETED',
            'created_at': 1000,
            'updated_at': 1010,
            'reason': 'Проверки пройдены',
            'decisions': [{'model': 'gpt-test', 'reasoning': 'medium',
                           'reason': 'Выбрана модель', 'runtime_verified': True}],
            'attempts_detail': [{'codex': {'ok': True, 'session_id': 's1'},
                                 'verification': {'ok': True, 'checks': []}}],
            'result': {'ok': True, 'session_id': 's1'},
            'verification': {'ok': True, 'checks': []},
            'diff': 'answer.py | 1 +',
            'git_status': ' M answer.py',
            'guard': {'enabled': True, 'max_concurrent': 1, 'acquired': True,
                      'released': True},
            'fresh_limits': None,
            'prompt_metrics': {'budget_bytes': 32768, 'prompt_chars': 100,
                               'prompt_bytes': 120, 'truncated': True,
                               'truncated_sections': ['lessons']},
        }

    def test_build_report_contains_task_and_attempt_evidence(self):
        report = build_report(self.root, self.state, generated_at=1020)
        self.assertEqual(report['id'], 'run-1')
        self.assertEqual(report['models'], [{'model': 'gpt-test', 'reasoning': 'medium'}])
        self.assertEqual(report['attempts'][0]['codex']['session_id'], 's1')
        self.assertEqual(report['verification']['ok'], True)
        self.assertEqual(report['changed']['diff_stat'], 'answer.py | 1 +')
        self.assertEqual(report['timestamps']['generated_at'], '1970-01-01T00:17:00+00:00')
        self.assertTrue(report['next_step'])
        self.assertEqual(report['usage_guard']['max_concurrent'], 1)
        self.assertIsNone(report['fresh_limits'])
        self.assertEqual(report['prompt_budget']['prompt_bytes'], 120)
        self.assertTrue(report['prompt_budget']['truncated'])

    def test_write_report_creates_json_and_markdown(self):
        write_report(self.root, self.state)
        directory = self.root / '.ai-dev' / 'runs' / 'run-1'
        saved = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
        markdown = (directory / 'report.md').read_text(encoding='utf-8')
        self.assertEqual(saved['status'], 'COMPLETED')
        self.assertIn('Исправить задачу', markdown)
        self.assertIn('gpt-test', render_markdown(saved))
        self.assertIn('prompt_budget', saved)

    def test_ungranted_guard_decision_is_not_reported_as_model_attempt(self):
        state = dict(self.state, status='BLOCKED', attempts=0, attempts_detail=[],
                     decisions=[{'model': 'gpt-5.6-luna', 'reasoning': 'medium',
                                 'reason': 'first available'}], last_failure='account_guard')
        report = build_report(self.root, state)
        self.assertEqual(report['attempts'], [])
        self.assertEqual(report['models'], [])
        self.assertIn('Модель не запускалась', report['next_step'])


if __name__ == '__main__':
    unittest.main()
