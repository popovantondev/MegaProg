import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_dev import project_memory
from ai_dev.storage import read, write


def event(**overrides):
    value = {'schema_version': 1, 'event_id': 'event-1', 'timestamp': '2026-09-23T12:00:00Z',
             'project_id': 'project-1', 'task_id': 'task-1', 'stage_id': 'step-1',
             'session_id': 'session-1', 'actor_role': 'worker', 'type': 'stage_completed',
             'commit': 'abc123', 'artifact_refs': ['.ai-dev/runs/task-1/1/codex.jsonl'],
             'summary': 'Tests passed.'}
    value.update(overrides)
    return value


def decision(**overrides):
    value = {'schema_version': 1, 'decision_id': 'decision-1', 'timestamp': '2026-09-23T12:00:00Z',
             'project_id': 'project-1', 'task_id': 'task-1', 'stage_id': 'planning',
             'session_id': 'session-1', 'commit': 'abc123', 'decision': 'Use the existing state file.',
             'rationale': 'It is already canonical.', 'evidence_refs': ['ai_dev/storage.py'],
             'alternatives_considered': [], 'status': 'accepted', 'supersedes': None}
    value.update(overrides)
    return value


def failure(**overrides):
    value = {'schema_version': 1, 'failure_id': 'failure-1', 'timestamp': '2026-09-23T12:00:00Z',
             'project_id': 'project-1', 'task_id': 'task-1', 'stage_id': 'step-1',
             'session_id': 'session-1', 'commit': 'abc123', 'error_class': 'verification',
             'error_signature': 'sha256:abc', 'command': ['python', '-m', 'unittest'],
             'affected_paths': ['ai_dev/project_memory.py'], 'attempt': 2,
             'resolution_status': 'unresolved', 'artifact_refs': ['.ai-dev/runs/task-1/2/codex.jsonl']}
    value.update(overrides)
    return value


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        project_memory.initialize(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_initialize_creates_empty_v1_memory_and_current(self):
        directory = self.root / '.ai-dev' / 'memory'
        self.assertEqual(project_memory.read_current(self.root)['schema_version'], 1)
        for name in project_memory.LOGS:
            self.assertEqual((directory / name).read_text(encoding='utf-8'), '')

    def test_current_state_is_a_compact_projection_of_canonical_state(self):
        state = {'id': 'task-1', 'status': 'RUNNING', 'plan': {'schema': 2, 'steps': [
            {'id': 'step-1'}]}, 'checkpoint_index': 0,
            'progress': {'next_step': 'Wait for the current model turn'}}
        current = project_memory.project_current(self.root, state)
        self.assertEqual(current['active_task'], 'task-1')
        self.assertEqual(current['active_stage'], 'step-1')
        self.assertEqual(current['next_allowed_actions'], ['Wait for the current model turn'])
        self.assertNotIn('prompt', current)
        self.assertNotIn('attempts_detail', current)

    def test_current_state_atomic_replace_preserves_previous_on_failure(self):
        path = self.root / '.ai-dev' / 'memory' / 'current.json'
        before = path.read_bytes()
        with patch('ai_dev.storage.os.replace', side_effect=OSError('simulated replace failure')):
            with self.assertRaises(OSError):
                write(path, {'schema_version': 1, 'changed': True})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(project_memory.read_current(self.root)['schema_version'], 1)

    def test_append_event_decision_and_failure_preserves_artifact_refs(self):
        project_memory.append_event(self.root, **{k: v for k, v in event().items()
                                                   if k not in ('schema_version', 'event_id', 'timestamp')},
                                    event_id='event-1', timestamp='2026-09-23T12:00:00Z')
        project_memory.append_decision(self.root, **{k: v for k, v in decision().items()
                                                       if k not in ('schema_version', 'decision_id', 'timestamp')},
                                       decision_id='decision-1', timestamp='2026-09-23T12:00:00Z')
        project_memory.append_failure(self.root, **{k: v for k, v in failure().items()
                                                      if k not in ('schema_version', 'failure_id', 'timestamp')},
                                      failure_id='failure-1', timestamp='2026-09-23T12:00:00Z')
        directory = self.root / '.ai-dev' / 'memory'
        self.assertEqual(project_memory._read_jsonl(directory / 'events.jsonl')[0]['artifact_refs'],
                         ['.ai-dev/runs/task-1/1/codex.jsonl'])
        self.assertEqual(project_memory._read_jsonl(directory / 'decisions.jsonl')[0]['evidence_refs'],
                         ['ai_dev/storage.py'])
        self.assertEqual(project_memory._read_jsonl(directory / 'failures.jsonl')[0]['attempt'], 2)

    def test_duplicate_event_id_is_idempotent_for_same_record_and_rejects_conflict(self):
        first = event()
        project_memory._append(self.root, 'events', first)
        replay = dict(first, timestamp='2026-09-23T12:01:00Z')
        self.assertFalse(project_memory._append(self.root, 'events', replay))
        with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
            project_memory._append(self.root, 'events', dict(first, summary='different'))
        self.assertEqual(len(project_memory._read_jsonl(self.root / '.ai-dev/memory/events.jsonl')), 1)

    def test_malformed_entry_fails_closed(self):
        path = self.root / '.ai-dev' / 'memory' / 'events.jsonl'
        path.write_text('{broken\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'malformed JSON'):
            project_memory._read_jsonl(path)

    def test_unknown_schema_version_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported Project Memory schema_version'):
            project_memory._append(self.root, 'events', event(schema_version=2))
        current = project_memory.read_current(self.root)
        path = self.root / '.ai-dev' / 'memory' / 'current.json'
        current['schema_version'] = 2
        write(path, current)
        with self.assertRaisesRegex(ValueError, 'Unsupported Project Memory current'):
            project_memory.read_current(self.root)

    def test_memory_delta_updates_history_and_only_allowed_current_fields(self):
        local_event = event(project_id=project_memory._project_id(self.root))
        delta = {'schema_version': 1, 'new_events': [local_event], 'new_decisions': [],
                 'new_failures': [], 'resolved_blockers': [], 'new_blockers': ['Need review'],
                 'current_state_patch': {'active_invariants': ['Preserve existing API']}}
        current = project_memory.apply_delta(self.root, delta)
        self.assertEqual(current['blockers'], ['Need review'])
        self.assertEqual(current['active_invariants'], ['Preserve existing API'])
        self.assertEqual(len(project_memory._read_jsonl(self.root / '.ai-dev/memory/events.jsonl')), 1)

    def test_invalid_delta_does_not_change_canonical_memory_or_history(self):
        current_path = self.root / '.ai-dev' / 'memory' / 'current.json'
        before = current_path.read_bytes()
        delta = {'schema_version': 1, 'new_events': [event()], 'new_decisions': [],
                 'new_failures': [], 'resolved_blockers': [], 'new_blockers': [],
                 'current_state_patch': {'status': 'COMPLETED'}}
        with self.assertRaisesRegex(ValueError, 'non-editable fields'):
            project_memory.apply_delta(self.root, delta)
        self.assertEqual(current_path.read_bytes(), before)
        self.assertEqual(project_memory._read_jsonl(self.root / '.ai-dev/memory/events.jsonl'), [])

    def test_invalid_delta_record_is_rejected_before_any_append(self):
        delta = {'schema_version': 1, 'new_events': [event(schema_version=99)],
                 'new_decisions': [], 'new_failures': [], 'resolved_blockers': [],
                 'new_blockers': [], 'current_state_patch': {}}
        with self.assertRaisesRegex(ValueError, 'Unsupported Project Memory schema_version'):
            project_memory.apply_delta(self.root, delta)
        self.assertEqual(project_memory._read_jsonl(self.root / '.ai-dev/memory/events.jsonl'), [])

    def test_supervisor_memory_update_links_task_stage_session_and_raw_artifact(self):
        state = {'id': 'task-1', 'status': 'RUNNING', 'session_id': 'session-1',
                 'plan': {'schema': 2, 'steps': [{'id': 'step-1'}]}, 'checkpoint_index': 0,
                 'attempts': 1, 'created_at': 0, 'progress': {'next_step': 'Wait'}}
        current = project_memory.record_task_state(self.root, state, 'RUNNING')
        events = project_memory._read_jsonl(self.root / '.ai-dev/memory/events.jsonl')
        self.assertEqual(current['active_task'], 'task-1')
        self.assertEqual(events[-1]['stage_id'], 'step-1')
        self.assertEqual(events[-1]['session_id'], 'session-1')
        self.assertIn('runs/task-1/1/codex.jsonl', events[-1]['artifact_refs'])

    def test_accepted_plan_creates_linked_decision_and_event(self):
        state = {'id': 'task-2', 'status': 'PREFLIGHT', 'session_id': None,
                 'plan': {'schema': 2, 'objective': 'Goal', 'steps': [{'id': 'step-1'}]},
                 'planning_attempts': [{'codex': {'session_id': 'planner-session'}}],
                 'progress': {'next_step': 'Start worker'}}
        project_memory.record_task_state(self.root, state, 'PREFLIGHT', {'reason': 'Plan accepted'})
        decisions = project_memory._read_jsonl(self.root / '.ai-dev/memory/decisions.jsonl')
        events = project_memory._read_jsonl(self.root / '.ai-dev/memory/events.jsonl')
        self.assertEqual(decisions[0]['task_id'], 'task-2')
        self.assertEqual(decisions[0]['session_id'], 'planner-session')
        self.assertIn('runs/task-2/plan-1/codex.jsonl', decisions[0]['evidence_refs'])
        self.assertEqual(events[0]['type'], 'plan_accepted')

    def test_verified_checkpoint_completion_links_its_session_and_stage(self):
        state = {'id': 'task-4', 'status': 'RETRY', 'session_id': None,
                 'plan': {'schema': 2, 'steps': [{'id': 'step-2'}]}, 'checkpoint_index': 1,
                 'checkpoint_history': [{'id': 'step-1', 'session_id': 'finished-session'}],
                 'progress': {'next_step': 'Start the next stage'}}
        project_memory.record_task_state(self.root, state, 'RETRY', {'reason': 'Этап проверен. Перехожу к следующему.'})
        events = project_memory._read_jsonl(self.root / '.ai-dev/memory/events.jsonl')
        self.assertEqual(events[0]['type'], 'stage_completed')
        self.assertEqual(events[0]['stage_id'], 'step-1')
        self.assertEqual(events[0]['session_id'], 'finished-session')

    def test_bootstrap_records_only_accepted_optimization_milestones_and_debt(self):
        current = project_memory.bootstrap_optimization_roadmap(self.root)
        self.assertEqual(current['active_stage'], 'milestone-4')
        self.assertEqual(current['status'], 'IN_PROGRESS')
        project_memory.bootstrap_optimization_roadmap(self.root)
        decisions = project_memory._read_jsonl(self.root / '.ai-dev/memory/decisions.jsonl')
        events = project_memory._read_jsonl(self.root / '.ai-dev/memory/events.jsonl')
        self.assertEqual(len(decisions), 5)
        self.assertEqual(len(events), 6)
        debt = next(item for item in decisions if item['decision_id'].startswith('debt-'))
        self.assertEqual(debt['status'], 'deferred_low_priority')
        current = project_memory.complete_milestone_4(self.root)
        decisions = project_memory._read_jsonl(self.root / '.ai-dev/memory/decisions.jsonl')
        events = project_memory._read_jsonl(self.root / '.ai-dev/memory/events.jsonl')
        self.assertEqual(current['status'], 'COMPLETED')
        self.assertEqual(len(decisions), 6)
        self.assertEqual(events[-1]['event_id'], 'bootstrap-milestone-4-completed-unknown')

    def test_commit_event_links_task_and_refreshes_git_head(self):
        import subprocess
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.name', 'Memory Test'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.email', 'memory@example.invalid'], check=True)
        (self.root / 'source.txt').write_text('one\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'add', 'source.txt'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'initial'], check=True)
        (self.root / 'source.txt').write_text('two\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qam', 'task result'], check=True)
        commit = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        project_memory.record_commit(self.root, {'id': 'task-3', 'status': 'COMPLETED'})
        current = project_memory.read_current(self.root)
        event_rows = project_memory._read_jsonl(self.root / '.ai-dev/memory/events.jsonl')
        self.assertEqual(current['repo_head'], commit)
        self.assertEqual(event_rows[-1]['type'], 'commit_created')
        self.assertEqual(event_rows[-1]['task_id'], 'task-3')

    def test_legacy_state_without_memory_remains_readable_and_projects_safely(self):
        root = self.root / 'legacy-project'
        root.mkdir()
        write(root / '.ai-dev' / 'state.json', {'id': 'old-task', 'status': 'COMPLETED'})
        current = project_memory.project_current(root, read(root / '.ai-dev' / 'state.json'))
        self.assertEqual(current['status'], 'COMPLETED')
        self.assertIsNone(current['active_task'])


if __name__ == '__main__':
    unittest.main()
