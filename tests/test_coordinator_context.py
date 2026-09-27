import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_dev import coordinator_context, project_memory
from ai_dev.storage import read, write


def memory_event(root, event_id, task_id=None, summary='event', **overrides):
    value = {'event_id': event_id, 'task_id': task_id, 'stage_id': 'stage-1',
             'project_id': project_memory._project_id(root),
             'session_id': None, 'actor_role': 'worker', 'type': 'stage_completed',
             'commit': 'abc123', 'artifact_refs': ['.ai-dev/runs/%s/codex.jsonl' % (task_id or 'old')],
             'summary': summary}
    value.update(overrides)
    return project_memory.append_event(root, **value)


def memory_decision(root, decision_id, task_id, decision):
    return project_memory.append_decision(
        root, project_id=project_memory._project_id(root),
        decision_id=decision_id, task_id=task_id, stage_id='planning',
        session_id=None, commit='abc123', decision=decision, rationale='accepted rationale',
        evidence_refs=['docs/adr/decision.md'], alternatives_considered=[], status='accepted',
        supersedes=None)


def memory_failure(root, failure_id, task_id, path='src/target.py'):
    return project_memory.append_failure(
        root, project_id=project_memory._project_id(root), failure_id=failure_id,
        task_id=task_id, stage_id='stage-1', session_id=None,
        commit='abc123', error_class='test_failure', error_signature='same-test-fails',
        command=['python', '-m', 'unittest'], affected_paths=[path], attempt=2,
        resolution_status='unresolved', artifact_refs=['.ai-dev/runs/%s/error.json' % task_id])


class CoordinatorContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=self.root, check=True)
        (self.root / 'tracked.txt').write_text('initial', encoding='utf-8')
        subprocess.run(['git', 'add', 'tracked.txt'], cwd=self.root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'initial'], cwd=self.root, check=True)
        project_memory.initialize(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def set_current(self, **updates):
        current = project_memory.read_current(self.root)
        current.update(updates)
        write(self.root / '.ai-dev/memory/current.json', current)

    def test_empty_memory_builds_valid_capsule_and_is_deterministic_except_timestamp(self):
        first = coordinator_context.build_capsule(self.root)
        second = coordinator_context.build_capsule(self.root)
        self.assertEqual(first['schema_version'], 1)
        self.assertEqual(first['context_meta']['memory_status'], 'OK')
        self.assertEqual(first['current']['status'], 'IDLE')
        self.assertEqual(first['context_meta']['snapshot_id'], second['context_meta']['snapshot_id'])
        self.assertLessEqual(first['context_meta']['bytes_estimated'], first['context_meta']['budget_bytes'])
        self.assertEqual(first['context_meta']['bytes_estimated'],
                         len(json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode('utf-8')))

    def test_active_task_blockers_decision_and_only_related_history_are_included(self):
        self.set_current(active_task='task-live', active_stage='stage-1', status='BLOCKED',
                         blockers=['Need a local decision'], active_invariants=['Keep public API stable'],
                         recent_decisions=['decision-live'], next_allowed_actions=['Wait for approval'])
        write(self.root / '.ai-dev/state.json', {'id': 'task-live', 'status': 'BLOCKED',
              'reason': 'Canonical state requires a review.',
              'prompt': 'Finish the focused feature.', 'plan': {'schema': 2, 'steps': [
                  {'id': 'stage-1', 'title': 'Implement the bounded change'}]}, 'checkpoint_index': 0})
        memory_decision(self.root, 'decision-live', 'task-live', 'Preserve the current API.')
        memory_decision(self.root, 'decision-other', 'task-other', 'Unrelated old task.')
        memory_event(self.root, 'event-live', 'task-live', 'Verification blocked.')
        memory_event(self.root, 'event-other', 'task-other', 'Unrelated old event.')
        memory_failure(self.root, 'failure-live', 'task-live')
        capsule = coordinator_context.build_capsule(self.root)
        self.assertEqual(capsule['task']['goal'], 'Finish the focused feature.')
        self.assertEqual(capsule['task']['stage_summary'], 'Implement the bounded change')
        self.assertEqual(capsule['current']['blockers'],
                         ['Canonical state requires a review.', 'Need a local decision'])
        self.assertEqual([row['id'] for row in capsule['decisions']], ['decision-live'])
        self.assertEqual([row['id'] for row in capsule['recent_events']], ['event-live'])
        self.assertEqual([row['id'] for row in capsule['relevant_failures']], ['failure-live'])
        serialized = json.dumps(capsule)
        self.assertNotIn('Unrelated old', serialized)
        self.assertNotIn('raw transcript content', serialized)
        self.assertIn('.ai-dev/runs/task-live/error.json', serialized)

    def test_component_filter_and_only_raw_artifact_refs_are_returned(self):
        self.set_current(active_task='task-live', active_stage='stage-1', status='RUNNING')
        memory_event(self.root, 'event-1', 'task-live', 'Changed parser')
        memory_event(self.root, 'event-2', 'task-live', 'Changed unrelated UI')
        capsule = coordinator_context.build_capsule(self.root, component='parser')
        self.assertEqual([row['summary'] for row in capsule['recent_events']], ['Changed parser'])
        self.assertNotIn('raw transcript content', json.dumps(capsule))

    def test_budget_drops_low_priority_items_deterministically_and_marks_truncated(self):
        self.set_current(active_task='task-live', active_stage='stage-1', status='RUNNING')
        for index in range(30):
            memory_event(self.root, 'event-%02d' % index, 'task-live', 'evidence %02d ' % index + ('x' * 250))
        one = coordinator_context.build_capsule(self.root, budget=3072)
        two = coordinator_context.build_capsule(self.root, budget=3072)
        self.assertLessEqual(one['context_meta']['bytes_estimated'], 3072)
        self.assertTrue(one['context_meta']['truncated'])
        self.assertGreater(one['context_meta']['items_dropped'], 0)
        self.assertEqual([row['id'] for row in one['recent_events']],
                         [row['id'] for row in two['recent_events']])
        self.assertEqual(one['recent_events'][0]['id'], 'event-29')

    def test_staleness_detects_head_task_and_event_changes(self):
        capsule = coordinator_context.build_capsule(self.root)
        self.assertFalse(coordinator_context.is_stale(self.root, capsule)['stale'])
        memory_event(self.root, 'event-new', 'task-new', 'new event')
        current = project_memory.read_current(self.root)
        current.update(active_task='task-new', status='RUNNING')
        write(self.root / '.ai-dev/memory/current.json', current)
        state_change = coordinator_context.is_stale(self.root, capsule)
        self.assertTrue(state_change['stale'])
        self.assertIn('ACTIVE_TASK_CHANGED', state_change['reasons'])
        self.assertIn('EVENTS_CHANGED', state_change['reasons'])
        (self.root / 'tracked.txt').write_text('changed', encoding='utf-8')
        subprocess.run(['git', 'add', 'tracked.txt'], cwd=self.root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'second'], cwd=self.root, check=True)
        changed = coordinator_context.is_stale(self.root, capsule)
        self.assertTrue(changed['stale'])
        self.assertIn('HEAD_CHANGED', changed['reasons'])

    def test_checkpoint_delta_and_missing_cursor_fail_closed(self):
        memory_event(self.root, 'event-1', None, 'before checkpoint')
        initial = coordinator_context.build_capsule(self.root)
        saved = coordinator_context.save_checkpoint(self.root, 'coord-1', initial)
        self.assertEqual(saved['last_seen_event_id'], 'event-1')
        memory_event(self.root, 'event-2', None, 'after checkpoint')
        delta = coordinator_context.build_capsule(self.root, checkpoint_id='coord-1')
        self.assertTrue(delta['context_meta']['delta'])
        self.assertEqual([row['id'] for row in delta['recent_events']], ['event-2'])
        missing = coordinator_context.build_capsule(self.root, since_event_id='gone')
        self.assertEqual(missing['recent_events'], [])
        self.assertFalse(missing['context_meta']['event_cursor_found'])

    def test_text_rendering_uses_same_capsule_fields_and_cli_stays_offline(self):
        capsule = coordinator_context.build_capsule(self.root)
        rendered = coordinator_context.render_capsule(capsule)
        self.assertIn('GIT', rendered)
        self.assertIn('ACTIVE TASK', rendered)
        self.assertIn(capsule['context_meta']['snapshot_id'], rendered)
        self.assertNotIn('history dump', rendered.casefold())
        from ai_dev.cli import main
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['-C', str(self.root), 'context', '--json']), 0)
        parsed = json.loads(output.getvalue())
        self.assertIn('context_meta', parsed)
        self.assertEqual(parsed['context_meta']['bytes_estimated'], len(output.getvalue().strip().encode('utf-8')))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['-C', str(self.root), 'status', '--json']), 0)

    def test_malformed_memory_is_degraded_but_capsule_remains_available(self):
        path = self.root / '.ai-dev/memory/events.jsonl'
        path.write_text('{broken\n', encoding='utf-8')
        capsule = coordinator_context.build_capsule(self.root)
        self.assertEqual(capsule['context_meta']['memory_status'], 'DEGRADED')
        self.assertEqual(capsule['current']['status'], 'IDLE')

    def test_non_git_project_marks_git_dirty_unknown_and_context_compaction_visible(self):
        plain = tempfile.TemporaryDirectory()
        try:
            root = Path(plain.name)
            project_memory.initialize(root)
            current = project_memory.read_current(root)
            current.update(active_task='task-long', status='RUNNING',
                           active_invariants=['Invariant %d %s' % (n, 'x' * 220) for n in range(8)],
                           next_allowed_actions=['Action %d %s' % (n, 'y' * 220) for n in range(5)])
            write(root / '.ai-dev/memory/current.json', current)
            write(root / '.ai-dev/state.json', {'id': 'task-long', 'status': 'RUNNING',
                  'prompt': 'Goal ' + 'z' * 1300})
            capsule = coordinator_context.build_capsule(root, budget=3072)
            self.assertIsNone(capsule['project']['dirty'])
            self.assertTrue(capsule['context_meta']['truncated'])
            self.assertEqual(capsule['context_meta']['dropped_reasons']['core_budget_compaction'], 1)
        finally:
            plain.cleanup()


if __name__ == '__main__':
    unittest.main()
