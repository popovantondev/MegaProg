import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_dev import codex
from ai_dev.report import build_report
from ai_dev.telemetry import UNKNOWN, attach_context, runtime_model, runtime_reasoning


class TurnTelemetryTests(unittest.TestCase):
    def test_codex_turn_records_config_usage_duration_and_unknown_observed_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            events = [
                {'type': 'thread.started', 'thread_id': 'session-1'},
                {'type': 'turn.started'},
                {'type': 'turn.completed', 'usage': {
                    'input_tokens': 120, 'cached_input_tokens': 80,
                    'cache_write_input_tokens': 12, 'output_tokens': 9,
                    'reasoning_output_tokens': 4}},
            ]
            output = '\n'.join(json.dumps(item) for item in events) + '\n'
            config = {'model': 'gpt-6-astra', 'reasoning': 'medium',
                      'timeout_seconds': 0, 'detect_stall': False}
            with patch('ai_dev.codex.run', return_value=(0, output)), \
                 patch('ai_dev.codex.time.monotonic', side_effect=[10.0, 12.5]):
                result = codex.execute(root, config, {'executable': 'codex'},
                                        'task', root / 'turn.jsonl')
            turn = result['telemetry']
            self.assertEqual(turn['configured_model'], 'gpt-6-astra')
            self.assertEqual(turn['configured_reasoning'], 'medium')
            self.assertEqual(turn['observed_model'], UNKNOWN)
            self.assertEqual(turn['observed_reasoning'], UNKNOWN)
            self.assertEqual(turn['session_id'], 'session-1')
            self.assertEqual(turn['usage']['input_tokens'], 120)
            self.assertEqual(turn['usage']['cached_input_tokens'], 80)
            self.assertEqual(turn['usage']['cache_write_input_tokens'], 12)
            self.assertEqual(turn['usage']['output_tokens'], 9)
            self.assertEqual(turn['usage']['reasoning_output_tokens'], 4)
            self.assertEqual(turn['duration_seconds'], 2.5)
            self.assertEqual(result['usage'], events[-1]['usage'])  # legacy field preserved

    def test_explicit_runtime_fields_are_used_only_when_codex_reports_them(self):
        events = [{'type': 'turn.completed', 'model': 'gpt-6-astra',
                   'reasoning_effort': 'medium'}]
        self.assertEqual(runtime_model(events), 'gpt-6-astra')
        self.assertEqual(runtime_reasoning(events), 'medium')

    def test_supervisor_adds_role_stage_and_retry_without_changing_legacy_usage(self):
        result = {'ok': True, 'session_id': 'session-2', 'usage': {'input_tokens': 7}}
        turn = attach_context(result, task_id='task-1', stage_id='stage-2',
                              role='recovery', retry_number=2, attempt_number=4,
                              escalation_reason='two verified failures',
                              configured_model='gpt-6-astra', configured_reasoning='low')
        self.assertEqual((turn['task_id'], turn['stage_id'], turn['role']),
                         ('task-1', 'stage-2', 'recovery'))
        self.assertEqual((turn['retry_number'], turn['attempt_number']), (2, 4))
        self.assertEqual(turn['escalation_reason'], 'two verified failures')
        self.assertEqual(result['usage']['input_tokens'], 7)

    def test_report_exposes_turn_telemetry_additively(self):
        state = {'id': 'task-1', 'status': 'COMPLETED', 'attempts': 1,
                 'decisions': [{'model': 'gpt-6-luna', 'reasoning': 'medium',
                                'attempt': 1, 'role': 'worker'}],
                 'attempts_detail': [{'attempt': 1, 'codex': {'ok': True,
                     'usage': {'input_tokens': 5}, 'telemetry': {
                         'task_id': 'task-1', 'role': 'worker',
                         'observed_model': UNKNOWN}}}],
                 'config': {}}
        report = build_report(Path(tempfile.gettempdir()), state, generated_at=0)
        self.assertEqual(report['turn_telemetry'][0]['task_id'], 'task-1')
        self.assertEqual(report['attempts'][0]['codex']['usage']['input_tokens'], 5)


if __name__ == '__main__':
    unittest.main()
