import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest.mock import patch

from ai_dev import approved_plan
from ai_dev.account_guard import AccountGuardError


class ApprovedPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.check_call(['git', 'init', '-q'], cwd=str(self.root))
        (self.root / 'README.md').write_text('test project\n')
        self.plan = {
            'schema_version': 1, 'plan_id': 'two-functions', 'objective': 'Build two small calculator functions',
            'budget': {'max_model_turns': 3},
            'features': [
                {'id': 'add', 'title': 'Add', 'acceptance': ['add(2, 3) returns 5'],
                 'checks': [[sys.executable, '-c', 'import calculator; assert calculator.add(2,3)==5']]},
                {'id': 'multiply', 'title': 'Multiply', 'acceptance': ['multiply(2, 3) returns 6'],
                 'checks': [[sys.executable, '-c', 'import calculator; assert calculator.multiply(2,3)==6']]},
            ],
            'tasks': [
                {'id': 'add-task', 'feature_id': 'add', 'title': 'Implement add',
                 'instructions': 'Create calculator.add', 'depends_on': [],
                 'allowed_paths': ['calculator.py'],
                 'checks': [[sys.executable, '-c', 'import calculator; assert calculator.add(2,3)==5']]},
                {'id': 'multiply-task', 'feature_id': 'multiply', 'title': 'Implement multiply',
                 'instructions': 'Add calculator.multiply', 'depends_on': ['add-task'],
                 'allowed_paths': ['calculator.py'],
                 'checks': [[sys.executable, '-c', 'import calculator; assert calculator.multiply(2,3)==6']]},
            ],
        }
        self.plan_path = self.root / 'approved-plan.json'
        self.plan_path.write_text(json.dumps(self.plan))

    def _fake_turns(self, side_effect):
        catalog = {'models': [{'model': 'gpt-6-luna',
                               'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}],
                   'limits': None, 'errors': {}}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(approved_plan.codex, 'doctor', return_value={'ready': True, 'executable': 'codex'}))
        stack.enter_context(patch.object(approved_plan, 'discover', return_value=catalog))
        stack.enter_context(patch.object(approved_plan, 'model_turn', return_value=contextlib.nullcontext()))
        return stack.enter_context(patch.object(approved_plan.codex, 'execute', side_effect=side_effect))

    def _result(self, log, session):
        log.write_text(json.dumps({'type': 'thread.started', 'thread_id': session}) + '\n' +
                       json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}}) + '\n')
        return {'ok': True, 'session_id': session, 'usage': {'input_tokens': 10, 'output_tokens': 2},
                'telemetry': {'duration_seconds': 0.01}}

    def test_pause_new_process_resume_keeps_first_feature_and_no_repeat(self):
        calls = []
        def execute(root, config, doctor, prompt, log, session=None):
            calls.append((prompt, session))
            source = self.root / 'calculator.py'
            if len(calls) == 1:
                source.write_text('def add(a, b):\n    return a + b\n')
            else:
                source.write_text(source.read_text() + '\ndef multiply(a, b):\n    return a * b\n')
            return self._result(log, 'session-%d' % len(calls))
        self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        first = approved_plan.run_plan(self.root, 'two-functions', max_tasks=1)
        self.assertEqual(first['status'], 'PAUSED')
        self.assertEqual(first['features']['add']['status'], 'VERIFIED')
        self.assertEqual(first['tasks']['multiply-task']['status'], 'PENDING')
        self.assertTrue(first['features']['add']['proof']['checks'])
        # load/run after the first call represents a fresh CLI process.
        second = approved_plan.run_plan(self.root, 'two-functions')
        self.assertEqual(second['status'], 'COMPLETED')
        self.assertEqual([x['status'] for x in second['features'].values()], ['VERIFIED', 'VERIFIED'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(second['model_turns'], 2)
        progress = approved_plan.summary(self.root, 'two-functions')
        self.assertEqual(progress['remaining_tasks'], 0)
        self.assertIsNone(progress['current_task'])
        self.assertEqual(approved_plan.run_plan(self.root, 'two-functions')['model_turns'], 2)
        self.assertEqual(len(calls), 2)

    def test_failed_source_preflight_leaves_plan_id_available_for_retry(self):
        base = self.root / '.ai-dev/approved-plans/two-functions'
        with patch.object(approved_plan, '_source_signature',
                          side_effect=ValueError('source diff exceeds limit')):
            with self.assertRaisesRegex(ValueError, 'source diff exceeds limit'):
                approved_plan.import_plan(self.root, self.plan_path)
        self.assertFalse(base.exists())
        state = approved_plan.import_plan(self.root, self.plan_path)
        self.assertEqual(state['status'], 'READY')

    def test_git_root_comparison_accepts_canonical_unicode_path_forms(self):
        root = self.root / 'Учебный-проект'
        root.mkdir()
        subprocess.check_call(['git', 'init', '-q'], cwd=str(root))
        resolved = str(root.resolve())
        alternate_form = unicodedata.normalize(
            'NFD' if unicodedata.normalize('NFC', resolved) == resolved else 'NFC',
            resolved,
        )
        original_git = approved_plan._git

        def git_with_alternate_root(path, *args):
            if args == ('rev-parse', '--show-toplevel'):
                return alternate_form.encode('utf-8') + b'\n'
            return original_git(path, *args)

        with patch.object(approved_plan, '_git', side_effect=git_with_alternate_root):
            state = approved_plan.import_plan(root, self.plan_path)
        self.assertEqual(state['status'], 'READY')

    def test_failed_check_preserves_patch_and_full_log(self):
        def execute(root, config, doctor, prompt, log, session=None):
            (self.root / 'calculator.py').write_text('def add(a, b):\n    return -1\n')
            return self._result(log, 'session-bad')
        self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        state = approved_plan.run_plan(self.root, 'two-functions')
        self.assertEqual(state['status'], 'BLOCKED')
        self.assertEqual(state['tasks']['add-task']['checks'][0]['exit_code'], 1)
        self.assertTrue((self.root / 'calculator.py').exists())
        self.assertTrue((self.root / state['tasks']['add-task']['checks'][0]['log']).is_file())
        self.assertEqual(state['features']['add']['status'], 'PENDING')

    def test_out_of_scope_change_blocks_without_discarding_evidence(self):
        def execute(root, config, doctor, prompt, log, session=None):
            (self.root / 'outside.py').write_text('unexpected = True\n')
            return self._result(log, 'session-scope')
        self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        state = approved_plan.run_plan(self.root, 'two-functions')
        self.assertEqual(state['status'], 'BLOCKED')
        self.assertIn('outside.py', state['reason'])
        self.assertTrue((self.root / 'outside.py').exists())

    def test_oversized_context_blocks_before_attempt_or_model_turn(self):
        (self.root / 'large.txt').write_text('x' * (approved_plan.MAX_CONTEXT_BYTES + 1))
        self.plan['tasks'][0]['context_paths'] = ['large.txt']
        self.plan_path.write_text(json.dumps(self.plan))
        execute = self._fake_turns(lambda *args, **kwargs: self.fail('worker must not start'))
        approved_plan.import_plan(self.root, self.plan_path)
        state = approved_plan.run_plan(self.root, 'two-functions')
        self.assertEqual(state['status'], 'BLOCKED')
        self.assertIn('preflight', state['reason'])
        self.assertEqual(state['model_turns'], 0)
        self.assertEqual(state['tasks']['add-task']['attempts'], 0)
        execute.assert_not_called()

    def test_external_drift_can_be_reconciled_with_checks_and_no_new_turn(self):
        def execute(root, config, doctor, prompt, log, session=None):
            (self.root / 'calculator.py').write_text('def add(a, b):\n    return a + b\n')
            (self.root / 'outside.py').write_text('other task\n')
            return self._result(log, 'session-add')
        worker = self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        blocked = approved_plan.run_plan(self.root, 'two-functions')
        self.assertEqual(blocked['status'], 'BLOCKED')
        self.assertIn('author unknown', blocked['reason'])
        with self.assertRaisesRegex(ValueError, 'External paths differ'):
            approved_plan.reconcile_plan(self.root, 'two-functions', ['wrong.py'])
        result = approved_plan.reconcile_plan(self.root, 'two-functions', ['outside.py'])
        self.assertEqual(result['status'], 'PAUSED')
        self.assertEqual(result['tasks']['add-task']['status'], 'COMPLETED')
        self.assertEqual(result['features']['add']['status'], 'VERIFIED')
        self.assertEqual(result['model_turns'], 1)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(result['tasks']['add-task']['reconciliation']['accepted_external_paths'], ['outside.py'])

    def test_legacy_external_drift_requires_explicit_current_state_review(self):
        def execute(root, config, doctor, prompt, log, session=None):
            (self.root / 'calculator.py').write_text('def add(a, b):\n    return a + b\n')
            (self.root / 'outside.py').write_text('other task\n')
            return self._result(log, 'session-add')
        worker = self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        blocked = approved_plan.run_plan(self.root, 'two-functions')
        entry = blocked['tasks']['add-task']
        entry.pop('external_drift')
        blocked['reason'] = 'Worker changed files outside allowed paths: outside.py'
        base = self.root / '.ai-dev/approved-plans/two-functions'
        approved_plan._save(base, blocked)
        with self.assertRaisesRegex(ValueError, 'Legacy block'):
            approved_plan.reconcile_plan(self.root, 'two-functions', ['outside.py'])
        result = approved_plan.reconcile_plan(self.root, 'two-functions', ['outside.py'],
                                              accept_current_state=True)
        self.assertEqual(result['tasks']['add-task']['status'], 'COMPLETED')
        self.assertEqual(worker.call_count, 1)

    def test_reconcile_rejects_approved_patch_changed_after_block(self):
        def execute(root, config, doctor, prompt, log, session=None):
            (self.root / 'calculator.py').write_text('def add(a, b):\n    return a + b\n')
            (self.root / 'outside.py').write_text('other task\n')
            return self._result(log, 'session-add')
        worker = self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        blocked = approved_plan.run_plan(self.root, 'two-functions')
        self.assertEqual(blocked['status'], 'BLOCKED')
        (self.root / 'calculator.py').write_text('def add(a, b):\n    return -1\n')
        with self.assertRaisesRegex(ValueError, 'patch changed'):
            approved_plan.reconcile_plan(self.root, 'two-functions', ['outside.py'])
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(approved_plan.load(self.root, 'two-functions')[1]['status'], 'BLOCKED')

    def test_reconcile_check_failure_never_starts_a_new_worker(self):
        def execute(root, config, doctor, prompt, log, session=None):
            (self.root / 'calculator.py').write_text('def add(a, b):\n    return -1\n')
            (self.root / 'outside.py').write_text('other task\n')
            return self._result(log, 'session-add')
        worker = self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        blocked = approved_plan.run_plan(self.root, 'two-functions')
        self.assertEqual(blocked['status'], 'BLOCKED')
        result = approved_plan.reconcile_plan(self.root, 'two-functions', ['outside.py'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertIn('verification failed', result['reason'])
        self.assertEqual(result['model_turns'], 1)
        self.assertEqual(worker.call_count, 1)

    def test_external_drift_during_verification_can_be_rechecked_without_model(self):
        def execute(root, config, doctor, prompt, log, session=None):
            (self.root / 'calculator.py').write_text('def add(a, b):\n    return a + b\n')
            return self._result(log, 'session-add')
        worker = self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        original = approved_plan.run_command
        calls = [0]
        def check(command, root, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                (self.root / 'outside.py').write_text('concurrent update\n')
            return original(command, root, **kwargs)
        with patch.object(approved_plan, 'run_command', side_effect=check):
            blocked = approved_plan.run_plan(self.root, 'two-functions')
        self.assertEqual(blocked['status'], 'BLOCKED')
        self.assertIn('during task verification', blocked['reason'])
        result = approved_plan.reconcile_plan(self.root, 'two-functions', ['outside.py'])
        self.assertEqual(result['tasks']['add-task']['status'], 'COMPLETED')
        self.assertEqual(result['model_turns'], 1)
        self.assertEqual(worker.call_count, 1)

    def test_failed_check_resumes_same_session_and_preserves_first_log(self):
        sessions = []
        def execute(root, config, doctor, prompt, log, session=None):
            sessions.append(session)
            value = '-1' if len(sessions) == 1 else 'a + b'
            (self.root / 'calculator.py').write_text('def add(a, b):\n    return ' + value + '\n')
            return self._result(log, 'session-add')
        self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        blocked = approved_plan.run_plan(self.root, 'two-functions')
        first_log = self.root / blocked['tasks']['add-task']['checks'][0]['log']
        self.assertEqual(blocked['status'], 'BLOCKED')
        resumed = approved_plan.run_plan(self.root, 'two-functions', max_tasks=1, resume=True)
        self.assertEqual(resumed['status'], 'PAUSED')
        self.assertEqual(sessions, [None, 'session-add'])
        self.assertTrue(first_log.is_file())
        self.assertEqual(resumed['tasks']['add-task']['attempts'], 2)
        self.assertEqual(resumed['model_turns'], 2)

    def test_resume_after_completed_turn_log_skips_duplicate_model_call(self):
        self._fake_turns(lambda *args, **kwargs: self.fail('must not repeat model turn'))
        state = approved_plan.import_plan(self.root, self.plan_path)
        base = self.root / '.ai-dev/approved-plans/two-functions'
        log = base / 'logs/add-task-turn-1.jsonl'
        log.parent.mkdir()
        self._result(log, 'session-interrupted')
        (self.root / 'calculator.py').write_text('def add(a, b):\n    return a + b\n')
        entry = state['tasks']['add-task']
        prior_paths = approved_plan._status_paths(self.root) - {'calculator.py'}
        entry.update(status='RUNNING', attempts=1, model_log=str(log.relative_to(self.root)),
                     before_paths=sorted(prior_paths), before_other_hashes={
                         path: approved_plan._file_digest(self.root / path) for path in prior_paths})
        state['status'], state['model_turns'] = 'RUNNING', 1
        approved_plan._save(base, state)
        result = approved_plan.run_plan(self.root, 'two-functions', max_tasks=1)
        self.assertEqual(result['status'], 'PAUSED')
        self.assertEqual(result['tasks']['add-task']['status'], 'COMPLETED')
        self.assertEqual(result['model_turns'], 1)

    def test_reject_cycle_and_changed_immutable_plan(self):
        self.plan['tasks'][0]['depends_on'] = ['multiply-task']
        with self.assertRaisesRegex(ValueError, 'cycle'):
            approved_plan.validate_plan(self.plan)
        self.plan['tasks'][0]['depends_on'] = []
        approved_plan.import_plan(self.root, self.plan_path)
        self.plan['objective'] = 'different plan under same ID'
        self.plan_path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(ValueError, 'different requirements'):
            approved_plan.import_plan(self.root, self.plan_path)

    def test_plan_turn_budget_stops_before_second_task(self):
        self.plan['budget']['max_model_turns'] = 1
        self.plan_path.write_text(json.dumps(self.plan))
        calls = []
        def execute(root, config, doctor, prompt, log, session=None):
            calls.append(session)
            (self.root / 'calculator.py').write_text('def add(a, b):\n    return a + b\n')
            return self._result(log, 'session-add')
        self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        result = approved_plan.run_plan(self.root, 'two-functions')
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['tasks']['add-task']['status'], 'COMPLETED')
        self.assertEqual(result['tasks']['multiply-task']['attempts'], 0)
        self.assertEqual(len(calls), 1)
        self.assertIn('budget exhausted', result['reason'])

    def test_account_slot_refusal_spends_no_model_turn(self):
        def execute(root, config, doctor, prompt, log, session=None):
            (self.root / 'calculator.py').write_text('def add(a, b):\n    return a + b\n')
            return self._result(log, 'session-add')
        self._fake_turns(execute)
        approved_plan.import_plan(self.root, self.plan_path)
        with patch.object(approved_plan, 'model_turn', side_effect=AccountGuardError('no slot', {})):
            blocked = approved_plan.run_plan(self.root, 'two-functions')
        self.assertEqual(blocked['status'], 'BLOCKED')
        self.assertEqual(blocked['model_turns'], 0)
        self.assertEqual(blocked['tasks']['add-task']['attempts'], 0)
        continued = approved_plan.run_plan(self.root, 'two-functions', max_tasks=1, resume=True)
        self.assertEqual(continued['status'], 'PAUSED')
        self.assertEqual(continued['tasks']['add-task']['attempts'], 1)


if __name__ == '__main__':
    unittest.main()
