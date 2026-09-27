import json
import tempfile
import unittest
from pathlib import Path

from ai_dev.context_compaction import (build_summary, inspect_jsonl, should_compact,
                                       summary_is_fresh)


class ContextCompactionTests(unittest.TestCase):
    def test_usage_events_and_context_error_are_measured(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'codex.jsonl'
            path.write_text(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 123}}) +
                            '\n{"type":"error","message":"ContextWindowExceeded"}\n' +
                            'damaged\n', encoding='utf-8')
            metrics = inspect_jsonl(path)
        self.assertEqual(metrics['estimated_tokens'], 123)
        self.assertTrue(metrics['context_window_exceeded'])
        self.assertEqual(metrics['corrupt_lines'], 1)
        self.assertEqual(should_compact(metrics)[1], 'ContextWindowExceeded')

    def test_summary_is_bounded_and_excludes_transcript_and_generated_context(self):
        state = {'id': 'task-1', 'prompt': 'goal ' * 2000, 'status': 'RETRY',
                 'verification': {'ok': True}, 'result': {'usage': {'input_tokens': 9}},
                 'decisions': [{'model': 'terra', 'reasoning': 'medium'}]}
        summary = build_summary(state, ['src/main.py', '.ai-dev/runs/x/codex.jsonl', '/tmp/x'])
        self.assertLessEqual(len(summary.encode('utf-8')), 12000)
        self.assertIn('task-1', summary)
        self.assertNotIn('input_tokens', summary)
        self.assertNotIn('.ai-dev', summary)
        self.assertTrue(summary_is_fresh(summary, state))
        self.assertFalse(summary_is_fresh(summary, dict(state, id='other')))

    def test_empty_and_missing_history_are_safe(self):
        metrics = inspect_jsonl('/definitely/missing/codex.jsonl')
        self.assertFalse(should_compact(metrics)[0])


if __name__ == '__main__':
    unittest.main()
