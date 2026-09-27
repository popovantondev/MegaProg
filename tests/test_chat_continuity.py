import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_dev import chat_continuity as handoff, cli, project_memory
from ai_dev.storage import lock, read, write


class ChatContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        for key, value in (('user.name', 'Test'), ('user.email', 'test@example.invalid')):
            subprocess.run(['git', '-C', str(self.root), 'config', key, value], check=True)
        (self.root / '.gitignore').write_text('.ai-dev/\n')
        (self.root / 'code.py').write_text('original')
        (self.root / 'docs').mkdir()
        for name in handoff.DOCUMENTS:
            (self.root / name).write_text('# Важное решение\nПолный roadmap и следующий шаг\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'base'], check=True)
        project_memory.initialize(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def export(self):
        result = handoff.export(self.root)
        return result, read(result['snapshot'])

    def save_approved(self, status='PAUSED'):
        plan = {'schema_version': 1, 'plan_id': 'portable-plan', 'objective': 'Keep every requirement',
                'budget': {'max_model_turns': 2},
                'features': [{'id': 'f1', 'title': 'Feature', 'acceptance': ['Must work'],
                              'checks': [['python3', '-m', 'unittest']]}],
                'tasks': [{'id': 't1', 'feature_id': 'f1', 'title': 'Task', 'instructions': 'Do it',
                           'depends_on': [], 'allowed_paths': ['code.py'],
                           'checks': [['python3', '-m', 'unittest']]}]}
        directory = self.root / '.ai-dev/approved-plans/portable-plan'
        directory.mkdir(parents=True)
        (directory / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
        state = {'schema_version': 1, 'plan_id': 'portable-plan',
                 'plan_digest': handoff._digest(handoff._json(plan).encode('utf-8')),
                 'status': status,
                 'tasks': {'t1': {'status': 'PENDING'}},
                 'features': {'f1': {'status': 'PENDING'}}}
        (directory / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        return directory, plan, state

    def test_paused_approved_plan_is_transferred_as_reference(self):
        _, plan, _ = self.save_approved()
        result, bundle = self.export()
        self.assertTrue(bundle['transfer']['SAFE_TO_OPEN_NEW_CHAT'])
        self.assertEqual(bundle['approved_plans'][0]['requirements'], plan)
        self.assertEqual(bundle['approved_plans'][0]['remaining_tasks'], 1)
        for name in ('PLANNER_HANDOFF.md', 'NEW_CHAT_BOOTSTRAP.md'):
            text = (Path(result['output']) / name).read_text()
            self.assertIn('approved-plan resume portable-plan', text)
            self.assertIn('Keep every requirement', text)

    def test_running_approved_plan_is_not_safe(self):
        self.save_approved('RUNNING')
        _, bundle = self.export()
        self.assertFalse(bundle['transfer']['SAFE_TO_OPEN_NEW_CHAT'])

    def test_malformed_approved_plan_is_not_safe(self):
        directory, _, _ = self.save_approved()
        (directory / 'plan.json').write_text('{')
        result, bundle = self.export()
        self.assertFalse(bundle['transfer']['SAFE_TO_OPEN_NEW_CHAT'])
        self.assertTrue(any('Malformed approved-plan' in reason
                            for reason in bundle['transfer']['reasons']))
        self.assertIn('Причины остановки передачи',
                      (Path(result['output']) / 'PLANNER_HANDOFF.md').read_text())

    def test_approved_plan_digest_mismatch_is_not_safe(self):
        directory, _, state = self.save_approved()
        state['plan_digest'] = '0' * 64
        (directory / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        _, bundle = self.export()
        self.assertFalse(bundle['transfer']['SAFE_TO_OPEN_NEW_CHAT'])
        self.assertTrue(any('digest mismatch' in reason
                            for reason in bundle['transfer']['reasons']))

    def test_completed_approved_plan_with_pending_task_is_not_safe(self):
        self.save_approved('COMPLETED')
        _, bundle = self.export()
        self.assertFalse(bundle['transfer']['SAFE_TO_OPEN_NEW_CHAT'])
        self.assertTrue(any('contradicts' in reason
                            for reason in bundle['transfer']['reasons']))

    def test_approved_plan_bytes_are_bound_after_export(self):
        directory, _, _ = self.save_approved()
        _, bundle = self.export()
        (directory / 'state.json').write_text((directory / 'state.json').read_text() + ' ')
        self.assertEqual(handoff.check(self.root, bundle)['status'], 'STALE')

    def test_new_approved_plan_entry_makes_snapshot_stale(self):
        self.save_approved()
        _, bundle = self.export()
        (self.root / '.ai-dev/approved-plans/unexpected-file').write_text('invalid')
        self.assertEqual(handoff.check(self.root, bundle)['status'], 'STALE')

    def test_completed_approved_plan_keeps_requirements_in_handoff(self):
        directory, plan, state = self.save_approved('COMPLETED')
        state['tasks']['t1']['status'] = 'COMPLETED'
        state['features']['f1']['status'] = 'VERIFIED'
        (directory / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        result, bundle = self.export()
        self.assertEqual(bundle['approved_plans'][0]['remaining_tasks'], 0)
        self.assertEqual(bundle['approved_plans'][0]['requirements'], plan)
        handoff_text = (Path(result['output']) / 'PLANNER_HANDOFF.md').read_text()
        self.assertIn('Keep every requirement', handoff_text)
        self.assertNotIn('approved-plan resume portable-plan', handoff_text)

    def test_legacy_only_handoff_has_empty_approved_plans(self):
        _, bundle = self.export()
        self.assertEqual(bundle['approved_plans'], [])
        self.assertTrue(bundle['transfer']['SAFE_TO_OPEN_NEW_CHAT'])

    def test_portable_export_full_docs_and_no_model_or_monitor(self):
        with patch('ai_dev.codex.execute') as model, patch('ai_dev.native_monitor.auto_open') as monitor:
            result, bundle = self.export()
        model.assert_not_called()
        monitor.assert_not_called()
        self.assertTrue(bundle['transfer']['SAFE_TO_OPEN_NEW_CHAT'])
        for name in ('PLANNER_HANDOFF.md', 'NEW_CHAT_BOOTSTRAP.md'):
            text = (Path(result['output']) / name).read_text()
            for document in bundle['documents'].values():
                self.assertIn(document, text)
        self.assertEqual(handoff.check(self.root, bundle)['status'], 'FRESH')
        self.assertFalse((self.root / '.ai-dev/state.json').exists())

    def test_dirty_content_changes_without_head_change_are_stale(self):
        path = self.root / 'code.py'
        path.write_text('dirty one')
        _, bundle = self.export()
        path.write_text('dirty two')
        self.assertIn('source_digest', handoff.check(self.root, bundle)['changed'])

    def test_roadmap_and_decisions_changes_are_stale(self):
        _, bundle = self.export()
        (self.root / handoff.DOCUMENTS[0]).write_text('new roadmap')
        self.assertEqual(handoff.check(self.root, bundle)['status'], 'STALE')
        _, fresh = self.export()
        with (self.root / '.ai-dev/memory/decisions.jsonl').open('a') as stream:
            stream.write('{}\n')
        self.assertIn('inputs', handoff.check(self.root, fresh)['changed'])

    def test_tamper_rejected(self):
        _, bundle = self.export()
        bundle['task']['status'] = 'COMPLETED'
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            handoff.check(self.root, bundle)

    def test_active_stage_not_safe_and_goal_contract_preserved(self):
        state = {'id': 'existing', 'status': 'IMPLEMENT', 'prompt': 'цель ' * 1000,
                 'plan': {'steps': [{'title': 'проверить', 'acceptance': ['важное условие']}]}}
        write(self.root / '.ai-dev/state.json', state)
        _, bundle = self.export()
        self.assertFalse(bundle['transfer']['SAFE_TO_OPEN_NEW_CHAT'])
        self.assertEqual(bundle['task']['prompt'], state['prompt'])
        self.assertEqual(bundle['task']['plan'], state['plan'])
        self.assertEqual(read(self.root / '.ai-dev/state.json'), state)

    def test_missing_task_with_active_memory_not_safe(self):
        current = project_memory.read_current(self.root)
        current.update(active_task='lost', status='BLOCKED')
        write(self.root / '.ai-dev/memory/current.json', current)
        _, bundle = self.export()
        self.assertFalse(bundle['transfer']['SAFE_TO_OPEN_NEW_CHAT'])

    def test_missing_document_fails_instead_of_truncation(self):
        (self.root / handoff.DOCUMENTS[0]).unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.export()

    def test_oversized_document_fails(self):
        (self.root / handoff.DOCUMENTS[0]).write_bytes(b'a' * (handoff.MAX_INPUT + 1))
        with self.assertRaisesRegex(ValueError, 'too large'):
            self.export()

    def test_existing_output_not_overwritten(self):
        result, _ = self.export()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            handoff.export(self.root, result['output'])

    def test_source_output_path_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside repository'):
            handoff.export(self.root, self.root / 'docs/export')

    def test_lock_owner_not_stolen(self):
        with lock(self.root):
            with self.assertRaises(ValueError):
                self.export()

    def test_malformed_state_rejected(self):
        (self.root / '.ai-dev/state.json').write_text('{')
        with self.assertRaises(ValueError):
            self.export()

    def test_cli_export_and_check(self):
        output = Path(self.temp.name) / 'portable'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['-C', str(self.root), 'handoff', '--output', str(output)]), 0)
            self.assertEqual(cli.main(['-C', str(self.root), 'handoff', '--check', str(output / 'snapshot.json')]), 0)
            (self.root / 'code.py').write_text('changed')
            self.assertEqual(cli.main(['-C', str(self.root), 'handoff', '--check', str(output / 'snapshot.json')]), 2)

    def test_verification_log_change_is_stale_not_new_pass(self):
        write(self.root / '.ai-dev/state.json', {'id': 'task1', 'status': 'COMPLETED',
              'verification': {'ok': True, 'checks': [{'exit_code': 0}]}})
        directory = self.root / '.ai-dev/runs/task1'
        directory.mkdir(parents=True)
        (directory / 'verify-0.log').write_text('OK')
        _, bundle = self.export()
        self.assertEqual(bundle['verification_binding']['current_code_verified'], 'UNKNOWN')
        (directory / 'verify-0.log').write_text('changed')
        self.assertEqual(handoff.check(self.root, bundle)['status'], 'STALE')

    def test_foreign_root_is_stale(self):
        _, bundle = self.export()
        bundle['identity']['root'] = '/different/repo'
        bundle.pop('digest')
        bundle['digest'] = handoff._digest(handoff._json(bundle).encode())
        self.assertIn('root', handoff.check(self.root, bundle)['changed'])

    def test_symlink_document_rejected(self):
        path = self.root / handoff.DOCUMENTS[0]
        path.unlink()
        path.symlink_to(self.root / handoff.DOCUMENTS[1])
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.export()


if __name__ == '__main__':
    unittest.main()
