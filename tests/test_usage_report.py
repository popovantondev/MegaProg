import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from ai_dev.cli import main
from ai_dev.usage_report import aggregate


class UsageReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runs = self.root / '.ai-dev' / 'runs'

    def write_run(self, name, data, state=None):
        directory = self.runs / name
        directory.mkdir(parents=True)
        (directory / 'report.json').write_text(json.dumps(data), encoding='utf-8')
        if state is not None:
            (directory / 'state.json').write_text(json.dumps(state), encoding='utf-8')

    def test_aggregates_tokens_and_recorded_percentages(self):
        self.write_run('one', {'id': 'one', 'kind': 'normal', 'attempts': [
            {'model': 'm1', 'reasoning': 'low', 'codex': {'usage': {
                'input_tokens': 10, 'cached_input_tokens': 2, 'output_tokens': 5,
                'reasoning_output_tokens': 3}}}]})
        result = aggregate(self.root)
        self.assertEqual(result['totals']['tasks'], 1)
        self.assertEqual(result['totals']['attempts'], 1)
        self.assertEqual(result['totals']['recorded_tokens'], 20)
        self.assertEqual(result['groups'][0]['percent_of_recorded_tokens'], 100.0)
        self.assertEqual(result['groups'][0]['kind'], 'normal')
        self.assertEqual(result['source'], 'recorded local usage')

    def test_duplicate_report_roots_are_counted_once(self):
        data = {'id': 'same', 'attempts': [{'model': 'm1', 'reasoning': 'low',
                                             'usage': {'input_tokens': 4}}]}
        self.write_run('same', data)
        result = aggregate(self.root, [self.root, self.runs])
        self.assertEqual(result['totals']['tasks'], 1)
        self.assertEqual(result['totals']['input_tokens'], 4)

    def test_missing_usage_unknown_model_and_json_output(self):
        self.write_run('missing', {'id': 'missing', 'attempts': [
            {'reasoning': 'medium', 'codex': {'ok': True}}]}, {'id': 'missing'})
        result = aggregate(self.root)
        self.assertEqual(result['missing_usage'], {'tasks': 1, 'attempts': 1})
        self.assertEqual(result['unknown_model'], {'tasks': 1, 'attempts': 1})
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['-C', str(self.root), 'usage-report', '--json']), 0)
        encoded = json.loads(output.getvalue())
        self.assertEqual(encoded['source'], 'recorded local usage')
        self.assertFalse(encoded['account_level_breakdown']['available'])
        self.assertIn('не процент общего лимита Codex', encoded['disclaimer'])

    def test_state_reasons_retry_statuses_and_unknown_are_reported(self):
        self.write_run('routing', {
            'id': 'routing', 'status': 'BLOCKED',
            'attempts': [
                {'codex': {'usage': {'input_tokens': 1}}},
                {'model': 'gpt-5.6-terra', 'codex': {'usage': {'output_tokens': 2}}},
            ]
        }, {
            'id': 'routing', 'status': 'BLOCKED',
            'decisions': [
                {'model': 'gpt-5.6-luna', 'reasoning': 'medium', 'kind': 'normal',
                 'reason': 'Тип задачи: normal; первый доступный вариант по политике проекта.'},
                {'model': 'gpt-5.6-terra', 'reasoning': 'medium', 'kind': 'normal',
                 'reason': 'Повышение после провала проверок.'},
            ],
            'guard': {'acquired': False, 'reason': 'no_free_slot'},
        })
        result = aggregate(self.root)
        self.assertEqual(result['totals']['blocked'], 1)
        self.assertEqual(result['totals']['retry'], {'tasks': 1, 'attempts': 1})
        self.assertEqual(result['guard_blocks'], 1)
        self.assertEqual(result['routing_events']['escalation']['attempts'], 1)
        self.assertEqual(result['groups'][0]['reasoning'], 'medium')
        self.assertTrue(any('Повышение после провала проверок.' in reason
                            for group in result['groups'] for reason in group['routing_reasons']))
        self.assertIn('Spark пропущен', '\n'.join(result['improvement_hints']))
        self.assertIn('(unknown)', result['spark']['skipped_reasons'])

    def test_reports_tier_guard_limits_slots_and_block_reasons(self):
        self.write_run('guard', {'id': 'guard', 'status': 'BLOCKED', 'attempts': [{
            'model': 'gpt-6-astra', 'reasoning': 'low', 'kind': 'expert',
            'codex': {'usage': {'input_tokens': 7, 'output_tokens': 3}}}],
            'usage_guard': {'acquired': False, 'reason': 'no_free_tier_slot',
                            'tier': 'expensive', 'max_concurrent': 5,
                            'tier_limits': {'cheap': 5, 'terra': 3, 'sol': 2, 'expensive': 1},
                            'occupied_slots': {'global': 2, 'expensive': 1},
                            'blocked_slots': {'global': 2, 'expensive': 1}}})
        result = aggregate(self.root)
        self.assertEqual(result['groups'][0]['tier'], 'expensive')
        self.assertEqual(result['guard']['tier_limits']['expensive'], 1)
        self.assertEqual(result['guard']['occupied_slots']['expensive'], 1)
        self.assertEqual(result['guard']['block_reasons']['no_free_tier_slot'], 1)
        self.assertIn('Guard заблокировал', '\n'.join(result['improvement_hints']))

    def test_guard_block_without_model_turn_does_not_create_missing_usage(self):
        self.write_run('guard', {'id': 'guard', 'status': 'BLOCKED', 'attempts': [],
                                 'usage_guard': {'acquired': False, 'reason': 'no_free_tier_slot'}})
        result = aggregate(self.root)
        self.assertEqual(result['totals']['attempts'], 0)
        self.assertEqual(result['missing_usage'], {'tasks': 0, 'attempts': 0})
        self.assertIn('usage для них не расходуется', '\n'.join(result['improvement_hints']))

    def test_review_imports_are_reported_as_hints_only(self):
        directory = self.root / '.ai-dev' / 'reviews'
        directory.mkdir(parents=True)
        (directory / 'one.json').write_text(json.dumps({
            'artifact_type': 'untrusted_chatgpt_advisory', 'status': 'ACCEPTED_AS_HINT'}), encoding='utf-8')
        (directory / 'two.json').write_text(json.dumps({
            'artifact_type': 'untrusted_chatgpt_advisory', 'status': 'STALE'}), encoding='utf-8')
        result = aggregate(self.root)
        self.assertEqual(result['review_imports'], {'count': 2, 'accepted_as_hint': 1, 'stale': 1})
        self.assertIn('Импортировано advisory', '\n'.join(result['improvement_hints']))

    def test_chatgpt_policy_fields_and_hint_are_reported(self):
        self.write_run('advisory', {'id': 'advisory', 'chatgpt_advisory': {
            'enabled': True, 'reason': 'diagnosis', 'level': 'normal',
            'cache_status': 'stale'}})
        result = aggregate(self.root)
        policy = result['chatgpt_advisory']
        self.assertEqual(policy['eligible'], 1)
        self.assertEqual(policy['levels']['normal'], 1)
        self.assertEqual(policy['cache']['stale'], 1)
        self.assertIn('Luna/Spark', '\n'.join(result['improvement_hints']))


if __name__ == '__main__':
    unittest.main()
