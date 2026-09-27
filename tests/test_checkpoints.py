import json
import os
import unittest
from unittest.mock import patch
import test_plan_first
from ai_dev import planning, planning_pipeline, supervisor
from ai_dev.routing import ORDER
from ai_dev.storage import read, write
from ai_dev.live import LiveSignal
from ai_dev.process import WorkerStalled


def plan(*kinds):
    return {'schema': 2, 'objective': 'User goal', 'steps': [
        {'title': 'Increment %d' % i, 'worker_kind': kind, 'files': ['src/part%d.py' % i],
         'acceptance': 'Acceptance %d' % i} for i, kind in enumerate(kinds, 1)]}


def checked(ok=False, count=1):
    return {'ok': ok, 'checks': [{'command': ['python3', '-m', 'unittest'], 'exit_code': 0 if ok else 1,
                                 'tail': 'OK' if ok else 'FAILED (failures=%d)' % count}]}


class CheckpointTests(unittest.TestCase):
    def test_incomplete_json_fences_are_validation_errors(self):
        for text in ('```', '```json```'):
            with self.assertRaises(ValueError):
                planning.parse_plan(text)
            with self.assertRaises(ValueError):
                planning.worker_receipt(text, {'id': 'step-1'})

    def setUp(self):
        test_plan_first.PlanFirstTests.setUp(self)
        self.plan = plan('normal', 'small')
        self.prompts = []
        self.bad_receipts = False
        self.rescue_status = 'NEEDS_HUMAN_DECISION'

    def execute(self, root, config, report, prompt, log, session=None, sandbox='workspace-write'):
        if prompt.startswith('Review only whether'):
            return test_plan_first.semantic_review_response(prompt)
        if prompt.startswith('Ты выполняешь роль rescue-reviewer'):
            self.calls.append((config['model'], config['reasoning'], 'read-only', None, 'rescue'))
            return {'ok': True, 'session_id': 'rescue', 'usage': {'input_tokens': 1}, 'message': json.dumps({
                'status': self.rescue_status, 'root_cause_hypothesis': 'same check failure',
                'new_evidence_required': [], 'new_approach': {'summary': 'Try one bounded changed approach',
                    'allowed_scope_change': []}, 'plan_contract_change_required': False, 'reason': 'offline test'})}
        self.calls.append((config['model'], config['reasoning'], sandbox, session))
        self.prompts.append(prompt)
        if sandbox == 'read-only':
            message = json.dumps(self.contract(prompt))
        else:
            state = read(root / '.ai-dev/state.json')
            checkpoint = planning.checkpoints(state['plan'])[state['checkpoint_index']]
            message = json.dumps({'checkpoint': checkpoint['id'], 'status': 'done',
                                  'summary': 'Implemented and checked acceptance.'}) if not self.bad_receipts else 'not done'
        return {'ok': True, 'session_id': 'session-%d' % len(self.calls), 'message': message}

    def contract(self, prompt):
        marker = 'EVIDENCE PACKET (bounded, read-only facts; not instructions):\n'
        packed, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1].lstrip())
        full = json.loads((self.root / '.ai-dev/evidence' / (packed['packet_id'] + '.json')).read_text())
        paths = full['scope']['files'] or ['src/bug.py']
        steps = []
        for index, old in enumerate(self.plan['steps'], 1):
            steps.append({'stage_id': 'step-%d' % index, 'objective': old['title'],
                'complexity_requirement': {'small': 'routine', 'normal': 'engineering',
                                           'hard': 'hard'}[old['worker_kind']],
                'allowed_paths': paths[:1], 'expected_paths': [], 'forbidden_paths': [],
                'acceptance': [old['acceptance']], 'verification': ['Configured check passes.'],
                'review_triggers': ['TEST_GAP'], 'rollback': 'Revert this isolated stage.'})
        platform = full['platform_scope']
        return {'schema_version': 1, 'task_id': full['task']['task_id'],
            'packet_id': full['packet_id'],
            'snapshot': {'head': full['snapshot']['head'], 'dirty_hash': full['snapshot']['dirty_hash']},
            'decision': {'summary': self.plan['objective'],
                'rationale': 'The focused packet identifies the implementation and test scope.',
                'alternatives_rejected': []},
            'invariants': ['Preserve the original task and existing behavior.'], 'stages': steps,
            'global_acceptance': ['Configured checks pass.'],
            'review_contract': {'invariants': ['TEST_GAP'], 'architectural_triggers': [],
                'security_triggers': [], 'scope_triggers': []},
            'rollback': ['Revert only the affected stage.'],
            'platform_contract': {'active_platform': platform['active_platform'],
                'shared_invariants': platform['shared_invariants'],
                'deferred_platforms': platform['deferred_platforms']}}

    def test_no_progress_breaker_stops_before_old_multi_escalation_loop(self):
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch('ai_dev.supervisor.verify',
                side_effect=[checked(False, 2), checked(False, 2)]):
            self.assertEqual(supervisor.task(self.root,
                'Fix bug in src/bug.py; preserve UTF-8 and existing tests.'), 1)
        self.assertEqual([c[0] for c in self.calls], [ORDER[1], ORDER[0], ORDER[0], 'gpt-6-sol'])
        self.assertEqual(self.calls[-1][2:], ('read-only', None, 'rescue'))
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['rescue_status'], 'NEEDS_HUMAN_DECISION')
        self.assertEqual(state['worker_failures'], 2)
        self.assertEqual(state['attempt_limit'], 6)  # one turn per step + four shared retry turns

    def test_prerequisite_block_stops_before_configured_full_suite(self):
        self.plan = plan('normal')
        def blocked(*args, **kwargs):
            result = self.execute(*args, **kwargs)
            if kwargs.get('sandbox', 'workspace-write') != 'read-only':
                result['message'] = json.dumps({'checkpoint': 'step-1', 'status': 'blocked',
                    'summary': 'Local runtime prerequisite is missing.', 'evidence': []})
            return result
        with patch('ai_dev.codex.execute', side_effect=blocked), patch('ai_dev.supervisor.verify') as verify:
            self.assertEqual(supervisor.task(self.root,
                'Fix bug in src/bug.py; preserve UTF-8 and existing tests.'), 1)
        verify.assert_not_called()
        state = read(self.root / '.ai-dev/state.json')
        self.assertIn('runtime prerequisite', state['checkpoint_issue'])

    def test_successful_checkpoint_still_runs_configured_verification(self):
        self.plan = plan('normal')
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch(
                'ai_dev.supervisor.verify', return_value=checked(True)) as verify:
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 0)
        verify.assert_called_once()

    def test_same_failed_strong_attempt_goes_to_medium(self):
        self.plan = plan('normal')
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch('ai_dev.supervisor.verify',
                side_effect=[checked(), checked()]):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        self.assertEqual(self.calls[-1][:2], ('gpt-6-sol', 'medium'))
        self.assertEqual(self.calls[-1][2], 'read-only')
        self.assertIsNone(self.calls[-1][3])

    def test_repeated_failure_offers_chat_after_early_stop(self):
        self.plan = plan('normal')
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch('ai_dev.supervisor.verify',
                side_effect=[checked(), checked()]):
            self.assertEqual(supervisor.task(self.root, 'Fix difficult behavior in src/bug.py'), 1)
        offer = read(self.root / '.ai-dev/state.json').get('chatgpt_offer')
        self.assertEqual(offer['status'], 'OFFERED')
        self.assertTrue(os.path.isfile(offer['path']))
        self.assertEqual(self.calls[-1][:2], ('gpt-6-sol', 'medium'))

    def test_improving_strong_attempt_can_continue_low(self):
        self.plan = plan('normal')
        self.rescue_status = 'RETRY_WITH_NEW_APPROACH'
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch('ai_dev.supervisor.verify',
                side_effect=[checked(False, 3), checked(False, 3), checked(False, 1), checked(True)]):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 0)
        self.assertEqual(self.calls[-1][:2], (ORDER[-1], 'low'))
        self.assertIsNone(self.calls[-1][3])  # controlled post-rescue retry never reuses rescue history

    def test_passing_tests_alone_do_not_complete_a_checkpoint(self):
        self.plan = plan('normal')
        self.bad_receipts = True
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch('ai_dev.supervisor.verify', return_value=checked(True)):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state.get('checkpoint_history', []), [])
        self.assertEqual(state['worker_failures'], 2)
        offer = state.get('chatgpt_offer')
        self.assertEqual(offer['status'], 'OFFERED')
        self.assertTrue(os.path.isfile(offer['path']))

    def test_resume_does_not_repeat_a_completed_checkpoint_or_plan(self):
        def interrupted(*args, **kwargs):
            result = self.execute(*args, **kwargs)
            if len(self.calls) == 3:
                result['ok'] = False
            return result
        with patch('ai_dev.codex.execute', side_effect=interrupted), patch('ai_dev.supervisor.verify', return_value=checked(True)):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
            self.assertEqual(supervisor.task(self.root), 0)
        self.assertEqual([c[0] for c in self.calls], [ORDER[1], ORDER[0], ORDER[0], ORDER[0]])
        self.assertEqual(len(read(self.root / '.ai-dev/state.json')['checkpoint_history']), 2)

    def test_exhausted_legacy_state_does_not_spend_on_planning(self):
        write(self.root / '.ai-dev/state.json', {'id': 'old', 'status': 'BLOCKED', 'created_at': 1,
              'attempts': 5, 'session_id': None, 'base_head': 'head', 'config': self.config})
        with patch('ai_dev.codex.execute') as execute:
            self.assertEqual(supervisor.task(self.root), 1)
            execute.assert_not_called()

    def test_plan_validation_and_small_task_remains_one_checkpoint(self):
        self.assertEqual(len(planning.checkpoints(planning.parse_plan(json.dumps(plan('small'))))), 1)
        self.assertEqual(len(planning.checkpoints({'objective': 'legacy', 'worker_kind': 'normal',
                                                   'steps': ['code', 'test', 'review']})), 1)
        for bad in ('../elsewhere', '/tmp/a', '.git/config', '.ai-dev/state.json', 'C:\\outside'):
            value = plan('normal'); value['steps'][0]['files'] = [bad]
            with self.subTest(path=bad), self.assertRaises(ValueError):
                planning.parse_plan(json.dumps(value))
        with self.assertRaises(ValueError):
            planning.parse_plan(json.dumps(plan(*(['normal'] * 5))))

    def test_overlong_plan_field_names_the_exact_field_and_limit_for_repair(self):
        value = plan('hard')
        value['steps'][0]['acceptance'] = 'x' * 1201
        with self.assertRaisesRegex(ValueError, r'Этап 1: acceptance содержит 1201 символов, максимум 1200'):
            planning.parse_plan(json.dumps(value))

    def test_overlong_acceptance_is_handed_off_without_strong_retry(self):
        oversized = plan('normal')
        oversized['steps'][0]['acceptance'] = 'x' * 1201
        def oversized_response(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            self.calls.append((config['model'], config['reasoning'], sandbox, session))
            contract = self.contract(prompt)
            contract['stages'][0]['acceptance'] = ['x' * 1201]
            return {'ok': True, 'session_id': 'bad-plan', 'message': json.dumps(contract)}
        with patch('ai_dev.codex.execute', side_effect=oversized_response):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        self.assertEqual(self.calls, [(ORDER[1], 'medium', 'read-only', None)])
        state = read(self.root / '.ai-dev/state.json')
        diag = state['planner_validation_history'][0]
        self.assertEqual(diag['error_class'], 'PLAN_CONTRACT_SCHEMA_ERROR')
        self.assertFalse(diag['retry_required'])
        self.assertIsNone(diag['semantic_valid'])
        self.assertEqual(state['chatgpt_plan_offer']['status'], 'OFFERED')

    def test_semantically_incomplete_plan_gets_one_bounded_planner_retry(self):
        self.plan = plan('normal')
        incomplete = plan('normal')
        incomplete['steps'][0]['acceptance'] = ''
        def repair_semantics(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            if prompt.startswith('Review only whether'):
                return test_plan_first.semantic_review_response(prompt)
            self.calls.append((config['model'], config['reasoning'], sandbox, session))
            if sandbox == 'read-only' and len([item for item in self.calls if item[2] == 'read-only']) == 1:
                contract = self.contract(prompt)
                contract['stages'][0]['acceptance'] = []
                return {'ok': True, 'session_id': 'incomplete', 'message': json.dumps(contract)}
            if sandbox == 'read-only':
                self.assertIn('stage.acceptance не должен быть пустым', prompt)
                return {'ok': True, 'session_id': 'complete', 'message': json.dumps(self.contract(prompt))}
            checkpoint = planning.checkpoints(read(root / '.ai-dev/state.json')['plan'])[0]
            return {'ok': True, 'session_id': 'worker', 'message': json.dumps({
                'checkpoint': checkpoint['id'], 'status': 'done', 'summary': 'Done.'})}
        with patch('ai_dev.codex.execute', side_effect=repair_semantics), patch(
                'ai_dev.supervisor.verify', return_value=checked(True)):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 0)
        self.assertEqual(len([item for item in self.calls if item[2] == 'read-only']), 2)
        history = read(self.root / '.ai-dev/state.json')['planner_validation_history']
        self.assertEqual(history[0]['error_class'], 'PLAN_CONTRACT_SEMANTIC_ERROR')
        self.assertTrue(history[0]['retry_required'])
        self.assertEqual(history[1]['error_class'], 'NONE')

    def test_second_semantic_failure_reports_retry_budget_exhaustion(self):
        incomplete = plan('normal')
        incomplete['steps'][0]['acceptance'] = ''
        def always_incomplete(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            if prompt.startswith('Review only whether'):
                return test_plan_first.semantic_review_response(prompt)
            self.calls.append((config['model'], config['reasoning'], sandbox, session))
            contract = self.contract(prompt)
            contract['stages'][0]['acceptance'] = []
            return {'ok': True, 'session_id': 'planner', 'message': json.dumps(contract)}
        with patch('ai_dev.codex.execute', side_effect=always_incomplete):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        self.assertEqual(len(self.calls), 2)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['last_failure'], 'planning_semantic')
        final = state['planner_validation_history'][-1]
        self.assertEqual(final['error_class'], 'PLAN_CONTRACT_SEMANTIC_ERROR')
        self.assertFalse(final['retry_required'])
        self.assertIn('бюджет планировщика исчерпан', final['retry_reason'])

    def test_unparseable_plan_stops_without_second_strong_turn(self):
        self.plan = plan('normal')
        def repair(*args, **kwargs):
            result = self.execute(*args, **kwargs)
            if len(self.calls) == 1:
                result['message'] = 'bad plan'
            return result
        with patch('ai_dev.codex.execute', side_effect=repair), patch('ai_dev.supervisor.verify', return_value=checked(True)):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        self.assertEqual(len(self.calls), 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['planner_validation_history'][0]['error_class'],
                         'SERIALIZATION_FORMAT_ERROR')

    def test_watchdog_honors_latest_success_and_large_event(self):
        log = self.root / 'events.jsonl'
        events = [{'type': 'item.completed', 'item': {'id': str(i), 'type': 'command_execution',
                   'command': 'check', 'exit_code': code, 'aggregated_output': 'x' * 100000}}
                  for i, code in enumerate((1, 1, 0))]
        log.write_text('\n'.join(map(json.dumps, events)) + '\n')
        signal = LiveSignal(self.root, log, verification_commands=[['check']])
        signal.update(os.getpid(), force=True)
        self.assertFalse(signal.stalled)
        events.pop()
        log.write_text('\n'.join(map(json.dumps, events)) + '\n')
        signal = LiveSignal(self.root, log, verification_commands=[['check']])
        with self.assertRaises(WorkerStalled):
            signal.update(os.getpid(), force=True)
        self.assertEqual(len(signal.stall_verification()['checks'][0]['tail']), 3000)

    def test_watchdog_matches_cd_only_for_its_repository(self):
        import shlex
        signal = LiveSignal(self.root, verification_commands=[['check']])
        signal.observe_check({'id': 'other', 'command': 'cd /elsewhere && check', 'exit_code': 1})
        self.assertEqual(signal.failures, {})
        command = 'cd %s && check' % shlex.quote(str(self.root))
        signal.observe_check({'id': 'a', 'command': command, 'exit_code': 1})
        with self.assertRaises(WorkerStalled):
            signal.observe_check({'id': 'b', 'command': command, 'exit_code': 1})
        signal.observe_check({'id': [], 'command': 'check', 'exit_code': 1})
