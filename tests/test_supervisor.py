import json
import tempfile
import subprocess
import sys
from ai_dev.process import run
import unittest
from pathlib import Path
from unittest.mock import patch
from ai_dev import codex, supervisor
from ai_dev.cli import main
from ai_dev.storage import read, write, lock
from ai_dev.orchestration import approve, create_plan, create_task


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = dict(supervisor.DEFAULT, verify=[['python3', '-c', 'print("ok")']],
                           account_guard={'enabled': False}, strong_planning=False, max_attempts=2)
        write(self.root / '.ai-dev/config.json', self.config)
        def git(root, *args):
            if args == ('rev-parse', '--show-toplevel'):
                return str(root)
            if args == ('rev-parse', 'HEAD'):
                return 'head'
            return ''
        p = patch('ai_dev.supervisor.snapshot', return_value='snapshot')
        p.start()
        self.addCleanup(p.stop)

        p = patch('ai_dev.supervisor.git', side_effect=git)
        p.start()
        self.addCleanup(p.stop)
        p = patch('ai_dev.supervisor.discover', return_value={'models': [
            {'model': name, 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'medium'}]}
            for name in ['gpt-6-luna', 'gpt-6-sol', 'gpt-6-astra']
        ], 'limits': None})
        p.start()
        self.addCleanup(p.stop)
        p = patch('ai_dev.codex.doctor', return_value={'ready': True, 'resume': True, 'executable': 'codex'})
        p.start()
        self.addCleanup(p.stop)

    def test_progress_line_contains_live_task_status(self):
        state = {
            'id': 'task-1', 'created_at': 100.0, 'attempts': 1,
            'config': {'max_attempts': 2},
            'verification': {'checks': [{'status': 'PASS', 'exit_code': 0}]},
        }
        line = supervisor.progress_line(self.root, state, 'COMPLETED', now=125.0)
        self.assertIn('project=%s' % self.root.name, line)
        self.assertIn('task_id=task-1', line)
        self.assertIn('phase=COMPLETED', line)
        self.assertIn('attempt=1/2', line)
        self.assertIn('verify=PASS exit=0', line)
        self.assertIn('elapsed=25s', line)
        self.assertIn('report=', line)

    def test_progress_line_explains_pending_approval(self):
        state = {'id': 'task-2', 'created_at': 100.0, 'attempts': 0,
                 'config': {'max_attempts': 2}}
        line = supervisor.progress_line(self.root, state, 'PENDING_APPROVAL', now=101.0)
        self.assertIn('phase=PENDING_APPROVAL', line)
        self.assertIn('ожидание подтверждения пользователя', line)

    def test_waiting_slot_preserves_attempt_then_completes_with_evidence(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        count = [0]
        @contextmanager
        def slot(*args):
            count[0] += 1
            if count[0] == 1:
                raise supervisor.AccountGuardError('busy', {'reason': 'no_free_global_slot'})
            yield SimpleNamespace(evidence={'acquired': True})
        def wake(_):
            waiting = read(self.root / '.ai-dev/state.json')
            self.assertEqual(waiting['status'], 'WAITING_FOR_SLOT')
            self.assertEqual(waiting['attempts'], 0)
        with patch('ai_dev.supervisor.model_turn', side_effect=slot), \
             patch('ai_dev.supervisor.time.sleep', side_effect=wake), \
             patch('ai_dev.codex.execute', return_value={'ok': True, 'session_id': 'test'}):
            self.assertEqual(supervisor.task(self.root, 'solve'), 0)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['attempts'], 1)
        self.assertEqual(state['progress']['verify'], 'PASS')
        report = read(self.root / '.ai-dev/runs' / state['id'] / 'report.json')
        self.assertEqual(report['progress']['phase'], 'COMPLETED')
        events = (self.root / '.ai-dev/runs' / state['id'] / 'progress.jsonl').read_text()
        self.assertIn('WAITING_FOR_SLOT', events)

    def test_unchanged_slot_poll_notifies_once_before_acquisition(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        calls, notices = [0], []

        @contextmanager
        def slot(*args):
            calls[0] += 1
            if calls[0] < 3:
                raise supervisor.AccountGuardError('busy', {
                    'reason': 'no_free_global_slot', 'owners': [{
                        'project': 'other', 'task_id': 'task', 'model': 'gpt-6-luna',
                        'reasoning': 'low', 'held_seconds': calls[0]}]})
            yield SimpleNamespace(evidence={'acquired': True})

        with patch('ai_dev.supervisor.model_turn', side_effect=slot), \
             patch('ai_dev.supervisor.time.sleep'):
            with supervisor.waiting_turn({'account_guard': {'wait_seconds': 5}},
                                         'gpt-6-luna', 'low', notices.append):
                pass
        self.assertEqual(calls[0], 3)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['owners'][0]['project'], 'other')

    def test_cancel_wait_does_not_consume_attempt(self):
        from contextlib import contextmanager
        @contextmanager
        def slot(*args):
            raise supervisor.AccountGuardError('busy', {'reason': 'no_free_global_slot'})
            yield
        with patch('ai_dev.supervisor.model_turn', side_effect=slot), \
             patch('ai_dev.supervisor.time.sleep', side_effect=KeyboardInterrupt), \
             patch('ai_dev.codex.execute') as execute:
            self.assertEqual(supervisor.task(self.root, 'solve'), 1)
            execute.assert_not_called()
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['status'], 'BLOCKED')
        self.assertEqual(state['attempts'], 0)
        self.assertIsNone(state['session_id'])

    def test_account_guard_default_matches_tier_capacity(self):
        self.assertEqual(supervisor.DEFAULT['account_guard']['max_concurrent'], 49)
        self.assertEqual(supervisor.DEFAULT['account_guard']['tier_limits'],
                         {'cheap': 30, 'terra': 12, 'sol': 4, 'expensive': 3})
        write(self.root / '.ai-dev/config.json', {
            **self.config,
            'account_guard': {'enabled': True},
        })
        configured = supervisor.configure(self.root)
        self.assertEqual(configured['account_guard']['enabled'], True)

    def test_resume_accepts_matching_migrated_legacy_guard_snapshot(self):
        legacy_guard = {'enabled': False, 'max_concurrent': 11, 'stale_seconds': 3600,
                        'tier_limits': {'cheap': 5, 'terra': 3, 'sol': 2, 'expensive': 1}}
        legacy_config = dict(self.config, account_guard=legacy_guard)
        write(self.root / '.ai-dev/config.json', legacy_config)
        write(self.root / '.ai-dev/state.json', {
            'id': 'legacy-task', 'prompt': 'solve', 'status': 'BLOCKED', 'attempts': 0,
            'session_id': None, 'base_head': 'head', 'config': legacy_config,
            'created_at': 1, 'routing': {'kind': 'auto', 'model': None, 'reasoning': None},
            'decisions': [], 'routing_policy': 'legacy',
        })
        with patch('ai_dev.codex.execute', return_value={'ok': True, 'session_id': 's'}):
            self.assertEqual(supervisor.task(self.root), 0)
        self.assertEqual(read(self.root / '.ai-dev/config.json')['account_guard']['max_concurrent'], 49)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['config']['account_guard']['max_concurrent'], 49)
        self.assertEqual(state['config']['account_guard']['tier_limits']['expensive'], 3)

    def test_resume_rejects_genuine_guard_configuration_mismatch(self):
        state_config = dict(self.config, account_guard={'enabled': False, 'max_concurrent': 8})
        write(self.root / '.ai-dev/state.json', {
            'id': 'custom-task', 'prompt': 'solve', 'status': 'BLOCKED', 'attempts': 0,
            'session_id': None, 'base_head': 'head', 'config': state_config,
            'created_at': 1, 'routing': {'kind': 'auto', 'model': None, 'reasoning': None},
            'decisions': [], 'routing_policy': 'legacy',
        })
        with self.assertRaisesRegex(ValueError, 'Конфигурация изменилась'):
            supervisor.task(self.root)

    def test_verification_failure_retries_exact_session(self):
        def execute(root, config, report, prompt, log, session):
            if session is None:
                (root / 'answer').write_text('bad')
            else:
                self.assertEqual(session, 'session-1')
                (root / 'answer').write_text('good')
            return {'ok': True, 'session_id': 'session-1'}
        self.config['verify'] = [['python3', '-c', 'from pathlib import Path; assert Path("answer").read_text() == "good"']]
        write(self.root / '.ai-dev/config.json', self.config)
        with patch('ai_dev.codex.execute', side_effect=execute):
            self.assertEqual(supervisor.task(self.root, 'solve'), 0)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['status'], 'COMPLETED')
        self.assertEqual(state['attempts'], 2)

    def test_no_checks_blocks_before_model(self):
        self.config['verify'] = []
        write(self.root / '.ai-dev/config.json', self.config)
        with patch('ai_dev.codex.execute') as execute:
            self.assertEqual(supervisor.task(self.root, 'solve'), 1)
            execute.assert_not_called()

    def test_explicit_read_only_worker_failure_cannot_pass_old_tests(self):
        message = ("I couldn’t inspect or modify the repository: the environment is read-only, "
                   "and the initial file-list command was rejected by policy. No files were "
                   "changed, and I could not run the requested verification commands.")
        with patch('ai_dev.codex.execute', return_value={
                'ok': True, 'session_id': 'session-1', 'message': message}) as execute, \
             patch('ai_dev.supervisor.verify') as verify:
            self.assertEqual(supervisor.task(self.root, 'Add an export regression test'), 1)
        execute.assert_called_once()
        verify.assert_not_called()
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['status'], 'BLOCKED')
        self.assertEqual(state['last_failure'], 'worker_access')
        self.assertIn('NO_CHANGE', state['reason'])
        report = read(self.root / '.ai-dev/runs' / state['id'] / 'report.json')
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertIn('No files were changed', report['codex']['message'])

    def test_worker_access_failure_requires_explicit_unfinished_result(self):
        self.assertFalse(supervisor.worker_access_failure(
            'The environment was read-only at first, but I fixed access and changed the file.'))
        self.assertTrue(supervisor.worker_access_failure(
            'I could not access the repository. No files were modified.'))

    def test_guard_block_is_not_recorded_as_a_model_attempt(self):
        self.config['account_guard']['wait_seconds'] = 0
        write(self.root / '.ai-dev/config.json', self.config)
        class DeniedTurn:
            def __enter__(self):
                raise supervisor.AccountGuardError('slot unavailable', {
                    'acquired': False, 'reason': 'no_free_tier_slot'})

            def __exit__(self, *args):
                return False

        with patch('ai_dev.supervisor.model_turn', return_value=DeniedTurn()):
            with patch('ai_dev.codex.execute') as execute:
                self.assertEqual(supervisor.task(self.root, 'solve'), 1)
                execute.assert_not_called()
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(state['attempts'], 0)
        self.assertNotIn('attempt', state['decisions'][0])

    def test_orchestration_model_turn_requires_explicit_approval(self):
        plan = create_plan(self.root, 'approved execution', ['answer'], [['python3', '-c', 'print(1)']])
        task = create_task(self.root, plan['plan_id'], 'solve', task_id='orch-task')
        with patch('ai_dev.codex.execute') as execute:
            self.assertEqual(supervisor.task(self.root, 'solve', orchestration_task_id=task['task_id']), 1)
            execute.assert_not_called()
        self.assertEqual(read(self.root / '.ai-dev/state.json')['status'], 'PENDING_APPROVAL')
        approve(self.root, 'model_turn', 'owner', task_id=task['task_id'])
        with patch('ai_dev.codex.execute', return_value={'ok': True, 'session_id': 's'}):
            with patch('ai_dev.supervisor.verify', return_value={'ok': True}):
                self.assertEqual(supervisor.task(self.root), 0)

    def test_attempt_budget_survives_resume(self):
        with patch('ai_dev.codex.execute', return_value={'ok': True, 'session_id': 's'}) as execute:
            with patch('ai_dev.supervisor.verify', return_value={'ok': False}):
                self.assertEqual(supervisor.task(self.root, 'solve'), 1)
                self.assertEqual(supervisor.task(self.root), 1)
                self.assertEqual(execute.call_count, 2)

    def test_failed_model_is_not_success_or_auto_retry(self):
        with patch('ai_dev.codex.execute', return_value={'ok': False, 'session_id': 's'}) as execute:
            self.assertEqual(supervisor.task(self.root, 'solve'), 1)
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(read(self.root / '.ai-dev/state.json')['status'], 'BLOCKED')

    def test_context_window_failure_restarts_without_rerouting(self):
        sessions = []
        def execute(root, config, report, prompt, log, session):
            sessions.append(session)
            if len(sessions) == 1:
                return {'ok': False, 'session_id': 'old',
                        'error': {'type': 'error', 'message': 'ContextWindowExceeded'}}
            return {'ok': True, 'session_id': 'new'}
        with patch('ai_dev.codex.execute', side_effect=execute):
            with patch('ai_dev.supervisor.verify', return_value={'ok': True}):
                self.assertEqual(supervisor.task(self.root, 'solve'), 0)
        state = read(self.root / '.ai-dev/state.json')
        self.assertEqual(sessions, [None, None])
        self.assertEqual(state['compaction']['compaction_reason'], 'ContextWindowExceeded')
        self.assertEqual(state['compaction']['new_session_id'], 'new')
        self.assertEqual(state['decisions'][0]['model'], state['decisions'][1]['model'])

    def test_safety_flags_apply_to_resume(self):
        for session in (None, 's'):
            command = codex.command('codex', dict(self.config, model='gpt-6-luna', reasoning='medium'), session)
            self.assertIn('forced_login_method="chatgpt"', command)
            self.assertIn('service_tier="default"', command)
            self.assertIn('default_permissions=":workspace"', command)
            self.assertNotIn('sandbox_mode="workspace-write"', command)
            self.assertIn('--ignore-user-config', command)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)

    def test_read_only_profile_is_not_upgraded(self):
        command = codex.command('codex', dict(self.config, model='gpt-6-luna', reasoning='medium'),
                                sandbox='read-only')
        self.assertIn('default_permissions=":read-only"', command)
        self.assertNotIn('default_permissions=":workspace"', command)

    def test_full_access_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            codex.command('codex', dict(self.config, model='gpt-6-luna', reasoning='medium'),
                          sandbox='danger-full-access')

    def test_windows_worker_full_access_is_explicit_and_review_stays_read_only(self):
        config = dict(self.config, model='gpt-6-luna', reasoning='low',
                      windows_worker_full_access=True)
        with patch('ai_dev.codex.os.name', 'nt'):
            for session in (None, 'saved-session'):
                worker = codex.command('codex', config, session=session)
                self.assertIn('--dangerously-bypass-approvals-and-sandbox', worker)
                self.assertNotIn('default_permissions=":workspace"', worker)
                self.assertIn('approval_policy="never"', worker)
                planner = codex.command('codex', config, session=session, sandbox='read-only')
                self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', planner)
                self.assertIn('default_permissions=":read-only"', planner)
                task_planner = codex.command('codex', dict(config, _megaprog_task_planner=True),
                                             session=session, sandbox='read-only')
                self.assertIn('--dangerously-bypass-approvals-and-sandbox', task_planner)
        with patch('ai_dev.codex.os.name', 'posix'):
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', codex.command('codex', config))

    def test_worker_access_command_persists_and_rolls_back(self):
        with patch('ai_dev.cli.sys.platform', 'win32'):
            self.assertEqual(main(['-C', str(self.root), 'worker-access', 'enable']), 0)
            self.assertTrue(read(self.root / '.ai-dev/config.json')['windows_worker_full_access'])
            self.assertEqual(main(['-C', str(self.root), 'worker-access', 'disable']), 0)
            self.assertFalse(read(self.root / '.ai-dev/config.json')['windows_worker_full_access'])

    def test_total_model_turn_budget_counts_planner_and_worker(self):
        state = {'planning_attempts': [{}], 'attempts': 1}
        with self.assertRaisesRegex(ValueError, 'Лимит model turns исчерпан'):
            supervisor.ensure_turn_budget(state, {'max_model_turns': 2})
        supervisor.ensure_turn_budget({'planning_attempts': [{}], 'attempts': 0},
                                      {'max_model_turns': 2})

    def test_exit_zero_without_completed_event_is_failure(self):
        with patch('ai_dev.codex.run', return_value=(0, '{"type":"thread.started","thread_id":"s"}\n')):
            result = codex.execute(self.root, dict(self.config, model='gpt-6-luna', reasoning='medium'),
                                   {'executable': 'codex'}, 'hi', self.root / 'log')
        self.assertFalse(result['ok'])
        self.assertEqual(result['session_id'], 's')

    def test_api_environment_removed(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'secret', 'CODEX_API_KEY': 'secret'}):
            self.assertNotIn('OPENAI_API_KEY', codex.environment())
            self.assertNotIn('CODEX_API_KEY', codex.environment())

    def test_init_is_idempotent(self):
        self.assertEqual(main(['-C', str(self.root), 'init']), 0)
        self.assertEqual(main(['-C', str(self.root), 'init']), 0)
        self.assertEqual(read(self.root / '.ai-dev/config.json'), self.config)
        self.assertEqual((self.root / '.gitignore').read_text(), '.ai-dev/\n')

    def test_real_verification_failure(self):
        self.config['verify'] = [['python3', '-c', 'raise SystemExit(7)']]
        result = supervisor.verify(self.root, self.config, self.root)
        self.assertFalse(result['ok'])
        self.assertEqual(result['checks'][0]['exit_code'], 7)

    def test_timeout_preserves_output(self):
        log = self.root / 'timeout.log'
        with self.assertRaises(subprocess.TimeoutExpired):
            run([sys.executable, '-c', 'import time; print("started", flush=True); time.sleep(10)'],
                self.root, timeout=0.2, output_path=log)
        self.assertIn('started', log.read_text())

    def test_dirty_repository_blocks_before_model(self):
        with patch('ai_dev.supervisor.git', side_effect=lambda root, *args:
                   str(root) if args == ('rev-parse', '--show-toplevel') else ' M user.py'):
            with self.assertRaises(ValueError):
                supervisor.task(self.root, 'solve')

    def test_changed_head_blocks_resume(self):
        with patch('ai_dev.codex.execute', return_value={'ok': False, 'session_id': 's'}):
            supervisor.task(self.root, 'solve')
        with patch('ai_dev.supervisor.git', side_effect=lambda root, *args:
                   str(root) if args == ('rev-parse', '--show-toplevel') else 'new-head'):
            with self.assertRaises(ValueError):
                supervisor.task(self.root)

    def test_resume_after_network_failure_does_not_escalate_again(self):
        self.config['max_attempts'] = 3
        write(self.root / '.ai-dev/config.json', self.config)
        models = []
        def execute(root, config, report, prompt, log, session):
            models.append(config['model'])
            return {'ok': len(models) != 2, 'session_id': 'session'}
        with patch('ai_dev.codex.execute', side_effect=execute):
            with patch('ai_dev.supervisor.verify', side_effect=[{'ok': False}, {'ok': True}]):
                self.assertEqual(supervisor.task(self.root, 'normal task'), 1)
                self.assertEqual(supervisor.task(self.root), 0)
        self.assertEqual(models, ['gpt-6-luna', 'gpt-6-sol', 'gpt-6-sol'])

    def test_resume_after_infrastructure_failure_does_not_escalate_again(self):
        self.config['max_attempts'] = 3
        write(self.root / '.ai-dev/config.json', self.config)
        models = []
        def execute(root, config, report, prompt, log, session):
            models.append(config['model'])
            if len(models) == 2:
                raise OSError('connection reset')
            return {'ok': True, 'session_id': 'session'}
        with patch('ai_dev.codex.execute', side_effect=execute):
            with patch('ai_dev.supervisor.verify', side_effect=[{'ok': False}, {'ok': True}]):
                self.assertEqual(supervisor.task(self.root, 'normal task'), 1)
                self.assertEqual(supervisor.task(self.root), 0)
        self.assertEqual(models, ['gpt-6-luna', 'gpt-6-sol', 'gpt-6-sol'])
        self.assertEqual(read(self.root / '.ai-dev/state.json')['last_failure'], None)

    def test_concurrent_lock_rejected(self):
        with lock(self.root):
            with self.assertRaises(ValueError):
                with lock(self.root):
                    pass


if __name__ == '__main__':
    unittest.main()
