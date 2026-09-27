import json
import os
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_dev import evidence_packet as evidence
from ai_dev.cli import main


class EvidencePacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.check_call(['git', 'init', '-q'], cwd=self.root)
        subprocess.check_call(['git', 'config', 'user.email', 'test@example.invalid'], cwd=self.root)
        subprocess.check_call(['git', 'config', 'user.name', 'Test'], cwd=self.root)
        (self.root / 'src').mkdir()
        (self.root / 'tests').mkdir()
        (self.root / 'src' / 'importer.py').write_text(
            'def import_history(history):\n    return history\n', encoding='utf-8')
        (self.root / 'tests' / 'test_importer.py').write_text(
            'def test_import_history_order():\n    assert True\n', encoding='utf-8')
        (self.root / 'PLAN.md').write_text('# Import contract\nKeep messages in original order.\n', encoding='utf-8')
        (self.root / 'PROJECT_PLAN.md').write_text(
            '# Smart library plan\nUse the local E5 model and preserve citation metadata.\n', encoding='utf-8')
        (self.root / 'src' / 'unrelated.py').write_text('def unrelated():\n    pass\n', encoding='utf-8')
        subprocess.check_call(['git', 'add', '.'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '-qm', 'baseline'], cwd=self.root)
        (self.root / '.ai-dev' / 'runs' / 'task-1').mkdir(parents=True)
        state = {'id': 'task-1', 'status': 'BLOCKED',
                 'prompt': 'Import saved Telegram history.json with local media in original order.',
                 'kind': 'hard', 'checkpoint_index': 0,
                 'plan': {'schema': 2, 'objective': 'Import local Telegram history safely',
                          'worker_kind': 'hard', 'steps': [{'id': 'step-1', 'title': 'Import history',
                          'worker_kind': 'hard', 'files': ['src/importer.py', 'tests/test_importer.py', 'PLAN.md'],
                          'acceptance': 'Preserve source archive and original text/media order.'}]}}
        self.write_json('.ai-dev/state.json', state)
        self.write_json('.ai-dev/runs/task-1/state.json', state)
        (self.root / '.ai-dev' / 'memory').mkdir(parents=True)
        self.write_jsonl('.ai-dev/memory/decisions.jsonl', [])
        self.write_jsonl('.ai-dev/memory/failures.jsonl', [])
        self.write_jsonl('.ai-dev/memory/events.jsonl', [])

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, rel, value):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def write_jsonl(self, rel, rows):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def build(self, **kwargs):
        return evidence.build_packet(self.root, task_id='task-1', **kwargs)

    def test_build_uses_saved_task_and_is_model_free(self):
        packet = self.build()
        self.assertEqual(packet['task']['task_id'], 'task-1')
        self.assertEqual(packet['budget']['model_turns_used_to_build'], 0)

    def test_task_goal_is_preserved(self):
        packet = self.build()
        self.assertIn('history.json', packet['task']['goal'])

    def test_plan_scope_selects_declared_source(self):
        packet = self.build()
        self.assertIn('src/importer.py', packet['scope']['files'])

    def test_plan_scope_selects_declared_test(self):
        packet = self.build()
        self.assertTrue(any(x['path'] == 'tests/test_importer.py'
                            for x in packet['scope']['test_fragments']))

    def test_plan_scope_includes_referenced_spec_excerpt(self):
        packet = self.build()
        self.assertTrue(any(x['path'] == 'PLAN.md'
                            for x in packet['scope']['reference_fragments']))

    def test_explicit_task_reference_is_prioritized_as_evidence(self):
        goal = ('Validate the ExampleProject local search provenance path. Read '
                'TASK.md and PROJECT_PLAN.md before planning.')
        packet = evidence.build_packet(self.root, task_id='task-1', goal=goal)
        refs = {row['path']: row for row in packet['scope']['reference_fragments']}
        self.assertIn('PROJECT_PLAN.md', refs)
        self.assertEqual(refs['PROJECT_PLAN.md']['selection_reason'], 'EXPLICIT_TASK_REFERENCE')
        self.assertIn('PROJECT_PLAN.md', packet['scope']['files'])

    def test_requested_directory_closes_to_bounded_tracked_children(self):
        packet = evidence.build_packet(self.root, task_id='task-1', extra_requests=[{
            'kind': 'path', 'query': 'tests', 'reason': 'Read focused test evidence.'}])
        request = packet['selection_hygiene']['evidence_requests'][0]
        self.assertTrue(request['found'])
        self.assertIn('tests/test_importer.py', packet['scope']['files'])
        self.assertTrue(any(row['path'] == 'tests/test_importer.py' and
                            row['selection_reason'] == 'PLANNER_REQUEST'
                            for row in packet['scope']['test_fragments']))

    def test_source_excerpt_is_referenced_with_hash_and_range(self):
        item = self.build()['scope']['source_fragments'][0]
        self.assertIn('content_hash', item)
        self.assertIn('snapshot_commit', item)
        self.assertLessEqual(item['line_start'], item['line_end'])
        self.assertLessEqual(item['excerpt_bytes'], evidence.MAX_FRAGMENT_BYTES)

    def test_oversized_source_line_is_omitted_as_whole_evidence(self):
        (self.root / 'src' / 'huge.py').write_text('x' * (evidence.MAX_FRAGMENT_BYTES + 1), encoding='utf-8')
        self.assertIsNone(evidence._fragment(self.root, 'src/huge.py', [],
                                             subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.root, text=True).strip(),
                                             'test'))

    def test_does_not_read_ai_dev_as_source(self):
        packet = self.build()
        self.assertFalse(any('.ai-dev' in path for path in packet['scope']['files']))

    def test_excludes_generated_and_archive_paths(self):
        for value in ('.ai-dev/logs/a.jsonl', 'dist/app.py', 'releases/x.py',
                      'releases.during-turn-abc/app.py', 'cache/module.py',
                      '.generated/module.py', '../outside.py'):
            self.assertIsNone(evidence._safe_rel(value))

    def test_explicit_plan_path_may_include_a_generated_copy(self):
        self.write_json('.ai-dev/state.json', {'id': 'task-1', 'status': 'PREFLIGHT',
            'prompt': 'Update dist/app.py as the explicit build output under test.',
            'plan': {'steps': [{'files': ['dist/app.py'], 'title': 'Inspect generated copy',
                                'acceptance': 'The requested generated output is checked.'}]}})
        self.write_json('.ai-dev/runs/task-1/state.json', json.loads(
            (self.root / '.ai-dev/state.json').read_text(encoding='utf-8')))
        (self.root / 'dist').mkdir()
        (self.root / 'dist' / 'app.py').write_text('def update_build():\n    return True\n', encoding='utf-8')
        subprocess.check_call(['git', 'add', 'dist/app.py'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '-qm', 'explicit output evidence'], cwd=self.root)
        packet = self.build()
        self.assertIn('dist/app.py', packet['scope']['files'])
        self.assertIn('dist/app.py', packet['selection_hygiene']['explicit_overrides'])

    def test_deferred_windows_filters_implementation_but_keeps_shared_contract(self):
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts' / 'windows_build.ps1').write_text('Write-Output serialization\n', encoding='utf-8')
        (self.root / 'contracts').mkdir()
        (self.root / 'contracts' / 'shared_serialization.py').write_text(
            'def encode_contract(value):\n    return value\n', encoding='utf-8')
        subprocess.check_call(['git', 'add', 'scripts/windows_build.ps1',
                               'contracts/shared_serialization.py'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '-qm', 'platform candidates'], cwd=self.root)
        packet = evidence.build_packet(self.root, task_id='task-1',
            goal='Implement local archive serialization. Windows build deferred; preserve the shared serialization contract.')
        self.assertEqual(packet['platform_scope']['deferred_platforms'], ['windows'])
        self.assertIn('scripts/windows_build.ps1', packet['selection_hygiene']['platform_excluded'])
        self.assertNotIn('scripts/windows_build.ps1', packet['scope']['files'])
        self.assertIn('contracts/shared_serialization.py', packet['scope']['files'])
        requested = evidence.build_packet(self.root, task_id='task-1',
            goal='Implement local archive serialization. Windows build deferred; preserve the shared serialization contract.',
            extra_requests=[{'kind': 'path', 'query': 'scripts/windows_build.ps1',
                             'reason': 'Check the implementation'}])
        self.assertNotIn('scripts/windows_build.ps1', requested['scope']['files'])
        self.assertIn('scripts/windows_build.ps1', requested['selection_hygiene']['platform_excluded'])

    def test_candidate_filter_excludes_release_cache_and_git_ignored_copies(self):
        (self.root / 'releases.during-turn-smoke').mkdir()
        (self.root / 'releases.during-turn-smoke' / 'importer.py').write_text(
            'def import_history(value):\n    return value\n', encoding='utf-8')
        (self.root / 'cache').mkdir()
        (self.root / 'cache' / 'importer.py').write_text(
            'def import_history(value):\n    return value\n', encoding='utf-8')
        (self.root / 'ignored.py').write_text('def import_history(value):\n    return value\n', encoding='utf-8')
        (self.root / '.gitignore').write_text('ignored.py\n', encoding='utf-8')
        subprocess.check_call(['git', 'add', '-f', 'releases.during-turn-smoke/importer.py',
                               'cache/importer.py', 'ignored.py', '.gitignore'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '-qm', 'generated copies'], cwd=self.root)
        packet = evidence.build_packet(self.root, task_id='task-1',
            goal='Import history with safe importer module and test coverage.')
        self.assertIn('releases.during-turn-smoke/importer.py', packet['selection_hygiene']['generated_excluded'])
        self.assertIn('cache/importer.py', packet['selection_hygiene']['generated_excluded'])
        self.assertIn('ignored.py', packet['selection_hygiene']['ignore_excluded'])
        self.assertNotIn('ignored.py', packet['scope']['files'])

    def test_default_budget_is_within_target_and_hard_limit(self):
        packet = self.build()
        self.assertLessEqual(packet['budget']['final_bytes'], evidence.DEFAULT_BUDGET)
        self.assertLessEqual(packet['budget']['final_bytes'], evidence.MAX_BUDGET)

    def test_budget_rejects_too_small_and_too_large(self):
        with self.assertRaises(ValueError):
            self.build(budget=evidence.MIN_BUDGET - 1)
        with self.assertRaises(ValueError):
            self.build(budget=evidence.MAX_BUDGET + 1)

    def test_budget_drops_whole_evidence_not_partial_text(self):
        packet = self.build(budget=evidence.MIN_BUDGET)
        self.assertLessEqual(packet['budget']['final_bytes'], evidence.MIN_BUDGET)
        self.assertTrue(all(isinstance(item['excerpt'], str)
                            for item in packet['scope']['source_fragments']))

    def test_dirty_overlay_is_explicit_and_hashable(self):
        (self.root / 'src' / 'importer.py').write_text('def import_history(x):\n    return x + 1\n', encoding='utf-8')
        packet = self.build()
        self.assertEqual(packet['snapshot']['working_tree'], 'DIRTY')
        fragment = next(x for x in packet['scope']['source_fragments'] if x['path'] == 'src/importer.py')
        self.assertEqual(fragment['source_version'], 'WORKTREE_OVERLAY')
        self.assertNotEqual(fragment['content_hash'], fragment['committed_hash'])

    def test_clean_file_fragment_marks_head(self):
        packet = self.build()
        fragment = next(x for x in packet['scope']['source_fragments'] if x['path'] == 'src/importer.py')
        self.assertEqual(fragment['source_version'], 'HEAD')

    def test_no_tests_is_explicit_unknown(self):
        (self.root / 'tests' / 'test_importer.py').unlink()
        subprocess.check_call(['git', 'rm', '--cached', '-q', 'tests/test_importer.py'], cwd=self.root)
        (self.root / '.ai-dev/state.json').unlink()
        (self.root / '.ai-dev/runs/task-1/state.json').unlink()
        packet = evidence.build_packet(self.root, goal='modify importer logic')
        self.assertEqual(packet['scope']['test_discovery'], 'NONE_FOUND')
        self.assertTrue(any('No tracked test source exists' in value for value in packet['unknowns']))

    def test_tests_are_discovered_even_when_no_relevant_excerpt_is_selected(self):
        packet = evidence.build_packet(self.root, goal='modify importer logic')
        self.assertEqual(packet['scope']['test_discovery'], 'DISCOVERED_NOT_RUN')
        self.assertIn('tests/test_importer.py', packet['scope']['test_paths_discovered'])

    def test_memory_task_match_is_included(self):
        row = {'decision_id': 'd1', 'timestamp': '2026-09-24T00:00:00Z', 'project_id': 'p',
               'task_id': 'task-1', 'stage_id': 'step-1', 'session_id': 's', 'commit': None,
               'decision': 'Keep original ordering', 'rationale': 'Source order matters',
               'evidence_refs': [], 'alternatives_considered': [], 'status': 'accepted', 'supersedes': []}
        self.write_jsonl('.ai-dev/memory/decisions.jsonl', [row])
        packet = self.build()
        self.assertEqual(packet['decisions'][0]['id'], 'd1')

    def test_memory_records_are_compact_and_do_not_copy_raw_artifacts(self):
        row = {'failure_id': 'f1', 'timestamp': '2026-09-24T00:00:00Z', 'project_id': 'p',
               'task_id': 'task-1', 'stage_id': 'step-1', 'session_id': 's', 'commit': None,
               'error_class': 'verification', 'error_signature': 'missing dependency',
               'command': ['python', '-m', 'test'], 'affected_paths': ['src/importer.py'],
               'attempt': 1, 'resolution_status': 'open', 'artifact_refs': ['.ai-dev/raw/big.log']}
        self.write_jsonl('.ai-dev/memory/failures.jsonl', [row])
        packet = self.build()
        self.assertNotIn('command', packet['failures'][0])
        self.assertLess(len(json.dumps(packet['failures'])), 2000)

    def test_identical_events_deduplicate_in_view(self):
        row = {'event_id': 'e1', 'timestamp': '2026-09-24T00:00:00Z', 'project_id': 'p',
               'task_id': 'task-1', 'stage_id': 'step-1', 'session_id': 's', 'actor_role': 'planner',
               'type': 'plan_saved', 'commit': None, 'artifact_refs': [], 'summary': 'saved plan'}
        visible, summary = evidence._event_projection([row, row], [])
        self.assertEqual(len(visible), 1)
        self.assertEqual(summary['raw_unique_count'], 1)

    def test_duplicate_event_id_with_conflicting_content_is_flagged(self):
        base = {'event_id': 'e1', 'timestamp': '2026-09-24T00:00:00Z', 'project_id': 'p',
                'task_id': 'task-1', 'stage_id': 'step-1', 'session_id': 's', 'actor_role': 'planner',
                'type': 'plan_saved', 'commit': None, 'artifact_refs': [], 'summary': 'one'}
        second = dict(base, summary='different')
        visible, summary = evidence._event_projection([base, second], [])
        self.assertEqual(summary['conflicts'][0]['kind'], 'DUPLICATE_ID_DIFFERENT_CONTENT')
        self.assertEqual([row['summary'] for row in visible], ['one', 'different'])

    def test_m5_rollup_requires_explicit_supersedes(self):
        rows = [{'event_id': 'm5-old', 'timestamp': '1', 'stage_id': 'milestone-5', 'commit': 'old'},
                {'event_id': 'm5-new', 'timestamp': '2', 'stage_id': 'milestone-5', 'commit': 'new'}]
        decisions = [{'stage_id': 'milestone-5', 'supersedes': ['old']}]
        visible, summary = evidence._event_projection(rows, decisions)
        self.assertEqual([row['event_id'] for row in visible], ['m5-new'])
        self.assertEqual(summary['rolled_up_event_ids'], ['m5-old'])

    def test_m5_events_preserved_without_supersession_proof(self):
        rows = [{'event_id': 'm5-old', 'timestamp': '1', 'stage_id': 'milestone-5', 'commit': 'old'},
                {'event_id': 'm5-new', 'timestamp': '2', 'stage_id': 'milestone-5', 'commit': 'new'}]
        visible, summary = evidence._event_projection(rows, [])
        self.assertEqual(len(visible), 2)
        self.assertEqual(summary['rolled_up_event_ids'], [])

    def test_m5_generated_view_rollup_never_rewrites_raw_journal(self):
        old = {'event_id': 'm5-old', 'timestamp': '2026-09-23T00:00:00Z', 'project_id': 'p',
               'task_id': 'old-task', 'stage_id': 'milestone-5', 'session_id': 's',
               'actor_role': 'coordinator', 'type': 'milestone_commit_finalized',
               'commit': 'oldcommit', 'artifact_refs': [], 'summary': 'Earlier M5 record'}
        new = dict(old, event_id='m5-new', timestamp='2026-09-24T00:00:00Z',
                   commit='newcommit', summary='Final M5 record')
        decision = {'decision_id': 'm5-final', 'timestamp': '2026-09-24T00:00:00Z',
                    'project_id': 'p', 'task_id': 'old-task', 'stage_id': 'milestone-5',
                    'session_id': 's', 'commit': 'newcommit', 'decision': 'Accept final M5',
                    'rationale': 'Verified final milestone', 'evidence_refs': [],
                    'alternatives_considered': [], 'status': 'accepted',
                    'supersedes': ['oldcommit']}
        self.write_jsonl('.ai-dev/memory/events.jsonl', [old, new])
        self.write_jsonl('.ai-dev/memory/decisions.jsonl', [decision])
        before = (self.root / '.ai-dev/memory/events.jsonl').read_bytes()
        packet = self.build()
        after = (self.root / '.ai-dev/memory/events.jsonl').read_bytes()
        self.assertEqual(packet['event_projection']['rolled_up_event_ids'], ['m5-old'])
        self.assertEqual(before, after)

    def test_packet_staleness_initially_valid(self):
        packet = self.build()
        self.assertEqual(evidence.check_packet(self.root, packet)['status'], 'VALID')

    def test_packet_staleness_detects_head_change(self):
        packet = self.build()
        (self.root / 'src' / 'unrelated.py').write_text('changed\n', encoding='utf-8')
        subprocess.check_call(['git', 'add', '.'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '-qm', 'unrelated change'], cwd=self.root)
        self.assertEqual(evidence.check_packet(self.root, packet)['status'], 'STALE')

    def test_packet_staleness_detects_overlay_change(self):
        packet = self.build()
        (self.root / 'src' / 'importer.py').write_text('changed\n', encoding='utf-8')
        result = evidence.check_packet(self.root, packet)
        self.assertEqual(result['status'], 'STALE')
        self.assertTrue(any('FILE_CHANGED' in reason for reason in result['reasons']))

    def test_dirty_hash_tracks_changed_large_file_without_loading_it_into_memory(self):
        large = self.root / 'src' / 'large.py'
        large.write_bytes(b'a' * (evidence.MAX_SOURCE_FILE + 10))
        subprocess.check_call(['git', 'add', '.'], cwd=self.root)
        subprocess.check_call(['git', 'commit', '-qm', 'large baseline'], cwd=self.root)
        packet = self.build()
        large.write_bytes(b'b' * (evidence.MAX_SOURCE_FILE + 10))
        result = evidence.check_packet(self.root, packet)
        self.assertEqual(result['status'], 'STALE')
        self.assertIn('WORKTREE_CHANGED', result['reasons'])

    def test_packet_staleness_detects_saved_task_change(self):
        packet = self.build()
        state = json.loads((self.root / '.ai-dev/state.json').read_text())
        state['status'] = 'COMPLETED'
        self.write_json('.ai-dev/state.json', state)
        result = evidence.check_packet(self.root, packet)
        self.assertEqual(result['status'], 'STALE')
        self.assertIn('TASK_STATE_CHANGED', result['reasons'])

    def test_packet_staleness_detects_memory_change(self):
        packet = self.build()
        (self.root / '.ai-dev/memory/events.jsonl').write_text('{}\n', encoding='utf-8')
        result = evidence.check_packet(self.root, packet)
        self.assertEqual(result['status'], 'STALE')
        self.assertIn('MEMORY_CHANGED:events.jsonl', result['reasons'])

    def test_stale_check_rejects_packet_path_traversal(self):
        packet = self.build()
        packet['staleness']['file_hashes'][0]['path'] = '../outside'
        result = evidence.check_packet(self.root, packet)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIn('INVALID_FILE_REFERENCE', result['reasons'])

    def test_packet_roundtrip_save_load(self):
        packet = self.build()
        path = evidence.save_packet(self.root, packet)
        self.assertEqual(evidence.load_packet(path)['packet_id'], packet['packet_id'])

    def test_wrong_task_id_requires_explicit_goal(self):
        with self.assertRaises(ValueError):
            evidence.build_packet(self.root, task_id='missing')

    def test_unsafe_task_id_is_rejected(self):
        with self.assertRaises(ValueError):
            evidence.build_packet(self.root, task_id='../outside', goal='safe task')

    def test_platform_windows_is_not_claimed_verified(self):
        packet = self.build()
        windows = next(item for item in packet['platform_constraints'] if item['platform'] == 'windows')
        self.assertEqual(windows['status'], 'UNKNOWN_UNVERIFIED')

    def test_budget_records_no_model_turns(self):
        packet = self.build()
        self.assertEqual(packet['budget']['model_turns_used_to_build'], 0)

    def test_attempt_telemetry_keeps_configured_and_observed_separate(self):
        attempt = evidence._compact_attempt({'attempt': 2, 'model': 'gpt-6-astra',
            'reasoning': 'medium', 'telemetry': {'configured_model': 'gpt-6-astra',
            'observed_model': 'UNKNOWN', 'observed_reasoning': 'UNKNOWN',
            'usage': {'input_tokens': 120, 'cached_input_tokens': 80,
                      'output_tokens': 10}, 'session_id': 'session-1'}})
        self.assertEqual(attempt['configured_model'], 'gpt-6-astra')
        self.assertEqual(attempt['observed_model'], 'UNKNOWN')
        self.assertEqual(attempt['input_tokens'], 120)
        grouped = evidence._aggregate_attempts([attempt, dict(attempt, input_tokens=30)])
        self.assertEqual(grouped[0]['turns'], 2)
        self.assertEqual(grouped[0]['input_tokens'], 150)

    def test_build_is_deterministic_except_elapsed_time(self):
        with patch.object(evidence.time, "monotonic", side_effect=[0, 0.125, 0, 0.125]):
            one = self.build()
            two = self.build()
        for value in (one, two):
            value['budget']['build_time_ms'] = 0
        self.assertEqual(one, two)

    def test_elapsed_time_digit_width_preserves_exact_byte_accounting(self):
        for elapsed in (0.001, 0.125, 1.125):
            with self.subTest(elapsed=elapsed), patch.object(
                    evidence.time, 'monotonic', side_effect=[0, elapsed]):
                packet = self.build()
            size = evidence._measure(packet)
            self.assertEqual(packet['budget']['final_bytes'], size)
            self.assertEqual(packet['budget']['estimated_tokens'], (size + 3) // 4)
            self.assertLessEqual(size, evidence.DEFAULT_BUDGET)

    def test_cli_build_saves_offline_packet(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['-C', str(self.root), 'evidence', 'build', '--task-id', 'task-1', '--json'])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['packet']['budget']['model_turns_used_to_build'], 0)
        self.assertTrue(Path(payload['saved_to']).is_file())


if __name__ == '__main__':
    unittest.main()
