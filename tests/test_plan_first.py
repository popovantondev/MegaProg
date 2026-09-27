import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
import io
from argparse import Namespace
from unittest.mock import patch
import test_supervisor
from test_routing import catalog
from ai_dev import evidence_packet, planning, supervisor, coordinator_context, test_recommendation
from ai_dev import cli
from ai_dev.routing import ORDER
from ai_dev.storage import read, write
from ai_dev.live import LiveSignal
from ai_dev.process import WorkerStalled
from ai_dev.process import run
from ai_dev.report import build_report
from ai_dev.usage_report import aggregate


def verification_pass():
    return {'ok': True, 'checks': [{'command': ['python3', '-m', 'unittest'],
        'exit_code': 0, 'tail': 'Ran 1 test in 0.01s\n\nOK'}]}


def verification_fail():
    return {'ok': False, 'checks': [{'command': ['python3', '-m', 'unittest'],
        'exit_code': 1, 'tail': 'FAILED (failures=1)'}]}


def semantic_review_response(prompt):
    packet = json.loads(prompt.split('REVIEW PACKET:\n', 1)[1])
    acceptance = packet['contract']['acceptance']
    refs = packet['available_evidence_refs']
    rows = []
    for index, criterion in enumerate(acceptance):
        ref = 'contract:acceptance:%d' % index
        if ref not in refs:
            ref = next((item for item in refs if item.startswith('verification:')), ref)
        rows.append({'criterion': criterion, 'status': 'PASS', 'evidence_refs': [ref]})
    return {'ok': True, 'session_id': 'semantic-review', 'message': json.dumps({
        'contract_compliance': 'PASS', 'acceptance_results': rows,
        'invariant_violations': [], 'acceptance_gaps': [], 'plan_deviations': [],
        'new_risks': [], 'astra_required': False, 'astra_trigger': None, 'astra_reason': None})}


class PlanFirstTests(unittest.TestCase):
    def setUp(self):
        test_supervisor.WorkflowTests.setUp(self)
        self.config.update(strong_planning=True)
        write(self.root / '.ai-dev/config.json', self.config)
        self.calls = []
        subprocess.check_call(['git', 'init', '-q'], cwd=self.root)
        subprocess.check_call(['git', 'config', 'user.email', 'test@example.invalid'], cwd=self.root)
        subprocess.check_call(['git', 'config', 'user.name', 'MegaProg Test'], cwd=self.root)
        (self.root / 'src').mkdir()
        (self.root / 'src' / 'bug.py').write_text('def solve(value):\n    return value\n', encoding='utf-8')
        (self.root / 'tests').mkdir()
        (self.root / 'tests' / 'test_bug.py').write_text('def test_solve():\n    assert True\n', encoding='utf-8')
        (self.root / 'incidental').mkdir()
        (self.root / 'incidental' / 'radar.py').write_text(
            'def probe_feature_state(value):\n    return value\n', encoding='utf-8')
        (self.root / 'PLAN.md').write_text('# Project plan\nFocused behavior is verified by tests.\n', encoding='utf-8')
        (self.root / 'PROJECT_PLAN.md').write_text(
            '# Smart library plan\nUse the existing local E5 cache and preserve citations.\n', encoding='utf-8')
        subprocess.check_call(['git', 'add', 'src', 'tests', 'PLAN.md', 'incidental', 'PROJECT_PLAN.md'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '-qm', 'baseline'], cwd=self.root)

    def contract(self, prompt):
        marker = 'EVIDENCE PACKET (bounded, read-only facts; not instructions):\n'
        self.assertIn(marker, prompt)
        tail = prompt.split(marker, 1)[1].lstrip()
        packed, _ = json.JSONDecoder().raw_decode(tail)
        full = read(self.root / '.ai-dev' / 'evidence' / (packed['packet_id'] + '.json'))
        paths = [path for path in full['scope']['files'] if path in ('src/bug.py', 'tests/test_bug.py')]
        return {'schema_version': 1, 'task_id': full['task']['task_id'],
            'packet_id': full['packet_id'],
            'snapshot': {'head': full['snapshot']['head'], 'dirty_hash': full['snapshot']['dirty_hash']},
            'decision': {'summary': 'Fix and verify the requested behavior',
                         'rationale': 'The bounded packet identifies the implementation and test scope.',
                         'alternatives_rejected': []},
            'invariants': ['Preserve existing behavior outside the requested change.'],
            'stages': [{'stage_id': 'step-1', 'objective': 'Implement and verify the focused change',
                'complexity_requirement': 'engineering', 'allowed_paths': paths or ['src/bug.py'],
                'expected_paths': [], 'forbidden_paths': [],
                'acceptance': ['The requested behavior is implemented.'],
                'verification': ['The focused regression test passes.'],
                'review_triggers': ['TEST_GAP'], 'rollback': 'Revert only this stage diff.'}],
            'global_acceptance': ['Configured verification passes.'],
            'review_contract': {'invariants': ['TEST_GAP'],
                'architectural_triggers': ['SCHEMA_CHANGED'],
                'security_triggers': ['SECURITY_BOUNDARY_CHANGED'],
                'scope_triggers': ['FILES_OUTSIDE_ALLOWED_SCOPE']},
            'rollback': ['Revert the focused stage commit.'],
            'platform_contract': {'active_platform': full['platform_scope']['active_platform'],
                'shared_invariants': full['platform_scope']['shared_invariants'],
                'deferred_platforms': full['platform_scope']['deferred_platforms']}}

    def execute(self, root, config, report, prompt, log, session=None, sandbox='workspace-write'):
        if prompt.startswith('Review only whether'):
            return semantic_review_response(prompt)
        if prompt.startswith('Ты выполняешь роль rescue-reviewer'):
            self.calls.append((config['model'], config['reasoning'], 'read-only', session, 'rescue'))
            return {'ok': True, 'session_id': 'rescue', 'usage': {'input_tokens': 10, 'output_tokens': 2},
                'message': json.dumps({'status': 'NEEDS_HUMAN_DECISION',
                    'root_cause_hypothesis': 'Repeated fixture failure', 'new_evidence_required': [],
                    'new_approach': {'summary': '', 'allowed_scope_change': []},
                    'plan_contract_change_required': False, 'reason': 'Test fixture requires manual review.'})}
        self.calls.append((config['model'], config['reasoning'], sandbox, session))
        result = {'ok': True, 'session_id': 'planner' if sandbox == 'read-only' else 'worker',
                  'usage': {'input_tokens': 10, 'output_tokens': 2}}
        if sandbox == 'read-only':
            self.assertNotIn('Implement the task below', prompt)
            result['message'] = json.dumps(self.contract(prompt))
        else:
            self.assertIn('Strong planner proposal', prompt)
            state = read(self.root / '.ai-dev/state.json')
            step = state.get('plan', {}).get('steps', [{}])[state.get('checkpoint_index', 0)]
            result['message'] = json.dumps({'checkpoint': step.get('id', 'step-1'),
                'status': 'done', 'summary': 'Focused behavior and configured checks verified.'})
        return result

    def test_plan_worker_repeated_failure_stops_and_uses_fresh_read_only_rescue(self):
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch(
                'ai_dev.supervisor.verify', side_effect=[verification_fail(), verification_fail()]):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        self.assertEqual([x[:2] for x in self.calls], [
            (ORDER[1], 'medium'), (ORDER[0], 'medium'), (ORDER[0], 'medium'), ('gpt-6-sol', 'medium')])
        self.assertEqual(self.calls[0][2:], ('read-only', None))
        self.assertEqual(self.calls[1][3], None)  # planner history never enters worker
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['attempts'], 2)
        self.assertEqual(state['worker_failures'], 2)
        self.assertEqual(state['attempt_limit'], 5)  # legacy config=2 reserves recovery
        self.assertEqual(state['rescue_status'], 'NEEDS_HUMAN_DECISION')
        self.assertTrue(state['anti_loop_telemetry']['circuit_breaker_fired'])
        self.assertTrue((self.root / state['rescue_packet']['path']).is_file())
        self.assertEqual(state['rescue_turns'][0]['telemetry']['role'], 'rescue')
        self.assertEqual(state['rescue_turns'][0]['telemetry']['session_id'], 'rescue')
        self.assertEqual(aggregate(self.root)['totals']['input_tokens'], 40)
        planner_turn = state['planning_attempts'][0]['codex']['telemetry']
        worker_turn = state['attempts_detail'][0]['codex']['telemetry']
        self.assertEqual((planner_turn['task_id'], planner_turn['role'],
                          planner_turn['stage_id']), (state['id'], 'planner', 'planning'))
        self.assertEqual((planner_turn['configured_model'], planner_turn['configured_reasoning']),
                         (ORDER[1], 'medium'))
        self.assertEqual(worker_turn['role'], 'worker')
        self.assertEqual(worker_turn['stage_id'], 'step-1')
        self.assertEqual(worker_turn['observed_model'], 'UNKNOWN')

    def test_plan_cli_reports_routing_without_starting_a_model_turn(self):
        selected = {'planner': {'model': 'gpt-6-astra', 'reasoning': 'low'},
                    'worker_estimate': {'model': 'gpt-6-luna', 'reasoning': 'low'},
                    'model_turns_started': 0}
        output = io.StringIO()
        with patch('ai_dev.cli.get_catalog', return_value=({'ready': True}, catalog())), \
             patch('ai_dev.cli.decision', return_value=selected) as decide, \
             patch('ai_dev.supervisor.task') as task, redirect_stdout(output):
            self.assertEqual(cli.main(['-C', str(self.root), 'plan', 'Fix shadowed route']), 0)
        decide.assert_called_once()
        task.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['model_turns_started'], 0)

    def test_plan_preview_honors_explicit_astra_medium(self):
        preview = cli.decision(self.root, catalog(), 'Fix bug',
                               Namespace(model=ORDER[-1], reasoning='medium', kind='normal', pin_scope=None))
        self.assertEqual((preview['planner']['model'], preview['planner']['reasoning']),
                         (ORDER[-1], 'medium'))
        self.assertEqual(preview['worker_estimate']['model'], ORDER[0])
        self.assertEqual(preview['pin_scope'], 'planner')
        self.assertEqual(preview['model_turns_started'], 0)

    def test_markdown_fenced_plan_is_normalized_without_second_planner_turn(self):
        calls = []
        def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            if prompt.startswith('Review only whether'):
                return semantic_review_response(prompt)
            calls.append(sandbox)
            if sandbox == 'read-only':
                return {'ok': True, 'session_id': 'planner',
                        'message': '```json\n' + json.dumps(self.contract(prompt)) + '\n```'}
            return {'ok': True, 'session_id': 'worker', 'message': json.dumps({
                'checkpoint': 'step-1', 'status': 'done', 'summary': 'Focused test passes.'})}
        with patch('ai_dev.codex.execute', side_effect=execute), \
             patch('ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 0)
        self.assertEqual(calls, ['read-only', 'workspace-write'])
        state = read(self.root / '.ai-dev/state.json')
        validation = state['planner_validation_history'][0]
        self.assertEqual(validation['parse_status'], 'REPAIRED')
        self.assertEqual(validation['error_class'], 'NONE')
        self.assertTrue(validation['schema_valid'])
        self.assertTrue(validation['semantic_valid'])
        self.assertFalse(validation['retry_required'])
        telemetry = state['planning_telemetry']
        self.assertEqual(telemetry['planner_response_status'], 'REPAIRED')
        self.assertEqual(telemetry['planner_parse_error_class'], 'NONE')
        self.assertTrue(telemetry['normalization_repaired'])
        self.assertEqual(telemetry['planner_turns'], 1)
        self.assertIsNone(telemetry['plan_contract_validation_error'])

    def test_plancontract_enum_failure_is_precise_and_does_not_spend_second_planner_turn(self):
        def invalid_enum_execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            if sandbox == 'read-only':
                self.calls.append((config['model'], config['reasoning'], sandbox, session))
                contract = self.contract(prompt)
                contract['stages'][0]['review_triggers'] = [
                    'Any proposed code edit, test failure, or discrepancy between cached and canonical evidence.']
                return {'ok': True, 'session_id': 'planner', 'message': json.dumps(contract),
                        'usage': {'input_tokens': 10, 'output_tokens': 2}}
            return self.execute(root, config, report, prompt, log, session, sandbox)
        with patch('ai_dev.codex.execute', side_effect=invalid_enum_execute):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['status'], 'BLOCKED')
        self.assertEqual(len(self.calls), 1)
        validation = state['planner_validation_history'][0]
        self.assertEqual(validation['error_class'], 'PLAN_CONTRACT_ENUM_ERROR')
        self.assertIn('Unknown review trigger', state['planning_telemetry']['plan_contract_validation_error'])
        self.assertEqual(state['planning_telemetry']['planner_parse_error_class'],
                         'PLAN_CONTRACT_ENUM_ERROR')
        self.assertFalse(state['planning_telemetry']['plan_contract_schema_valid'])
        self.assertFalse(state['planning_telemetry']['normalization_repaired'])
        self.assertFalse(validation['retry_required'])
        self.assertEqual(state['chatgpt_plan_offer']['reason'], 'planner_format')
        self.assertIn('PLAN_CONTRACT_ENUM_ERROR', state['reason'])

    def test_m1_m9_offline_pipeline_smoke_with_coordinator_capsule(self):
        self.config['verify'] = [['python3', '-m', 'unittest', 'tests.test_bug']]
        write(self.root / '.ai-dev/config.json', self.config)
        capsule = coordinator_context.build_capsule(self.root, component='src')
        self.assertEqual(capsule['schema_version'], 1)
        coordinator_text = coordinator_context.render_capsule(capsule)
        self.assertIn('PROJECT', coordinator_text)
        def smoke_execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            result = self.execute(root, config, report, prompt, log, session, sandbox)
            if sandbox == 'workspace-write':
                (root / 'src' / 'bug.py').write_text(
                    'def solve(value):\n    return value + 1\n', encoding='utf-8')
                (root / 'tests' / 'test_bug.py').write_text(
                    'from src.bug import solve\n\ndef test_solve():\n    assert solve(1) == 2\n',
                    encoding='utf-8')
            return result
        with patch('ai_dev.codex.execute', side_effect=smoke_execute), \
             patch('ai_dev.supervisor.run', return_value=(0, 'Ran 1 test in 0.01s\n\nOK')):
            result = supervisor.task(self.root,
                'Fix bug in src/bug.py and update tests/test_bug.py. Use this compact coordinator state when relevant:\n' + coordinator_text)
        smoke_state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(result, 0, {'reason': smoke_state.get('reason'),
            'checkpoint_issue': smoke_state.get('checkpoint_issue'),
            'review': smoke_state.get('review_cascade')})
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['routing']['pin_scope'], 'planner')
        self.assertTrue(state['planning_evidence']['packet_path'].startswith('.ai-dev/evidence/'))
        self.assertEqual(state['plan_contract']['task_id'], state['id'])
        self.assertTrue(state['attempt_history'])
        self.assertIn(state['review_cascade'][-1]['status'], ('PASS', 'PASS_NO_MODEL_REVIEW'))
        self.assertFalse(any(item.get('model') == 'gpt-6-astra'
                             for item in state.get('review_turns', [])))
        self.assertIn('model_wall_time_ms', state['wall_time'])
        self.assertTrue(state['phase_telemetry'])
        report = build_report(self.root, state)
        phases = {item['phase'] for item in report['phase_telemetry']}
        self.assertTrue({'context_load', 'evidence_build', 'completeness_check',
                         'planning', 'planning_model_turn', 'worker_execution',
                         'review', 'targeted_verification', 'total'} <= phases)
        self.assertIn(report['review_cascade'][-1]['status'], ('PASS', 'PASS_NO_MODEL_REVIEW'))
        self.assertTrue((self.root / '.ai-dev/memory/events.jsonl').is_file())
        suggestion = test_recommendation.recommend(self.root, ['src/bug.py'])
        self.assertEqual(suggestion['candidate_tests'], ['tests/test_bug.py'])
        self.assertFalse(suggestion['automatic_execution'])

    def test_planner_gets_one_bounded_evidence_reentry(self):
        request = {'status': 'NEEDS_EVIDENCE', 'requests': [{
            'kind': 'path', 'query': 'incidental/radar.py',
            'reason': 'Check whether the existing feature probe can be reused.'}]}
        calls = []
        def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            if prompt.startswith('Review only whether'):
                return semantic_review_response(prompt)
            calls.append(sandbox)
            if sandbox == 'read-only':
                if len([value for value in calls if value == 'read-only']) == 1:
                    return {'ok': True, 'session_id': 'planner-1', 'message': json.dumps(request)}
                return {'ok': True, 'session_id': 'planner-2', 'message': json.dumps(self.contract(prompt))}
            state = read(self.root / '.ai-dev/state.json')
            step = state['plan']['steps'][state.get('checkpoint_index', 0)]
            return {'ok': True, 'session_id': 'worker', 'message': json.dumps({
                'checkpoint': step['id'], 'status': 'done', 'summary': 'Focused check passed.'})}
        with patch('ai_dev.codex.execute', side_effect=execute), \
             patch('ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 0)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(calls, ['read-only', 'read-only', 'workspace-write'])
        self.assertEqual(state['extra_evidence_rounds'], 1)
        self.assertTrue(state['planning_telemetry']['planner_requested_more_evidence'])
        self.assertEqual(state['planning_telemetry']['planner_turns'], 2)
        self.assertEqual(state['planning_telemetry']['evidence_request_count'], 1)
        self.assertEqual(state['planning_telemetry']['evidence_requests_valid'], 1)
        self.assertEqual(state['planning_telemetry']['evidence_requests_rejected'], 0)
        self.assertTrue(state['planning_telemetry']['evidence_reentry_attempted'])
        self.assertTrue(state['planning_telemetry']['evidence_reentry_added_evidence'])
        self.assertEqual(state['planning_telemetry']['extra_evidence_rounds'], 1)
        packet = evidence_packet.load_packet(self.root / state['planning_evidence']['packet_path'])
        self.assertIn('incidental/radar.py', packet['scope']['files'])

    def test_example_style_contract_path_closure_reenters_once_and_accepts(self):
        planner_calls = []
        def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            if prompt.startswith('Review only whether'):
                return semantic_review_response(prompt)
            if sandbox == 'read-only':
                planner_calls.append(prompt)
                contract = self.contract(prompt)
                contract['stages'][0]['forbidden_paths'] = ['.ai-dev/']
                contract['stages'][0]['allowed_paths'].append('PROJECT_PLAN.md')
                return {'ok': True, 'session_id': 'planner-%d' % len(planner_calls),
                        'usage': {'input_tokens': 10, 'output_tokens': 2},
                        'message': json.dumps(contract)}
            return {'ok': True, 'session_id': 'worker', 'usage': {'input_tokens': 10, 'output_tokens': 2},
                'message': json.dumps({'checkpoint': 'step-1', 'status': 'done',
                                       'summary': 'Focused validation passed.'})}
        with patch('ai_dev.codex.execute', side_effect=execute), \
             patch('ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 0)
        state = read(self.root / '.ai-dev/state.json')
        telemetry = state['planning_telemetry']
        self.assertEqual(len(planner_calls), 2)
        self.assertNotEqual(state['planning_attempts'][0]['planning_evidence']['packet_id'],
                            state['planning_attempts'][1]['planning_evidence']['packet_id'])
        self.assertEqual(state['planning_attempts'][1]['planner_reentry_reason'], 'CONTRACT_PATH_CLOSURE')
        self.assertEqual(telemetry['contract_path_requests'], 1)
        self.assertEqual(telemetry['contract_path_valid'], 1)
        self.assertEqual(telemetry['contract_path_rejected'], 0)
        self.assertTrue(telemetry['contract_path_evidence_added'])
        self.assertEqual(telemetry['contract_path_reentry'], 1)
        self.assertEqual(telemetry['planner_semantic_correction_turns'], 0)
        self.assertEqual(telemetry['planner_turns'], 2)
        packet = evidence_packet.load_packet(self.root / state['planning_evidence']['packet_path'])
        self.assertIn('PROJECT_PLAN.md', packet['scope']['files'])
        self.assertEqual(state['plan_contract']['stages'][0]['contract']['forbidden_paths'], ['.ai-dev/'])

    def test_semantic_correction_and_path_closure_share_one_reentry_budget(self):
        planner_calls = []
        def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            if sandbox != 'read-only':
                self.fail('A blocked plan must not reach the worker.')
            planner_calls.append(prompt)
            if len(planner_calls) == 1:
                invalid = self.contract(prompt)
                invalid['decision']['summary'] = ''
                return {'ok': True, 'session_id': 'planner-1', 'message': json.dumps(invalid)}
            contract = self.contract(prompt)
            contract['stages'][0]['allowed_paths'].append('PROJECT_PLAN.md')
            return {'ok': True, 'session_id': 'planner-2', 'message': json.dumps(contract)}
        with patch('ai_dev.codex.execute', side_effect=execute):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(len(planner_calls), 2)
        self.assertEqual(state['last_failure'], 'planning_gate')
        self.assertEqual(state['evidence_reentry']['status'], 'LIMIT_REACHED')
        telemetry = state['planning_telemetry']
        self.assertEqual(telemetry['planner_turns'], 2)
        self.assertEqual(telemetry['planner_semantic_correction_turns'], 1)
        self.assertEqual(telemetry['contract_path_requests'], 1)
        self.assertEqual(telemetry['contract_path_reentry'], 0)

    def test_invalid_contract_after_path_reentry_stops_without_third_turn(self):
        calls = []
        def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            if prompt.startswith('Review only whether'):
                return semantic_review_response(prompt)
            if sandbox == 'read-only':
                calls.append(prompt)
                contract = self.contract(prompt)
                if len(calls) == 1:
                    contract['stages'][0]['allowed_paths'].append('PROJECT_PLAN.md')
                else:
                    contract['stages'][0]['allowed_paths'].append('missing/typo.py')
                return {'ok': True, 'session_id': 'planner-%d' % len(calls), 'message': json.dumps(contract)}
            self.fail('Invalid PlanContract must not reach the worker.')
        with patch('ai_dev.codex.execute', side_effect=execute):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(len(calls), 2)
        self.assertEqual(state['last_failure'], 'planning_gate')
        self.assertEqual(state['evidence_reentry']['status'], 'CONTRACT_PATH_INVALID')
        self.assertEqual(state['planning_telemetry']['planner_turns'], 2)

    def test_existing_contract_directory_uses_bounded_local_child_evidence(self):
        calls = []
        def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            if prompt.startswith('Review only whether'):
                return semantic_review_response(prompt)
            if sandbox == 'read-only':
                calls.append(prompt)
                contract = self.contract(prompt)
                contract['stages'][0]['allowed_paths'].append('incidental')
                contract['stages'][0]['forbidden_paths'] = ['.ai-dev/']
                return {'ok': True, 'session_id': 'planner-%d' % len(calls), 'message': json.dumps(contract)}
            return {'ok': True, 'session_id': 'worker', 'message': json.dumps({
                'checkpoint': 'step-1', 'status': 'done', 'summary': 'Focused validation passed.'})}
        with patch('ai_dev.codex.execute', side_effect=execute), \
             patch('ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 0)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(len(calls), 2)
        telemetry = state['planning_telemetry']
        self.assertEqual(telemetry['contract_path_requests'], 1)
        self.assertEqual(telemetry['contract_path_evidence_added'], True)
        self.assertEqual(telemetry['contract_path_reentry'], 1)
        packet = evidence_packet.load_packet(self.root / state['planning_evidence']['packet_path'])
        self.assertIn('incidental/radar.py', packet['scope']['files'])

    def test_partial_evidence_batch_keeps_valid_lookup_and_records_rejected_path(self):
        request = {'status': 'NEEDS_EVIDENCE', 'requests': [
            {'kind': 'path', 'query': 'incidental/radar.py',
             'reason': 'Read the existing local feature probe.'},
            {'kind': 'path', 'query': '../outside.py',
             'reason': 'This request is deliberately unsafe.'}]}
        calls = []
        def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            calls.append(sandbox)
            if prompt.startswith('Review only whether'):
                return semantic_review_response(prompt)
            if sandbox == 'read-only':
                if len([value for value in calls if value == 'read-only']) == 1:
                    return {'ok': True, 'session_id': 'planner-1', 'message': json.dumps(request)}
                return {'ok': True, 'session_id': 'planner-2', 'message': json.dumps(self.contract(prompt))}
            state = read(self.root / '.ai-dev/state.json')
            step = state['plan']['steps'][state.get('checkpoint_index', 0)]
            return {'ok': True, 'session_id': 'worker', 'message': json.dumps({
                'checkpoint': step['id'], 'status': 'done', 'summary': 'Focused validation completed.'})}
        with patch('ai_dev.codex.execute', side_effect=execute), \
             patch('ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 0)
        state = read(self.root / '.ai-dev/state.json')
        telemetry = state['planning_telemetry']
        self.assertEqual(calls, ['read-only', 'read-only', 'workspace-write', 'read-only'])
        self.assertTrue(telemetry['planner_requested_more_evidence'])
        self.assertEqual(telemetry['evidence_request_count'], 2)
        self.assertEqual(telemetry['evidence_requests_valid'], 1)
        self.assertEqual(telemetry['evidence_requests_rejected'], 1)
        self.assertEqual(telemetry['evidence_request_error_class'], 'EVIDENCE_REQUEST_VALIDATION_ERROR')
        self.assertTrue(telemetry['evidence_reentry_attempted'])
        self.assertTrue(telemetry['evidence_reentry_added_evidence'])
        self.assertEqual(state['planner_validation_history'][0]['parse_status'], 'ACCEPTED_REQUEST')
        self.assertEqual(state['planner_validation_history'][0]['error_class'],
                         'EVIDENCE_REQUEST_VALIDATION_ERROR')
        self.assertEqual(state['evidence_reentry']['status'], 'NEW_EVIDENCE_FOUND')
        self.assertEqual(len(state['evidence_reentry']['rejected_requests']), 1)

    def test_planner_stops_when_requested_evidence_does_not_exist(self):
        calls = []
        def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            calls.append(sandbox)
            return {'ok': True, 'session_id': 'planner', 'message': json.dumps({
                'status': 'NEEDS_EVIDENCE', 'requests': [{'kind': 'path',
                'query': 'incidental/not-found.py', 'reason': 'The requested implementation detail is absent.'}]})}
        with patch('ai_dev.codex.execute', side_effect=execute):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(calls, ['read-only'])
        self.assertEqual(state['last_failure'], 'planning_gate')
        self.assertEqual(state['evidence_reentry']['status'], 'NO_NEW_EVIDENCE')
        self.assertEqual(state['evidence_reentry']['error_class'], 'EVIDENCE_NOT_FOUND')
        self.assertTrue(state['planning_telemetry']['planner_requested_more_evidence'])
        self.assertTrue(state['planning_telemetry']['evidence_reentry_attempted'])
        self.assertFalse(state['planning_telemetry']['evidence_reentry_added_evidence'])
        self.assertEqual(state['planning_telemetry']['planner_turns'], 1)

    def test_planner_cannot_loop_after_one_evidence_reentry(self):
        request = {'status': 'NEEDS_EVIDENCE', 'requests': [{
            'kind': 'path', 'query': 'incidental/radar.py',
            'reason': 'Check whether the existing feature probe can be reused.'}]}
        calls = []
        def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            calls.append(sandbox)
            return {'ok': True, 'session_id': 'planner', 'message': json.dumps(request)}
        with patch('ai_dev.codex.execute', side_effect=execute):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(calls, ['read-only', 'read-only'])
        self.assertEqual(len(state['planning_attempts']), 2)
        self.assertEqual(state['evidence_reentry']['status'], 'LIMIT_REACHED')
        self.assertEqual(state['last_failure'], 'planning_gate')

    def test_plan_preview_can_pin_only_worker(self):
        preview = cli.decision(self.root, catalog(), 'Fix bug',
                               Namespace(model=ORDER[-1], reasoning='medium', kind='normal',
                                         pin_scope='worker'))
        self.assertEqual((preview['planner']['model'], preview['planner']['reasoning']),
                         (ORDER[1], 'medium'))
        self.assertEqual((preview['worker_estimate']['model'], preview['worker_estimate']['reasoning']),
                         (ORDER[-1], 'medium'))

    def test_task_cli_passes_pin_scope(self):
        with patch('ai_dev.supervisor.task', return_value=0) as task, \
             patch('ai_dev.self_repair.maybe_start'), \
             patch('ai_dev.native_monitor.auto_open'):
            self.assertEqual(cli.main(['-C', str(self.root), 'task', 'Fix bug',
                '--model', ORDER[-1], '--reasoning', 'medium', '--pin-scope', 'planner']), 0)
        task.assert_called_once_with(self.root, 'Fix bug', 'auto', ORDER[-1], 'medium', None, 'planner')

    def test_plan_survives_resume_and_network_failure_does_not_escalate(self):
        def execute(*args, **kwargs):
            result = self.execute(*args, **kwargs)
            if len(self.calls) == 2:
                result['ok'] = False
            return result
        with patch('ai_dev.codex.execute', side_effect=execute), patch('ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
            self.assertEqual(supervisor.task(self.root), 0)
        self.assertEqual([x[0] for x in self.calls], [ORDER[1], ORDER[0], ORDER[0]])

    def test_stalled_turn_goes_directly_to_strong(self):
        def execute(*args, **kwargs):
            result = self.execute(*args, **kwargs)
            if len(self.calls) == 2:
                result.update(ok=False, stalled=True)
            return result
        with patch('ai_dev.codex.execute', side_effect=execute), patch('ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 0)
        self.assertEqual([x[0] for x in self.calls], [ORDER[1], ORDER[0], ORDER[-1]])

    def test_strong_fallback_and_unavailable(self):
        self.assertEqual(planning.strong(catalog(ORDER[:-1]))['model'], ORDER[-2])
        with self.assertRaises(ValueError):
            planning.strong(catalog(ORDER[:1]))

    def test_discovery_failure_is_not_reported_as_model_unavailability(self):
        broken = catalog()
        broken['errors'] = {'models': 'Codex app-server завершился: initialize'}
        with self.assertRaisesRegex(ValueError, 'сбой discovery'):
            planning.strong(broken)

    def test_explicit_planner_pin_does_not_leak_to_worker_or_resume(self):
        def execute(*args, **kwargs):
            result = self.execute(*args, **kwargs)
            if len(self.calls) == 2:
                result['ok'] = False
            return result
        with patch('ai_dev.codex.execute', side_effect=execute), patch('ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug', kind='normal',
                                            model=ORDER[-1], reasoning='medium'), 1)
            self.assertEqual(supervisor.task(self.root), 0)
        self.assertEqual([x[:2] for x in self.calls], [
            (ORDER[-1], 'medium'), (ORDER[0], 'medium'), (ORDER[0], 'medium')])

    def test_task_pin_explicitly_applies_to_planner_and_worker(self):
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch(
                'ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug', kind='normal',
                model=ORDER[-1], reasoning='medium', pin_scope='task'), 0)
        self.assertEqual([x[:2] for x in self.calls], [
            (ORDER[-1], 'medium'), (ORDER[-1], 'medium')])
        report = json.loads((self.root / '.ai-dev/runs' /
                             read(self.root / '.ai-dev/state.json')['id'] / 'report.json').read_text())
        self.assertEqual(report['pin_scope'], 'task')

    def test_worker_scope_keeps_strong_default_planner_and_pins_only_worker(self):
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch(
                'ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug', kind='normal',
                model=ORDER[-1], reasoning='medium', pin_scope='worker'), 0)
        self.assertEqual([x[:2] for x in self.calls], [
            (ORDER[1], 'medium'), (ORDER[-1], 'medium')])

    def test_task_scope_persists_across_resume(self):
        def execute(*args, **kwargs):
            result = self.execute(*args, **kwargs)
            if len(self.calls) == 2:
                result['ok'] = False
            return result
        with patch('ai_dev.codex.execute', side_effect=execute), patch(
                'ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug', kind='normal',
                model=ORDER[-1], reasoning='medium', pin_scope='task'), 1)
            self.assertEqual(supervisor.task(self.root), 0)
        self.assertEqual([x[:2] for x in self.calls], [
            (ORDER[-1], 'medium'), (ORDER[-1], 'medium'), (ORDER[-1], 'medium')])

    def test_config_pinned_astra_reasoning_is_honored(self):
        self.config.update(model=ORDER[-1], reasoning='medium')
        write(self.root / '.ai-dev/config.json', self.config)
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch('ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 0)
        self.assertEqual(self.calls[0][:2], (ORDER[-1], 'medium'))
        self.assertEqual(self.calls[1][:2], (ORDER[0], 'medium'))

    def test_unsupported_pinned_model_stops_before_planner(self):
        with patch('ai_dev.codex.execute') as execute:
            self.assertEqual(supervisor.task(self.root, 'Fix bug', model='missing-model'), 1)
        execute.assert_not_called()
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['attempts'], 0)
        self.assertNotIn('planning_attempts', state)
        self.assertIn('разрешены только GPT-6', state['reason'])

    def test_astra_cap_blocks_before_planner(self):
        with patch('ai_dev.codex.execute') as execute:
            self.assertEqual(supervisor.task(self.root, 'Fix bug', model=ORDER[-1], reasoning='high'), 1)
        execute.assert_not_called()
        self.assertIn('только low/medium', read(self.root / '.ai-dev/state.json')['reason'])

    def test_pinned_quota_exhaustion_stops_before_planner(self):
        c = catalog()
        c['limits'] = {'rateLimitsByLimitId': {'worker': {
            'model': ORDER[1], 'spendControlReached': True}}}
        with patch('ai_dev.supervisor.discover', return_value=c), patch('ai_dev.codex.execute') as execute:
            self.assertEqual(supervisor.task(self.root, 'Fix bug', model=ORDER[1]), 1)
        execute.assert_not_called()
        self.assertIn('исчерпание квоты', read(self.root / '.ai-dev/state.json')['reason'])

    def test_strong_reports_real_quota_cause(self):
        c = catalog()
        c['limits'] = {'rateLimits': {'spendControlReached': True}}
        with self.assertRaisesRegex(ValueError, 'подтверждено исчерпание квоты'):
            planning.strong(c)

    def test_internal_exception_is_saved_for_self_repair_without_second_model(self):
        with patch.dict(os.environ, {'MEGAPROG_SELF_REPAIR_CHILD': '0'}), \
             patch('ai_dev.codex.execute', side_effect=TypeError('broken adapter')) as execute, \
             patch('ai_dev.self_repair.enqueue', return_value='PENDING') as enqueue:
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['status'], 'BLOCKED')
        self.assertEqual(state['self_health']['kind'], 'internal')
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(enqueue.call_count, 1)

        with patch.dict(os.environ, {'MEGAPROG_SELF_REPAIR_CHILD': '0'}), \
             patch('ai_dev.codex.doctor', return_value={'ready': False}), \
             patch('ai_dev.self_repair.enqueue') as later_enqueue:
            self.assertEqual(supervisor.task(self.root), 1)
        self.assertEqual(read(self.root / '.ai-dev/state.json')['self_health']['kind'], 'infrastructure')
        later_enqueue.assert_not_called()

    def test_invalid_plan_never_starts_worker_and_is_reported(self):
        with patch('ai_dev.codex.execute', return_value={'ok': True, 'message': 'bad', 'usage': {'input_tokens': 8}}) as execute:
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
            self.assertEqual(execute.call_count, 1)  # malformed serialization gets no expensive retry
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['attempts'], 0)
        self.assertEqual(state['last_failure'], 'planning_format')
        validation = state['planner_validation_history'][0]
        self.assertEqual(validation['error_class'], 'SERIALIZATION_FORMAT_ERROR')
        self.assertFalse(validation['retry_required'])
        self.assertTrue(validation['raw_artifact'].endswith('/plan-1/codex.jsonl'))
        offer = state['chatgpt_plan_offer']
        self.assertEqual(offer['recommended_mode'], 'normal')
        self.assertIn('обычный ChatGPT', open(offer['path'], encoding='utf-8').read())
        self.assertEqual(build_report(self.root, state)['attempts'][0]['codex']['usage']['input_tokens'], 8)
        self.assertEqual(build_report(self.root, state)['attempts'][0]['planner_validation']['error_class'],
                         'SERIALIZATION_FORMAT_ERROR')

    def test_resume_after_unrecoverable_format_error_does_not_retry_planner(self):
        with patch('ai_dev.codex.execute', return_value={'ok': True, 'message': 'bad'}) as execute:
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
            self.assertEqual(supervisor.task(self.root), 1)
            self.assertEqual(execute.call_count, 1)

    def test_external_plan_import_reuses_task_without_third_planner_turn(self):
        with patch('ai_dev.codex.execute', return_value={'ok': True, 'message': 'bad'}) as execute:
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
            self.assertEqual(execute.call_count, 1)
        state = read(self.root / '.ai-dev/state.json')
        response = self.root / 'external-plan.json'
        response.write_text(json.dumps({'task_id': state['id'], 'base_head': state['base_head'],
            'plan': {'schema': 2, 'objective': 'Fix bug', 'steps': [{
                'title': 'Fix and verify', 'worker_kind': 'normal',
                'files': ['src/app.py'], 'acceptance': 'Focused test passes.'}]}}, ensure_ascii=False),
            encoding='utf-8')
        with patch('ai_dev.plan_handoff.subprocess.check_output', side_effect=['head\n', '']):
            result = cli.main(['-C', str(self.root), 'plan-import', str(response)])
        self.assertEqual(result, 0)
        imported = read(self.root / '.ai-dev/state.json')
        self.assertEqual(imported['plan_source'], 'ordinary_chatgpt_manual')
        self.assertEqual(imported['status'], 'PREFLIGHT')
        response.unlink()
        with patch('ai_dev.codex.execute', return_value={'ok': True, 'session_id': 'worker',
                'message': json.dumps({'checkpoint': 'step-1', 'status': 'done', 'summary': 'Test passes.'})}) as execute, \
             patch('ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root), 0)
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(read(self.root / '.ai-dev/state.json')['status'], 'COMPLETED')

    def test_external_plan_import_rejects_stale_task_and_dirty_tree(self):
        with patch('ai_dev.codex.execute', return_value={'ok': True, 'message': 'bad'}):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        state = read(self.root / '.ai-dev/state.json')
        response = self.root / 'external-plan.json'
        plan = {'schema': 2, 'objective': 'Fix bug', 'steps': [{
            'title': 'Fix and verify', 'worker_kind': 'normal',
            'files': ['src/app.py'], 'acceptance': 'Focused test passes.'}]}
        response.write_text(json.dumps({'task_id': 'other-task',
            'base_head': state['base_head'], 'plan': plan}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'другой задаче'):
            from ai_dev.plan_handoff import import_plan
            import_plan(self.root, response)
        response.write_text(json.dumps({'task_id': state['id'],
            'base_head': state['base_head'], 'plan': plan}), encoding='utf-8')
        with patch('ai_dev.plan_handoff.subprocess.check_output', side_effect=['head\n', '?? changed.py\n']):
            with self.assertRaisesRegex(ValueError, 'рабочая папка'):
                import_plan(self.root, response)
        self.assertNotIn('plan', read(self.root / '.ai-dev/state.json'))

    def test_actual_check_events_trigger_only_two_configured_failures(self):
        signal = LiveSignal(self.root, verification_commands=[['pytest', '-q']])
        signal.observe_check({'id': 'a', 'command': 'rg missing', 'exit_code': 1})
        signal.observe_check({'id': 'b', 'command': 'pytest -q', 'exit_code': 1, 'aggregated_output': 'permission denied'})
        signal.observe_check({'id': 'c', 'command': 'pytest -q', 'exit_code': 1})
        signal.observe_check({'id': 'c', 'command': 'pytest -q', 'exit_code': 1})
        with self.assertRaises(WorkerStalled):
            signal.observe_check({'id': 'd', 'command': "/bin/zsh -lc 'pytest -q'", 'exit_code': 1})
        self.assertTrue(signal.stalled)

    def test_live_subprocess_is_stopped_after_second_check_failure(self):
        log = self.root / 'events.jsonl'
        signal = LiveSignal(self.root, log, verification_commands=[['pytest', '-q']])
        events = [{'type': 'item.completed', 'item': {'id': str(i), 'type': 'command_execution',
                  'command': 'pytest -q', 'exit_code': 1}} for i in range(2)]
        script = ('import time; print(%r, flush=True); time.sleep(20)' %
                  '\n'.join(json.dumps(e) for e in events))
        with self.assertRaises(WorkerStalled):
            run([sys.executable, '-c', script], self.root, timeout=5, output_path=log, live=signal)
        self.assertTrue(signal.stalled)

    def test_planner_guard_block_has_no_invented_usage(self):
        from ai_dev.account_guard import AccountGuardError
        self.config['account_guard']['wait_seconds'] = 0
        write(self.root / '.ai-dev/config.json', self.config)
        with patch('ai_dev.supervisor.model_turn', side_effect=AccountGuardError('busy', {'reason': 'no_free_tier_slot'})), patch('ai_dev.codex.execute') as execute:
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
            execute.assert_not_called()
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['last_failure'], 'account_guard')
        self.assertEqual(build_report(self.root, state)['attempts'], [])

    def test_repeated_failure_breaker_blocks_early_and_resume_does_not_spend(self):
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch('ai_dev.supervisor.verify', return_value=verification_fail()):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
            count = len(self.calls)
            self.assertEqual(supervisor.task(self.root), 1)
            self.assertEqual(len(self.calls), count)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['anti_loop_telemetry']['final_failure_status'], 'NEEDS_HUMAN_DECISION')
        self.assertEqual(state['attempts'], 2)

    def test_human_acknowledged_resume_starts_new_generation_and_preserves_old_history(self):
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch(
                'ai_dev.supervisor.verify', side_effect=[verification_fail(), verification_fail()]):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch(
                'ai_dev.supervisor.verify', return_value=verification_pass()):
            self.assertEqual(supervisor.task(self.root, allow_no_progress_resume=True), 0)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(len(state['anti_loop_history']), 1)
        self.assertEqual(len(state['attempt_history']), 3)
        self.assertEqual(state['attempt_history'][-1]['generation'], 1)
        self.assertEqual(state['attempt_history'][-1]['role'], 'worker')

    def test_missing_dependency_stops_without_spending_rescue_turn(self):
        missing = {'ok': False, 'checks': [{'command': ['python3', '-m', 'unittest'],
            'exit_code': 1, 'tail': "ModuleNotFoundError: No module named 'telethon'\nFAILED (errors=12)"}]}
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch(
                'ai_dev.supervisor.verify', side_effect=[missing, missing]):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['attempts'], 2)
        self.assertEqual(state.get('rescue_turns', []), [])
        self.assertEqual(state['rescue_status'], 'NEEDS_HUMAN_DECISION')
        self.assertEqual(state['anti_loop_telemetry']['rescue_model'], None)
        self.assertEqual(state['anti_loop_telemetry']['rescue_turns'], 0)

    def test_watchdog_failures_are_recorded_and_cannot_bypass_breaker(self):
        def stalled(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            result = self.execute(root, config, report, prompt, log, session, sandbox)
            if sandbox == 'workspace-write':
                result.update(ok=False, stalled=True,
                    stall_verification=verification_fail())
            return result
        with patch('ai_dev.codex.execute', side_effect=stalled):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(len(state['attempt_history']), 2)
        self.assertTrue(state['anti_loop_telemetry']['circuit_breaker_fired'])
        self.assertEqual(state['attempts'], 2)

    def test_new_task_does_not_inherit_recovery_model(self):
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch(
                'ai_dev.supervisor.verify', side_effect=[verification_fail(), verification_pass(), verification_pass()]):
            self.assertEqual(supervisor.task(self.root, 'First bug'), 0)
            self.assertEqual(supervisor.task(self.root, 'Second bug'), 0)
        self.assertEqual([x[0] for x in self.calls][-2:], [ORDER[1], ORDER[0]])

    def test_verify_permission_failure_blocks_without_expensive_retry(self):
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch(
                'ai_dev.supervisor.verify', return_value={'ok': False, 'checks': [
                    {'exit_code': 1, 'tail': 'sandbox-exec: Operation not permitted'}]}):
            self.assertEqual(supervisor.task(self.root, 'Fix bug'), 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(read(self.root / '.ai-dev/state.json').get('worker_failures', 0), 0)

    def test_orchestration_pipeline_approval_covers_bounded_recovery(self):
        from ai_dev.orchestration import create_plan, create_task, approve
        plan = create_plan(self.root, 'Fix bug', ['calc.py'], [['python3', '-m', 'unittest']])
        task = create_task(self.root, plan['plan_id'], 'Fix bug')
        with patch('ai_dev.codex.execute', side_effect=self.execute), patch(
                'ai_dev.supervisor.verify', side_effect=[verification_fail(), verification_fail(), verification_pass()]):
            self.assertEqual(supervisor.task(self.root, 'Fix bug', orchestration_task_id=task['task_id']), 1)
            self.assertEqual(self.calls, [])
            approve(self.root, 'model_turn', 'owner', task_id=task['task_id'])
            self.assertEqual(supervisor.task(self.root), 1)
        self.assertEqual(self.calls[-1][0], ORDER[1])
