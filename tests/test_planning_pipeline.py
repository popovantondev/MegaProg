import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_dev import evidence_packet, planning, planning_pipeline
from ai_dev.storage import write


class PlanningPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.check_call(['git', 'init', '-q'], cwd=self.root)
        subprocess.check_call(['git', 'config', 'user.email', 'test@example.invalid'], cwd=self.root)
        subprocess.check_call(['git', 'config', 'user.name', 'Pipeline Test'], cwd=self.root)
        (self.root / 'example_app').mkdir()
        (self.root / 'example_app' / 'controller.py').write_text(
            'def publish_archive(history):\n    return history\n', encoding='utf-8')
        (self.root / 'example_app' / 'upload.py').write_text(
            'def send_item(client, item):\n    return client.send(item)\n', encoding='utf-8')
        (self.root / 'incidental').mkdir()
        (self.root / 'incidental' / 'radar.py').write_text(
            'def probe_feature_state(value):\n    return value\n', encoding='utf-8')
        (self.root / 'tests').mkdir()
        (self.root / 'tests' / 'test_controller.py').write_text(
            'def test_publish_order():\n    assert True\n', encoding='utf-8')
        (self.root / 'tests' / 'test_upload.py').write_text(
            'def test_send_item():\n    assert True\n', encoding='utf-8')
        (self.root / 'PLAN_2_4.md').write_text(
            '# Example project\nPublish saved history sequentially and verify recovery.\n', encoding='utf-8')
        (self.root / 'PROJECT_PLAN.md').write_text(
            '# Smart library plan\nUse the existing local E5 cache and preserve source citations.\n', encoding='utf-8')
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts' / 'windows_version_info.txt').write_text(
            'Windows package metadata\n', encoding='utf-8')
        subprocess.check_call(['git', 'add', '.'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '-qm', 'baseline'], cwd=self.root)
        task_id = 'example-old-task'
        self.goal = ('Complete saved archive publication using supported client calls; '
            'preserve Windows support. Windows build deferred. Keep source archives unchanged, '
            'send sequentially, recover safely without duplicate posts.')
        state = {'id': task_id, 'status': 'CANCELLED', 'prompt': self.goal,
            'base_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.root,
                                                  text=True).strip(),
            'routing': {'kind': 'hard', 'model': None, 'reasoning': None, 'pin_scope': 'planner'},
            'config': {'verify': [['python3', '-m', 'unittest']], 'strong_planning': True},
            'plan': {'schema': 2, 'objective': 'Sequential safe publication',
                'steps': [{'id': 'step-1', 'title': 'Repair publication and recovery',
                    'worker_kind': 'hard',
                    'files': ['example_app/controller.py', 'example_app/upload.py',
                              'tests/test_controller.py', 'tests/test_upload.py', 'PLAN_2_4.md'],
                    'acceptance': 'Ordered publishing and safe recovery pass offline tests.'}]}}
        write(self.root / '.ai-dev/state.json', state)
        write(self.root / '.ai-dev/runs' / task_id / 'state.json', state)
        memory = self.root / '.ai-dev/memory'
        memory.mkdir(parents=True)
        for name in ('decisions.jsonl', 'failures.jsonl', 'events.jsonl'):
            (memory / name).write_text('', encoding='utf-8')
        self.packet = evidence_packet.build_packet(self.root, task_id=task_id)
        self.gate = planning_pipeline.completeness_gate(self.root, self.packet)

    def make_contract(self):
        packet = self.packet
        platform = packet['platform_scope']
        files = [name for name in packet['scope']['files']
                 if name.startswith('example_app/') or name.startswith('tests/')]
        return {'schema_version': 1, 'task_id': packet['task']['task_id'],
            'packet_id': packet['packet_id'],
            'snapshot': {'head': packet['snapshot']['head'],
                         'dirty_hash': packet['snapshot']['dirty_hash']},
            'decision': {'summary': 'Use supported sequential publication with durable recovery.',
                'rationale': 'The packet links the current upload path, controller and focused tests.',
                'alternatives_rejected': ['Blindly retry ambiguous sends.']},
            'invariants': ['Never send real Telegram messages during offline verification.',
                           'Keep the original source archive unchanged.'],
            'stages': [{'stage_id': 'step-1', 'objective': 'Repair send ordering and recovery.',
                'complexity_requirement': 'hard', 'allowed_paths': files,
                'expected_paths': [], 'forbidden_paths': ['dist/Example project.app'],
                'acceptance': ['Text, media and albums remain in source order.',
                               'Ambiguous sends are reconciled without blind duplicates.'],
                'verification': ['Fake-client tests cover ordering and restart recovery.'],
                'review_triggers': ['NEW_CONCURRENCY', 'TEST_GAP'],
                'rollback': 'Revert the isolated upload and controller stage diff.'}],
            'global_acceptance': ['Configured offline unit tests pass.'],
            'review_contract': {'invariants': ['TEST_GAP'],
                'architectural_triggers': ['PUBLIC_API_CHANGED', 'SCHEMA_CHANGED'],
                'security_triggers': ['SECURITY_BOUNDARY_CHANGED'],
                'scope_triggers': ['FILES_OUTSIDE_ALLOWED_SCOPE', 'PLATFORM_CONTRACT_CHANGED']},
            'rollback': ['Revert the stage commit and restore the previous package metadata.'],
            'platform_contract': {'active_platform': platform['active_platform'],
                'shared_invariants': platform['shared_invariants'],
                'deferred_platforms': platform['deferred_platforms']}}

    def test_example_project_packet_is_bounded_and_gate_is_explicit(self):
        self.assertLessEqual(self.packet['budget']['final_bytes'], evidence_packet.DEFAULT_BUDGET)
        self.assertEqual(self.packet['budget']['model_turns_used_to_build'], 0)
        self.assertEqual(self.gate['status'], 'READY')
        self.assertTrue(self.gate['planner_may_run'])
        self.assertEqual(self.packet['platform_scope']['deferred_platforms'], ['windows'])
        self.assertEqual(self.packet['platform_scope']['shared_invariants'],
            ['Preserve existing Windows support; do not create a Windows build in this task.'])

    def test_completeness_gate_distinguishes_degraded_and_blocked(self):
        degraded = json.loads(json.dumps(self.packet))
        degraded['completeness']['status'] = 'DEGRADED'
        degraded['unknowns'] = ['Project Memory not available.']
        result = planning_pipeline.completeness_gate(self.root, degraded)
        self.assertEqual(result['status'], 'DEGRADED')
        self.assertTrue(result['planner_may_run'])
        blocked = json.loads(json.dumps(self.packet))
        blocked['task']['goal'] = ''
        result = planning_pipeline.completeness_gate(self.root, blocked)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertFalse(result['planner_may_run'])
        host_only = json.loads(json.dumps(self.packet))
        host_only['platform_scope']['explicit'] = False
        result = planning_pipeline.completeness_gate(self.root, host_only)
        self.assertEqual(result['status'], 'DEGRADED')
        self.assertTrue(result['planner_may_run'])
        self.assertFalse(result['checks']['platform_scope_explicit'])

    def test_planner_projection_is_small_and_keeps_facts_and_unknowns(self):
        rendered, size, view = planning_pipeline.render_planner_input(self.packet, self.gate)
        self.assertLessEqual(size, planning_pipeline.PLANNER_PACKET_BUDGET + 100)
        self.assertEqual(view['packet_id'], self.packet['packet_id'])
        self.assertIn('example_app/controller.py', view['scope']['files'])
        self.assertIn('windows', json.dumps(view['platform_scope']).casefold())
        self.assertIn('unknowns', view)
        self.assertTrue(rendered.startswith('EVIDENCE PACKET'))

    def test_planner_instructions_publish_the_validator_enum_and_field_contract(self):
        prompt = planning_pipeline.planner_instructions(self.packet, self.gate)
        self.assertIn('Every review trigger must be an exact enum', prompt)
        for trigger in sorted(planning_pipeline.REVIEW_TRIGGERS):
            self.assertIn(trigger, prompt)
        self.assertIn('complexity_requirement must be exactly routine, engineering, or hard', prompt)
        self.assertIn('each invariant ≤600', prompt)
        self.assertIn('trigger IDs are exact enum strings ≤100', prompt)
        for field in ('invariants', 'review_triggers', 'allowed_paths', 'expected_paths',
                      'forbidden_paths', 'verification', 'rollback', 'platform_contract'):
            self.assertIn('"%s"' % field, prompt)

    def test_plan_contract_fixture_is_validated_and_mapped_to_worker_plan(self):
        canonical = self.make_contract()
        canonical['review_contract']['invariants'] = [
            'Preserve source archives and existing library behavior.']
        canonical['review_contract']['scope_triggers'].append('TEST_GAP')
        source = json.dumps(canonical)
        response = planning_pipeline.parse_response(source,
            self.packet['task']['task_id'], self.packet)
        self.assertEqual(response['kind'], 'plan')
        self.assertEqual(response['normalization_actions'], [])
        self.assertEqual(response['parse_status'], 'ACCEPTED')
        self.assertEqual(response['contract']['schema_version'], 1)
        self.assertEqual(response['plan']['schema'], 2)
        self.assertEqual(response['plan']['steps'][0]['worker_kind'], 'hard')
        self.assertEqual(response['plan']['steps'][0]['contract']['review_triggers'],
                         ['NEW_CONCURRENCY', 'TEST_GAP'])
        self.assertEqual(response['contract']['review_contract']['invariants'],
                         ['Preserve source archives and existing library behavior.'])
        self.assertIn('TEST_GAP', response['contract']['review_contract']['scope_triggers'])
        self.assertEqual(planning_pipeline.contract_staleness(self.root,
            response['contract'], self.packet)['status'], 'VALID')

    def test_ambiguous_multiple_plan_objects_fail_closed(self):
        source = json.dumps(self.make_contract()) + '\n' + json.dumps({'schema_version': 1})
        with self.assertRaises(planning.PlanParseError) as raised:
            planning_pipeline.parse_response(source, self.packet['task']['task_id'], self.packet)
        self.assertEqual(raised.exception.diagnostics['error_class'], 'PLAN_CONTRACT_AMBIGUOUS')
        self.assertFalse(raised.exception.diagnostics['retry_required'])

    def test_unknown_plancontract_field_is_reported_not_silently_dropped(self):
        contract = self.make_contract()
        contract['future_meaning'] = {'architecture': 'must remain visible'}
        with self.assertRaises(planning.PlanParseError) as raised:
            planning_pipeline.parse_response(json.dumps(contract),
                self.packet['task']['task_id'], self.packet)
        self.assertEqual(raised.exception.diagnostics['error_class'], 'PLAN_CONTRACT_SCHEMA_ERROR')
        self.assertIn('future_meaning', str(raised.exception))

    def test_malformed_enum_shapes_are_classified_without_type_errors(self):
        contract = self.make_contract()
        contract['stages'][0]['complexity_requirement'] = ['hard']
        with self.assertRaises(planning.PlanParseError) as raised:
            planning_pipeline.parse_response(json.dumps(contract),
                self.packet['task']['task_id'], self.packet)
        self.assertEqual(raised.exception.diagnostics['error_class'], 'PLAN_CONTRACT_SCHEMA_ERROR')

    def test_exact_known_review_enum_in_compatible_category_is_normalized(self):
        contract = self.make_contract()
        contract['review_contract']['invariants'] = ['TEST_GAP']
        response = planning_pipeline.parse_response(json.dumps(contract),
            self.packet['task']['task_id'], self.packet)
        self.assertIn('moved_known_review_trigger_to_scope_triggers', response['normalization_actions'])
        self.assertIn('TEST_GAP', response['contract']['review_contract']['scope_triggers'])
        self.assertNotIn('TEST_GAP', response['contract']['review_contract']['invariants'])

    def test_ai_dev_forbidden_path_is_valid_without_packet_evidence(self):
        contract = self.make_contract()
        contract['stages'][0]['forbidden_paths'] = ['.ai-dev/']
        response = planning_pipeline.parse_response(json.dumps(contract),
            self.packet['task']['task_id'], self.packet, root=self.root)
        self.assertEqual(response['kind'], 'plan')
        self.assertEqual(response['contract']['stages'][0]['contract']['forbidden_paths'], ['.ai-dev/'])

    def test_unsafe_forbidden_path_is_rejected_deterministically(self):
        contract = self.make_contract()
        contract['stages'][0]['forbidden_paths'] = ['../outside.py']
        with self.assertRaises(planning_pipeline.ContractPathInvalid) as raised:
            planning_pipeline.parse_response(json.dumps(contract),
                self.packet['task']['task_id'], self.packet, root=self.root)
        self.assertEqual(raised.exception.diagnostics['error_class'], 'CONTRACT_PATH_INVALID')

    def test_path_already_in_packet_does_not_trigger_closure(self):
        contract = self.make_contract()
        contract['stages'][0]['allowed_paths'].append('PROJECT_PLAN.md')
        packet = evidence_packet.build_packet(self.root, task_id=self.packet['task']['task_id'],
            goal=self.goal + ' Read PROJECT_PLAN.md.')
        self.assertIn('PROJECT_PLAN.md', packet['scope']['files'])
        contract['packet_id'] = packet['packet_id']
        contract['snapshot'] = {'head': packet['snapshot']['head'],
                                'dirty_hash': packet['snapshot']['dirty_hash']}
        response = planning_pipeline.parse_response(json.dumps(contract),
            packet['task']['task_id'], packet, root=self.root)
        self.assertEqual(response['kind'], 'plan')

    def test_saved_example_path_failure_is_classified_as_closure_not_semantic_error(self):
        contract = self.make_contract()
        contract['stages'][0]['forbidden_paths'] = ['.ai-dev/']
        contract['stages'][0]['allowed_paths'].append('PROJECT_PLAN.md')
        with self.assertRaises(planning_pipeline.ContractPathClosureRequired) as raised:
            planning_pipeline.parse_response(json.dumps(contract),
                self.packet['task']['task_id'], self.packet, root=self.root)
        self.assertEqual(raised.exception.diagnostics['error_class'], 'CONTRACT_PATH_NEEDS_EVIDENCE')
        self.assertNotEqual(raised.exception.diagnostics['error_class'], 'SEMANTIC_PLAN_ERROR')

    def test_existing_allowed_path_outside_packet_requests_one_closure(self):
        contract = self.make_contract()
        contract['stages'][0]['allowed_paths'].append('PROJECT_PLAN.md')
        with self.assertRaises(planning_pipeline.ContractPathClosureRequired) as raised:
            planning_pipeline.parse_response(json.dumps(contract),
                self.packet['task']['task_id'], self.packet, root=self.root)
        self.assertEqual(raised.exception.paths, ['PROJECT_PLAN.md'])
        self.assertEqual(raised.exception.diagnostics['parse_status'], 'PATH_CLOSURE_REQUIRED')
        self.assertFalse(raised.exception.diagnostics['retry_required'])

    def test_existing_allowed_directory_outside_packet_requests_closure(self):
        contract = self.make_contract()
        contract['stages'][0]['allowed_paths'].append('tests')
        packet = json.loads(json.dumps(self.packet))
        packet['scope']['files'] = [path for path in packet['scope']['files'] if not path.startswith('tests/')]
        packet['scope']['test_fragments'] = []
        with self.assertRaises(planning_pipeline.ContractPathClosureRequired) as raised:
            planning_pipeline.parse_response(json.dumps(contract),
                self.packet['task']['task_id'], packet, root=self.root)
        self.assertEqual(raised.exception.paths, [
            'tests/test_controller.py', 'tests/test_upload.py', 'tests'])

    def test_missing_unexpected_allowed_path_fails_closed_without_model_retry(self):
        contract = self.make_contract()
        contract['stages'][0]['allowed_paths'].append('missing/unplanned.py')
        with self.assertRaises(planning_pipeline.ContractPathInvalid) as raised:
            planning_pipeline.parse_response(json.dumps(contract),
                self.packet['task']['task_id'], self.packet, root=self.root)
        self.assertEqual(raised.exception.diagnostics['error_class'], 'CONTRACT_PATH_INVALID')
        self.assertFalse(raised.exception.diagnostics['retry_required'])

    def test_expected_new_file_is_not_requested_for_closure(self):
        contract = self.make_contract()
        contract['stages'][0]['expected_paths'] = ['example_app/new_importer.py']
        response = planning_pipeline.parse_response(json.dumps(contract),
            self.packet['task']['task_id'], self.packet, root=self.root)
        self.assertEqual(response['kind'], 'plan')

    def test_plan_contract_cannot_activate_deferred_windows_implementation(self):
        contract = self.make_contract()
        contract['stages'][0]['allowed_paths'].append('scripts/windows_version_info.txt')
        with self.assertRaises(planning.PlanParseError):
            planning_pipeline.parse_response(json.dumps(contract), self.packet['task']['task_id'], self.packet)

    def test_plan_contract_cannot_rewrite_snapshot_or_task_identity(self):
        contract = self.make_contract()
        contract['snapshot']['head'] = 'other'
        with self.assertRaises(planning.PlanParseError):
            planning_pipeline.parse_response(json.dumps(contract), self.packet['task']['task_id'], self.packet)

    def test_allowed_path_must_be_evidenced_or_declared_expected(self):
        contract = self.make_contract()
        contract['stages'][0]['allowed_paths'].append('secretly/unrelated.py')
        with self.assertRaises(planning.PlanParseError):
            planning_pipeline.parse_response(json.dumps(contract), self.packet['task']['task_id'], self.packet)

    def test_needs_evidence_request_is_narrow_and_not_a_plan_retry(self):
        response = planning_pipeline.parse_response(json.dumps({'status': 'NEEDS_EVIDENCE',
            'requests': [{'kind': 'path', 'query': 'example_app/upload.py',
                          'reason': 'Confirm the current Telethon send call.'}]}),
            self.packet['task']['task_id'], self.packet)
        self.assertEqual(response['kind'], 'needs_evidence')
        self.assertEqual(response['requests'][0]['query'], 'example_app/upload.py')
        self.assertEqual(response['request_count'], 1)
        self.assertEqual(response['valid_count'], 1)
        self.assertEqual(response['rejected_count'], 0)

    def test_needs_evidence_accepts_two_separate_path_requests(self):
        response = planning_pipeline.parse_response(json.dumps({'status': 'NEEDS_EVIDENCE',
            'requests': [{'kind': 'path', 'query': 'TASK.md', 'reason': 'Read task requirements.'},
                         {'kind': 'path', 'query': 'PROJECT_PLAN.md', 'reason': 'Read approved plan.'}]}),
            self.packet['task']['task_id'], self.packet)
        self.assertEqual([row['query'] for row in response['requests']],
                         ['TASK.md', 'PROJECT_PLAN.md'])
        self.assertEqual(response['valid_count'], 2)

    def test_saved_example_planner_response_splits_only_its_explicit_path_pair(self):
        response_text = json.dumps({'status': 'NEEDS_EVIDENCE', 'requests': [
            {'kind': 'path',
             'query': 'TASK.md and PROJECT_PLAN.md: sections specifying local E5 model, existing full search cache locations, and LIVE validation constraints',
             'reason': 'The packet lacks project requirements.'}]})
        response = planning_pipeline.parse_response(response_text,
            self.packet['task']['task_id'], self.packet)
        self.assertEqual([row['query'] for row in response['requests']],
                         ['TASK.md', 'PROJECT_PLAN.md'])
        self.assertEqual(response['normalization_actions'], ['split_explicit_two_path_request'])
        self.assertEqual(response['rejected_count'], 0)

    def test_one_invalid_request_does_not_discard_valid_batch_or_become_format_error(self):
        response = planning_pipeline.parse_response(json.dumps({'status': 'NEEDS_EVIDENCE',
            'requests': [
                {'kind': 'symbol', 'query': 'CompactFullSearch', 'reason': 'Find the search API.'},
                {'kind': 'test', 'query': 'test_full_search_cache.py', 'reason': 'Find focused tests.'},
                {'kind': 'path', 'query': 'TASK.md', 'reason': 'Read task constraints.'},
                {'kind': 'path', 'query': '../outside.py', 'reason': 'Unsafe traversal probe.'}]}),
            self.packet['task']['task_id'], self.packet)
        self.assertEqual(response['kind'], 'needs_evidence')
        self.assertEqual(len(response['requests']), 3)
        self.assertEqual(response['request_count'], 4)
        self.assertEqual(response['valid_count'], 3)
        self.assertEqual(response['rejected_count'], 1)
        self.assertEqual(response['error_class'], 'EVIDENCE_REQUEST_VALIDATION_ERROR')
        self.assertEqual(response['rejected'][0]['index'], 3)

    def test_unsafe_and_ambiguous_path_requests_are_rejected_safely(self):
        values = [('../outside.py', 'EVIDENCE_REQUEST_VALIDATION_ERROR'),
                  ('/etc/passwd', 'EVIDENCE_REQUEST_VALIDATION_ERROR'),
                  ('src/a.py; rm -rf /', 'EVIDENCE_REQUEST_VALIDATION_ERROR'),
                  ('Please inspect src/a.py for the relevant function', 'EVIDENCE_REQUEST_AMBIGUOUS')]
        for query, error_class in values:
            with self.subTest(query=query):
                response = planning_pipeline.parse_response(json.dumps({'status': 'NEEDS_EVIDENCE',
                    'requests': [{'kind': 'path', 'query': query, 'reason': 'Inspect a path.'}]}),
                    self.packet['task']['task_id'], self.packet)
                self.assertEqual(response['kind'], 'needs_evidence')
                self.assertEqual(response['requests'], [])
                self.assertEqual(response['rejected_count'], 1)
                self.assertEqual(response['error_class'], error_class)

    def test_needs_evidence_can_stop_with_zero_valid_requests_without_plan_parse_error(self):
        response = planning_pipeline.parse_response(json.dumps({'status': 'NEEDS_EVIDENCE',
            'requests': [{'kind': 'path', 'query': 'files maybe in a place', 'reason': 'Find files.'}]}),
            self.packet['task']['task_id'], self.packet)
        self.assertEqual(response['kind'], 'needs_evidence')
        self.assertEqual(response['requests'], [])
        self.assertEqual(response['error_class'], 'EVIDENCE_REQUEST_AMBIGUOUS')

    def test_reentry_counts_only_new_scoped_evidence(self):
        extra = evidence_packet.build_packet(self.root, task_id=self.packet['task']['task_id'],
            extra_requests=[{'kind': 'path', 'query': 'incidental/radar.py',
                             'reason': 'Need the send call signature.'}])
        self.assertTrue(planning_pipeline.added_evidence(self.packet, extra))
        self.assertNotEqual(extra['packet_id'], self.packet['packet_id'])
        self.assertIn('incidental/radar.py', extra['scope']['files'])

    def test_reentry_does_not_count_its_own_lifecycle_events_as_evidence(self):
        before = dict(self.packet)
        before['events'] = [{'id': 'created', 'type': 'task_created'}]
        after = dict(self.packet)
        after['events'] = before['events'] + [
            {'id': 'started', 'type': 'stage_started'},
            {'id': 'updated', 'type': 'task_state_updated'}]
        self.assertFalse(planning_pipeline.added_evidence(before, after))

    def test_planning_freshness_ignores_runtime_state_but_detects_task_change(self):
        contract = planning_pipeline.parse_response(json.dumps(self.make_contract()),
            self.packet['task']['task_id'], self.packet)['contract']
        state_path = self.root / '.ai-dev/state.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        state['status'] = 'RUNNING'
        write(state_path, state)
        self.assertEqual(planning_pipeline.contract_staleness(self.root, contract, self.packet)['status'], 'VALID')
        state['prompt'] += ' changed'
        write(state_path, state)
        self.assertEqual(planning_pipeline.contract_staleness(self.root, contract, self.packet)['status'], 'STALE')

    def test_resume_accepts_only_in_scope_worker_overlay(self):
        contract = planning_pipeline.parse_response(json.dumps(self.make_contract()),
            self.packet['task']['task_id'], self.packet)['contract']
        path = self.root / 'example_app' / 'controller.py'
        path.write_text(path.read_text(encoding='utf-8') + '\n# partial worker output\n', encoding='utf-8')
        self.assertEqual(planning_pipeline.contract_staleness(self.root, contract, self.packet,
            allow_planned_changes=True)['status'], 'VALID')
        (self.root / 'unrelated.py').write_text('outside scope\n', encoding='utf-8')
        self.assertEqual(planning_pipeline.contract_staleness(self.root, contract, self.packet,
            allow_planned_changes=True)['status'], 'STALE')


if __name__ == '__main__':
    unittest.main()
