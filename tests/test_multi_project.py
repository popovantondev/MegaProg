import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from ai_dev.account_guard import AccountGuard, AccountGuardError
from ai_dev.supervisor import waiting_turn
from ai_dev.dashboard import dashboard
from ai_dev.cli import main


class MultiProjectTests(unittest.TestCase):
    def test_live_child_releases_slot_and_waiter_acquires(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {'account_guard': {'directory': folder, 'max_concurrent': 1, 'wait_seconds': 10}}
            script = ('import json,sys,time; from ai_dev.account_guard import AccountGuard; '
                      'g=AccountGuard(json.loads(sys.argv[1]), "gpt-6-luna", "medium").acquire(); '
                      'print("READY",flush=True); sys.stdin.readline(); g.release()')
            child = subprocess.Popen([sys.executable, '-c', script, json.dumps(config)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'READY')
                notifications = []
                def notify(evidence):
                    notifications.append(evidence)
                    child.stdin.write('\n'); child.stdin.flush()
                with waiting_turn(config, 'gpt-6-luna', 'medium', notify) as guard:
                    self.assertTrue(guard.evidence['acquired'])
                self.assertTrue(notifications)
                self.assertEqual(child.wait(timeout=10), 0)
                self.assertFalse(list(Path(folder).glob('*.json')))
            finally:
                if child.poll() is None:
                    child.kill(); child.wait()
                child.stdin.close(); child.stdout.close()

    def test_watch_stops_without_model_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            stop = Path(folder)/'stop'
            stop.touch()
            output = io.StringIO()
            with redirect_stdout(output), patch('ai_dev.codex.execute') as execute:
                self.assertEqual(main(['dashboard', '--root', folder, '--watch', '--stop-file', str(stop)]), 0)
                execute.assert_not_called()
            self.assertEqual(output.getvalue().count('MEGAPROG — THIS TASK'), 1)

    def test_model_body_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {'account_guard': {'directory': folder}}
            with self.assertRaisesRegex(ValueError, 'model body'):
                with waiting_turn(config, 'gpt-6-luna', 'medium', lambda e: self.fail()):
                    raise ValueError('model body')
            self.assertFalse(list(Path(folder).glob('*.json')))

    def test_unknown_pair_does_not_wait(self):
        with self.assertRaises(AccountGuardError):
            with waiting_turn({}, 'unknown', 'medium', lambda e: self.fail()):
                self.fail()

    def test_full_slot_timeout_does_not_remove_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {'account_guard': {'directory': folder, 'max_concurrent': 1, 'wait_seconds': 0}}
            with AccountGuard(config, 'gpt-6-luna', 'medium') as owner:
                with self.assertRaises(AccountGuardError):
                    with waiting_turn(config, 'gpt-6-luna', 'medium', lambda e: self.fail()):
                        self.fail()
                self.assertTrue(all(p.exists() for p in owner.paths))

    def test_dashboard_cli_reads_independent_projects(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder)/'A', Path(folder)/'B'
            for root, status in ((first, 'RUNNING'), (second, 'WAITING_FOR_SLOT')):
                (root/'.ai-dev').mkdir(parents=True)
                (root/'.ai-dev/state.json').write_text(json.dumps({'id':root.name, 'status':status}), encoding='utf-8')
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(['dashboard','--root',str(first),'--root',str(second),'--json']), 0)
            result = json.loads(out.getvalue())
            self.assertEqual(result['counts'], {'RUNNING':1, 'WAITING_FOR_SLOT':1})
            self.assertEqual(result['model_turns_started'], 0)
            (first/'.ai-dev/state.json').write_text('[]')
            self.assertEqual(dashboard([first])['counts'], {'ERROR':1})

class ConcurrencyTests(unittest.TestCase):
    def test_legacy_single_slot_can_be_changed_and_blocked_task_resumed(self):
        from ai_dev.concurrency import configure
        from ai_dev.storage import write, read
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {'model': 'auto', 'account_guard': {'max_concurrent': 1}}
            write(root/'.ai-dev/config.json', config)
            write(root/'.ai-dev/state.json', {'id': 'x', 'status': 'BLOCKED', 'attempts': 0, 'config': config})
            self.assertEqual(configure(root)['max_concurrent'], 1)
            result = configure(root, 11, 300)
            self.assertEqual(result['max_concurrent'], 11)
            state = read(root/'.ai-dev/state.json')
            self.assertEqual(state['config']['account_guard']['max_concurrent'], 11)
            self.assertEqual(state['config']['model'], 'auto')
            self.assertEqual(state['attempts'], 0)
            write(root/'.ai-dev/state.json', dict(state, status='RUNNING'))
            with self.assertRaises(ValueError):
                configure(root, 20)
            self.assertEqual(read(root/'.ai-dev/config.json')['account_guard']['max_concurrent'], 11)
