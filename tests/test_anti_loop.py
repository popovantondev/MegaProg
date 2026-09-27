import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from ai_dev import anti_loop, supervisor


class AntiLoopTests(unittest.TestCase):
    def verification(self, tail, code=1):
        return {'ok': False, 'checks': [{'command': ['python3', '-m', 'unittest', 'discover', '-s', 'tests'],
            'exit_code': code, 'tail': tail}]}

    def test_first_failure_gets_one_correction(self):
        failure = anti_loop.failure_record(self.verification('ModuleNotFoundError: No module named \'telethon\'\nFAILED (errors=11)'), 'stage-1')
        self.assertFalse(anti_loop.should_stop(None, failure, None, None, [], 1)['stop'])

    def test_same_root_failure_different_count_has_same_fingerprint(self):
        first = anti_loop.failure_record(self.verification("ModuleNotFoundError: No module named 'telethon'\nFAILED (errors=11)"), 'stage')
        second = anti_loop.failure_record(self.verification("ModuleNotFoundError: No module named 'telethon'\nFAILED (errors=22)"), 'stage')
        self.assertEqual(first['signature'], second['signature'])
        self.assertTrue(anti_loop.should_stop(first, second, {'approach_id': 'a'},
            {'approach_id': 'b'}, [], 2)['stop'])

    def test_same_failure_with_new_evidence_allows_one_retry(self):
        failure = anti_loop.failure_record(self.verification('AssertionError: expected 3 actual 2'), 'stage')
        result = anti_loop.should_stop(failure, failure, {'approach_id': 'a'},
            {'approach_id': 'b'}, ['new_relevant_evidence'], 2)
        self.assertFalse(result['stop'])

    def test_different_diff_alone_does_not_count_as_progress(self):
        failure = anti_loop.failure_record(self.verification('AssertionError: expected 3 actual 2'), 'stage')
        result = anti_loop.should_stop(failure, failure, {'approach_id': 'a'},
            {'approach_id': 'b'}, [], 2)
        self.assertTrue(result['stop'])

    def test_fewer_errors_are_measurable_progress(self):
        before = anti_loop.failure_record(self.verification("ModuleNotFoundError: No module named 'x'\nFAILED (errors=12)"), 'stage')
        after = anti_loop.failure_record(self.verification("ModuleNotFoundError: No module named 'x'\nFAILED (errors=10)"), 'stage')
        self.assertEqual(before['signature'], after['signature'])
        self.assertIn('fewer_failing_checks_or_tests', anti_loop.progress_signals(before, after))

    def test_changed_root_failure_is_not_duplicate(self):
        before = anti_loop.failure_record(self.verification('AssertionError: expected 3 actual 2'), 'stage')
        after = anti_loop.failure_record(self.verification('TypeError: unsupported operand'), 'stage')
        self.assertNotEqual(before['signature'], after['signature'])
        self.assertIn('failure_signature_changed', anti_loop.progress_signals(before, after))

    def test_temp_path_normalized(self):
        one = anti_loop.failure_record(self.verification('OSError: failed /tmp/run-abc/file'), 'stage')
        two = anti_loop.failure_record(self.verification('OSError: failed /tmp/run-def/file'), 'stage')
        self.assertEqual(one['signature'], two['signature'])

    def test_secrets_redacted(self):
        one = anti_loop.failure_record(self.verification('AuthError: api_key=secret-one rejected'), 'stage')
        two = anti_loop.failure_record(self.verification('AuthError: api_key=secret-two rejected'), 'stage')
        self.assertEqual(one['signature'], two['signature'])
        packet, encoded = anti_loop.build_rescue_packet({'id': 'task', 'prompt': 'sensitive'},
            {'id': 'stage', 'title': 'Fix', 'acceptance': []}, [], 'api_key=secret-one', [],
            rescue_class={})
        self.assertNotIn('secret-one', encoded.decode())

    def test_packet_is_bounded_and_does_not_copy_transcript(self):
        packet, encoded = anti_loop.build_rescue_packet({'id': 'task', 'prompt': 'goal'},
            {'id': 'stage', 'title': 'Fix', 'acceptance': ['pass']},
            [{'attempt_id': 'a', 'attempt': 1, 'failure_signature': 'x',
              'raw_transcript': 'NEVER COPY THIS'}], 'x' * 100_000, ['src/a.py'])
        self.assertLessEqual(len(encoded), anti_loop.MAX_PACKET_BYTES)
        self.assertNotIn(b'NEVER COPY THIS', encoded)
        self.assertLessEqual(len(packet['current_diff']['bounded_fragments'].encode()), 8192)

    def test_route_infra_sol_and_astra(self):
        self.assertFalse(anti_loop.classify_rescue({'normalized_error': 'ModuleNotFoundError: no module named x'})['automatic_model'])
        self.assertEqual(anti_loop.classify_rescue({}, 'TEST_FAILURE')['model'], 'gpt-6-sol')
        self.assertEqual(anti_loop.classify_rescue({}, 'SCHEMA_CHANGED')['model'], 'gpt-6-astra')

    def test_post_rescue_repeated_failure_stops(self):
        failure = {'signature': 'same'}
        result = anti_loop.should_stop(failure, failure, {'approach_id': 'a'},
                                       {'approach_id': 'b'}, [], 3, post_rescue=True)
        self.assertTrue(result['stop'])

    def test_post_rescue_same_fingerprint_with_measurable_progress_may_continue(self):
        failure = {'signature': 'same', 'failures': [{'failing_count': 3}]}
        result = anti_loop.should_stop(failure, failure, {'approach_id': 'a'},
            {'approach_id': 'b'}, ['fewer_failing_checks_or_tests'], 3, post_rescue=True)
        self.assertFalse(result['stop'])

    def test_repeated_review_violation_has_stable_failure(self):
        first = anti_loop.review_failure('Review Cascade found contract violations: {"violations":["missing invariant"]}', 's1')
        second = anti_loop.review_failure('Review Cascade found contract violations: {"violations":["missing invariant"]}', 's1')
        self.assertEqual(first['signature'], second['signature'])
        self.assertTrue(anti_loop.should_stop(first, second, {'approach_id': 'a'},
            {'approach_id': 'b'}, [], 2)['stop'])

    def test_fresh_rescue_session_does_not_continue_worker_session_or_inherit_pin(self):
        state = {'id': 'task', 'prompt': 'goal', 'session_id': 'dirty-session', 'attempts': 2,
                 'routing': {'model': 'gpt-6-astra', 'reasoning': 'high'}}
        config = {'max_model_turns': 10, 'account_guard': {'enabled': False}}
        packet = {'task_id': 'task', 'stage_id': 'stage'}
        result = {'ok': True, 'session_id': 'fresh-session', 'message': json.dumps({
            'status': 'RETRY_WITH_NEW_APPROACH', 'root_cause_hypothesis': 'h',
            'new_evidence_required': [], 'new_approach': {'summary': 'new', 'allowed_scope_change': []},
            'plan_contract_change_required': False, 'reason': 'r'}), 'telemetry': {}}
        @contextmanager
        def slot(*args, **kwargs):
            yield object()
        with patch('ai_dev.supervisor.waiting_turn', side_effect=slot), \
             patch('ai_dev.supervisor.routing.choose', return_value={'model': 'gpt-6-sol', 'reasoning': 'medium'}) as choose, \
             patch('ai_dev.supervisor.codex.execute', return_value=result) as execute:
            answer = supervisor._run_rescue(Path(tempfile.gettempdir()), state,
                {'id': 'stage'}, packet, {'automatic_model': True, 'model': 'gpt-6-sol',
                'reasoning': 'medium', 'reason': 'engineering'}, {'executable': 'codex'},
                config, {'models': []}, lambda *args, **kwargs: None)
        self.assertEqual(answer['status'], 'RETRY_WITH_NEW_APPROACH')
        self.assertEqual(choose.call_args.kwargs['model'], 'gpt-6-sol')
        self.assertIsNone(execute.call_args.args[5])
        self.assertEqual(execute.call_args.kwargs['sandbox'], 'read-only')
        self.assertIsNone(state['session_id'])

    def test_plan_contract_revision_request_cannot_become_worker_retry(self):
        state = {'id': 'task', 'prompt': 'goal', 'session_id': None, 'attempts': 2}
        config = {'max_model_turns': 10, 'account_guard': {'enabled': False}}
        result = {'ok': True, 'session_id': 'new', 'message': json.dumps({
            'status': 'PLAN_CONTRACT_REVISION_REQUIRED', 'root_cause_hypothesis': 'wrong contract',
            'new_evidence_required': [], 'new_approach': {'summary': '', 'allowed_scope_change': []},
            'plan_contract_change_required': True, 'reason': 'contract invalid'}), 'telemetry': {}}
        @contextmanager
        def slot(*args, **kwargs):
            yield object()
        with patch('ai_dev.supervisor.waiting_turn', side_effect=slot), \
             patch('ai_dev.supervisor.routing.choose', return_value={}), \
             patch('ai_dev.supervisor.codex.execute', return_value=result):
            answer = supervisor._run_rescue(Path(tempfile.gettempdir()), state,
                {'id': 'stage'}, {}, {'automatic_model': True, 'model': 'gpt-6-astra',
                'reasoning': 'medium', 'reason': 'high risk'}, {'executable': 'codex'},
                config, {'models': []}, lambda *args, **kwargs: None)
        self.assertEqual(answer['status'], 'PLAN_CONTRACT_REVISION_REQUIRED')

    def test_rescue_classifies_dependency_without_model_and_astra_only_for_trigger(self):
        self.assertEqual(anti_loop.classify_rescue({'normalized_error': 'ModuleNotFoundError'})['model'], None)
        self.assertEqual(anti_loop.classify_rescue({}, 'SCHEMA_CHANGED')['model'], 'gpt-6-astra')

    def test_report_exposes_attempt_history_and_rescue_telemetry(self):
        from ai_dev.report import build_report
        state = {'id': 'task', 'attempt_history': [{'attempt_id': 'a'}],
                 'anti_loop_telemetry': {'circuit_breaker_fired': True},
                 'rescue_status': 'NEEDS_HUMAN_DECISION', 'rescue_packet': {'bytes': 10}}
        report = build_report(Path(tempfile.gettempdir()), state, generated_at=0)
        self.assertEqual(report['attempt_history'][0]['attempt_id'], 'a')
        self.assertTrue(report['anti_loop_telemetry']['circuit_breaker_fired'])
        self.assertEqual(report['rescue_status'], 'NEEDS_HUMAN_DECISION')



if __name__ == '__main__':
    unittest.main()
