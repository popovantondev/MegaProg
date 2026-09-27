import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_dev import review_cascade


class ReviewCascadeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.check_call(['git', 'init', '-q'], cwd=self.root)
        subprocess.check_call(['git', 'config', 'user.email', 'review@example.invalid'], cwd=self.root)
        subprocess.check_call(['git', 'config', 'user.name', 'Review Test'], cwd=self.root)
        (self.root / 'src').mkdir()
        (self.root / 'src' / 'app.py').write_text('def run():\n    return 1\n', encoding='utf-8')
        (self.root / 'tests').mkdir()
        (self.root / 'tests' / 'test_app.py').write_text('def test_run():\n    assert True\n', encoding='utf-8')
        subprocess.check_call(['git', 'add', '.'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '-qm', 'baseline'], cwd=self.root)
        self.head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.root, text=True).strip()
        self.stage = {'id': 'step-1', 'title': 'Update behavior', 'contract': {
            'allowed_paths': ['src/app.py', 'tests/test_app.py'], 'expected_paths': [],
            'forbidden_paths': ['dist'], 'acceptance': ['Configured verification passes.'],
            'verification': ['Configured verification passes.'], 'review_triggers': [],
            'rollback': 'Revert this stage.'}}
        self.contract = {'contract_id': 'contract-1', 'packet_id': 'evidence-1',
            'snapshot': {'head': self.head, 'dirty_hash': 'base'},
            'decision': {'summary': 'Keep behavior compatible.'},
            'invariants': ['Do not access network.'], 'stages': [self.stage],
            'platform_contract': {'active_platform': 'macos', 'deferred_platforms': ['windows']},
            'review_contract': {'invariants': [], 'architectural_triggers': [],
                'security_triggers': [], 'scope_triggers': []}}
        self.state = {'id': 'task-1', 'plan_contract': self.contract,
                      'planning_evidence': {'packet_id': 'evidence-1'}}
        self.pass_verify = {'ok': True, 'checks': [{'command': ['python3', '-m', 'unittest', 'tests.test_app'],
            'exit_code': 0, 'tail': 'Ran 1 test in 0.1s\n\nOK'}]}

    def build(self, verification=None):
        return review_cascade.build_review_packet(self.root, self.state, self.stage,
            verification or self.pass_verify, packet={'packet_id': 'evidence-1',
                'snapshot': {'head': self.head, 'dirty_hash': 'base'}})

    def test_clean_contract_diff_has_no_model_review(self):
        (self.root / 'src' / 'app.py').write_text('def run():\n    return 2\n', encoding='utf-8')
        packet = self.build()
        self.assertEqual(packet['deterministic_checks']['mechanical_status'], 'PASS_NO_MODEL_REVIEW')
        self.assertEqual(review_cascade.result(packet)['status'], 'PASS_NO_MODEL_REVIEW')
        self.assertLessEqual(packet['metrics']['review_packet_bytes'], review_cascade.REVIEW_PACKET_BUDGET)

    def test_trivial_clean_diff_consumes_no_review_turns(self):
        packet = self.build()
        result = review_cascade.result(packet, turns=[])
        self.assertEqual(result['status'], 'PASS_NO_MODEL_REVIEW')
        self.assertEqual(result['telemetry']['semantic_review_turns'], 0)
        self.assertEqual(result['telemetry']['astra_review_turns'], 0)

    def test_forbidden_path_is_deterministic_fix_required(self):
        (self.root / 'dist').mkdir()
        (self.root / 'dist' / 'app.bin').write_bytes(b'generated')
        packet = self.build()
        self.assertEqual(packet['deterministic_checks']['mechanical_status'], 'FIX_REQUIRED')
        self.assertIn('dist/app.bin', packet['implementation']['forbidden_paths_changed'])

    def test_outside_scope_path_triggers_without_a_model(self):
        (self.root / 'notes.md').write_text('unexpected\n', encoding='utf-8')
        packet = self.build()
        self.assertIn('FILES_OUTSIDE_ALLOWED_SCOPE', [x['trigger'] for x in packet['triggered_reviews']])
        self.assertEqual(review_cascade.deterministic_status(packet), 'FIX_REQUIRED')

    def test_missing_test_verification_is_test_gap(self):
        (self.root / 'src' / 'app.py').write_text('def run():\n    return 2\n', encoding='utf-8')
        verify = {'ok': True, 'checks': [{'command': ['python3', 'compileall', 'src'], 'exit_code': 0, 'tail': 'OK'}]}
        packet = self.build(verify)
        self.assertIn('TEST_GAP', [x['trigger'] for x in packet['triggered_reviews']])
        self.assertEqual(review_cascade.deterministic_status(packet), 'FIX_REQUIRED')

    def test_failing_test_never_passes(self):
        packet = self.build({'ok': False, 'checks': [{'command': ['pytest'], 'exit_code': 1, 'tail': '1 failed'}]})
        self.assertEqual(review_cascade.deterministic_status(packet), 'FIX_REQUIRED')

    def test_stale_contract_blocks(self):
        subprocess.check_call(['git', 'config', 'user.name', 'Review Test'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '--allow-empty', '-qm', 'new head'], cwd=self.root)
        packet = self.build()
        self.assertEqual(review_cascade.deterministic_status(packet), 'BLOCKED')

    def test_deferred_windows_change_is_platform_trigger(self):
        path = self.root / 'scripts' / 'windows'
        path.mkdir(parents=True)
        (path / 'build.ps1').write_text('Write-Output ok\n', encoding='utf-8')
        self.stage['contract']['allowed_paths'].append('scripts/windows/build.ps1')
        packet = self.build()
        self.assertIn('PLATFORM_CONTRACT_CHANGED', [x['trigger'] for x in packet['triggered_reviews']])

    def test_missing_expected_path_is_deterministic_failure(self):
        self.stage['contract']['expected_paths'] = ['src/generated.py']
        packet = self.build()
        self.assertEqual(packet['implementation']['missing_expected_paths'], ['src/generated.py'])
        self.assertEqual(review_cascade.deterministic_status(packet), 'FIX_REQUIRED')

    def test_high_cost_path_changes_are_triggers(self):
        cases = [('src/api.py', 'PUBLIC_API_CHANGED'), ('src/schema/model.py', 'SCHEMA_CHANGED'),
                 ('src/auth/session.py', 'SECURITY_BOUNDARY_CHANGED')]
        for name, trigger in cases:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('value = 2\n', encoding='utf-8')
            self.stage['contract']['allowed_paths'].append(name)
            packet = self.build()
            self.assertIn(trigger, [x['trigger'] for x in packet['triggered_reviews']])
            self.assertIn(trigger, review_cascade.HIGH_COST_TRIGGERS)
            path.unlink()

    def test_unrelated_source_change_does_not_trigger_astra(self):
        (self.root / 'src' / 'app.py').write_text('def run():\n    return 2\n', encoding='utf-8')
        packet = self.build()
        self.assertFalse(set(x['trigger'] for x in packet['triggered_reviews']) &
                         review_cascade.HIGH_COST_TRIGGERS)

    def test_acceptance_matrix_uses_only_explicit_evidence(self):
        packet = self.build()
        matrix = packet['deterministic_checks']['acceptance_matrix']
        self.assertEqual(matrix[0]['status'], 'PASS')
        self.stage['contract']['acceptance'] = ['Albums preserve message order.']
        packet = self.build()
        self.assertEqual(packet['deterministic_checks']['acceptance_matrix'][0]['status'], 'UNKNOWN')

    def test_worker_claim_without_artifact_reference_stays_unknown(self):
        self.stage['contract']['acceptance'] = ['Source provenance is verified.']
        evidence = [{'evidence_id': 'claim-1', 'kind': 'provenance',
            'criterion': 'Source provenance is verified.', 'claim': 'The source matches.',
            'source_ref': 'source:S1', 'artifact_ref': '../missing.txt', 'verification': 'PASS'}]
        packet = review_cascade.build_review_packet(self.root, self.state, self.stage,
            self.pass_verify, packet={'packet_id': 'evidence-1', 'snapshot': {
                'head': self.head, 'dirty_hash': 'base'}}, worker_evidence=evidence)
        self.assertEqual(packet['worker_evidence'], [])
        row = packet['deterministic_checks']['acceptance_matrix'][0]
        self.assertEqual(row['status'], 'UNKNOWN')
        self.assertEqual(row['evidence'], [])

    def test_structured_evidence_with_existing_artifact_is_packet_bound_but_unverified(self):
        artifact = self.root / 'source.txt'
        artifact.write_text('verified source bytes', encoding='utf-8')
        self.stage['contract']['acceptance'] = ['Source provenance is verified.']
        evidence = [{'evidence_id': 'prov-1', 'kind': 'provenance',
            'criterion': 'Source provenance is verified.', 'claim': 'Source S1 maps to source.txt.',
            'source_ref': 'source:S1', 'artifact_ref': 'source.txt', 'verification': 'PASS'}]
        packet = review_cascade.build_review_packet(self.root, self.state, self.stage,
            self.pass_verify, packet={'packet_id': 'evidence-1', 'snapshot': {
                'head': self.head, 'dirty_hash': 'base'}}, worker_evidence=evidence)
        ref = 'worker-evidence:prov-1'
        self.assertIn(ref, packet['available_evidence_refs'])
        self.assertEqual(packet['worker_evidence'][0]['status'], 'UNVERIFIED')
        row = packet['deterministic_checks']['acceptance_matrix'][0]
        self.assertEqual(row['evidence'], [ref])
        self.assertEqual(row['status'], 'UNKNOWN')

    def test_invalid_or_nonexistent_artifact_refs_are_rejected(self):
        self.stage['contract']['acceptance'] = ['Criterion']
        for path in ('missing.txt', '../escape.txt', '/etc/passwd'):
            packet = review_cascade.build_review_packet(self.root, self.state, self.stage,
                self.pass_verify, packet={'packet_id': 'evidence-1', 'snapshot': {
                    'head': self.head, 'dirty_hash': 'base'}}, worker_evidence=[{
                    'evidence_id': 'bad-ref', 'kind': 'provenance', 'criterion': 'Criterion',
                    'claim': 'Claim', 'source_ref': 'source:S1', 'artifact_ref': path,
                    'verification': 'PASS'}])
            self.assertEqual(packet['worker_evidence'], [])

    def test_worker_narrative_is_not_structured_evidence(self):
        packet = self.build()
        self.assertEqual(packet['worker_evidence'], [])
        self.assertNotIn('A worker said PASS', json.dumps(packet))

    def test_offline_review_keeps_unsupported_example_provenance_unknown(self):
        self.stage['contract']['acceptance'] = ['Provenance path is proven.']
        packet = self.build()
        matrix = packet['deterministic_checks']['acceptance_matrix']
        self.assertEqual(matrix[0]['status'], 'UNKNOWN')
        outcome = review_cascade.result(packet, turns=[])
        self.assertEqual(outcome['status'], 'UNKNOWN')
        self.assertEqual(outcome['telemetry']['semantic_review_turns'], 0)

    def test_packet_is_bounded_and_does_not_copy_raw_logs(self):
        (self.root / 'src' / 'app.py').write_text('token = "secret-value"\n' + ('x = 1\n' * 1500), encoding='utf-8')
        packet = self.build()
        encoded = json.dumps(packet, ensure_ascii=False).encode('utf-8')
        self.assertLessEqual(packet['metrics']['review_packet_bytes'], review_cascade.REVIEW_PACKET_BUDGET)
        self.assertNotIn(b'secret-value', encoded)
        self.assertNotIn('full_raw_log', encoded.decode('utf-8'))

    def test_result_schema_and_telemetry_cover_no_model_review(self):
        packet = self.build()
        result = review_cascade.result(packet)
        required = {'review_id', 'review_packet_bytes', 'deterministic_review_status',
            'deterministic_triggers', 'semantic_review_used', 'semantic_review_model',
            'semantic_review_reasoning', 'semantic_review_turns', 'astra_review_used',
            'astra_review_turns', 'astra_trigger', 'acceptance_pass', 'acceptance_fail',
            'acceptance_unknown', 'review_result'}
        self.assertTrue(required.issubset(result['telemetry']))

    def test_semantic_output_requires_valid_acceptance_refs(self):
        response = {'contract_compliance': 'PASS', 'acceptance_results': [
            {'criterion': 'Configured verification passes.', 'status': 'PASS',
             'evidence_refs': ['verification:' + review_cascade._sha(
                 json.dumps(self.pass_verify['checks'][0]['command'], ensure_ascii=False,
                            separators=(',', ':')))[:16]]}],
            'invariant_violations': [], 'acceptance_gaps': [], 'plan_deviations': [],
            'new_risks': [], 'astra_required': False, 'astra_trigger': None, 'astra_reason': None}
        parsed = review_cascade.parse_semantic_result(json.dumps(response))
        packet = self.build()
        review_cascade.validate_acceptance_results(parsed, packet)
        response['acceptance_results'][0]['evidence_refs'] = ['made-up']
        with self.assertRaises(ValueError):
            review_cascade.validate_acceptance_results(review_cascade.parse_semantic_result(json.dumps(response)), packet)

    def test_findings_must_cite_packet_evidence_and_cannot_contradict_pass(self):
        packet = self.build()
        valid_ref = 'contract:invariant:0'
        response = {'contract_compliance': 'PASS', 'acceptance_results': [
            {'criterion': 'Configured verification passes.', 'status': 'PASS',
             'evidence_refs': ['verification:' + review_cascade._sha(
                 json.dumps(self.pass_verify['checks'][0]['command'], ensure_ascii=False,
                            separators=(',', ':')))[:16]]}],
            'invariant_violations': [{'explanation': 'Contradictory finding.',
                                      'evidence_refs': [valid_ref]}],
            'acceptance_gaps': [], 'plan_deviations': [], 'new_risks': [],
            'astra_required': False, 'astra_trigger': None, 'astra_reason': None}
        parsed = review_cascade.parse_semantic_result(json.dumps(response))
        with self.assertRaisesRegex(ValueError, 'contradicts'):
            review_cascade.validate_acceptance_results(parsed, packet)
        response['contract_compliance'] = 'FAIL'
        response['invariant_violations'][0]['evidence_refs'] = ['not-in-packet']
        parsed = review_cascade.parse_semantic_result(json.dumps(response))
        with self.assertRaisesRegex(ValueError, 'outside its packet'):
            review_cascade.validate_acceptance_results(parsed, packet)

    def test_explicit_plan_deviation_triggers_semantic_review(self):
        self.state['stage_deviation'] = {'reported': True, 'description': 'Changed the recovery approach.',
                                         'architectural': False}
        packet = self.build()
        self.assertIn('PLAN_DEVIATION', [x['trigger'] for x in packet['triggered_reviews']])
        self.assertEqual(review_cascade.required_tier(packet), 'sol')
        with self.assertRaises(ValueError):
            review_cascade.build_astra_packet(packet, trigger='PLAN_DEVIATION')

    def test_architectural_plan_deviation_allows_narrow_astra_review(self):
        path = self.root / 'src' / 'app.py'
        path.write_text('def run():\n    return 2\n', encoding='utf-8')
        self.state['stage_deviation'] = {'reported': True, 'description': 'Changed persistence contract.',
                                         'architectural': True}
        packet = self.build()
        self.assertEqual(review_cascade.required_tier(packet), 'astra')
        narrow = review_cascade.build_astra_packet(packet, trigger='PLAN_DEVIATION')
        self.assertEqual(narrow['triggers'], ['PLAN_DEVIATION'])
        self.assertLessEqual(narrow['packet_bytes'], review_cascade.ASTRA_PACKET_BUDGET + 16)

    def test_legacy_task_without_contract_does_not_enter_new_review_flow(self):
        self.state.pop('plan_contract')
        with self.assertRaises(ValueError):
            review_cascade.build_review_packet(self.root, self.state, self.stage, self.pass_verify)

    def test_packet_reports_no_model_turn_for_deterministic_pass(self):
        packet = self.build()
        result = review_cascade.result(packet, turns=[])
        self.assertFalse(result['telemetry']['semantic_review_used'])
        self.assertEqual(result['telemetry']['semantic_review_turns'], 0)

    def test_astra_packet_is_narrow_and_requires_high_cost_trigger(self):
        path = self.root / 'src' / 'auth.py'
        path.write_text('token = "local"\n', encoding='utf-8')
        self.stage['contract']['allowed_paths'].append('src/auth.py')
        packet = self.build()
        narrow = review_cascade.build_astra_packet(packet, trigger='SECURITY_BOUNDARY_CHANGED')
        self.assertEqual([item['path'] for item in narrow['relevant_diff_fragments']], ['src/auth.py'])
        self.assertNotIn('changed_paths', narrow)
        response = {'contract_compliance': 'UNKNOWN', 'acceptance_results': [
            {'criterion': 'Configured verification passes.', 'status': 'UNKNOWN', 'evidence_refs': ['diff:src/app.py']}],
            'invariant_violations': [], 'acceptance_gaps': [], 'plan_deviations': [],
            'new_risks': [], 'astra_required': False, 'astra_trigger': None, 'astra_reason': None}
        with self.assertRaisesRegex(ValueError, 'outside its packet'):
            review_cascade.validate_acceptance_results(response, packet,
                allowed_refs=['contract:objective'])
        with self.assertRaises(ValueError):
            review_cascade.build_astra_packet(packet, trigger='TEST_GAP')

    def test_astra_packet_covers_all_high_cost_triggers_in_one_bounded_review(self):
        schema = self.root / 'src' / 'schema.py'
        schema.write_text('version = 2\n', encoding='utf-8')
        self.stage['contract']['allowed_paths'].append('src/schema.py')
        packet = self.build()
        narrow = review_cascade.build_astra_packet(packet,
            trigger=['SCHEMA_CHANGED', 'PUBLIC_API_CHANGED'])
        self.assertEqual(narrow['triggers'], ['PUBLIC_API_CHANGED', 'SCHEMA_CHANGED'])
        self.assertIn('src/schema.py', [item['path'] for item in narrow['relevant_diff_fragments']])


if __name__ == '__main__':
    unittest.main()
