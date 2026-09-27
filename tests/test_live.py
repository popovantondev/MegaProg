import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from ai_dev.live import LiveSignal
from ai_dev.process import run
from ai_dev.storage import write
from ai_dev.dashboard import project_status, render, dashboard
from ai_dev import native_monitor


class LiveTests(unittest.TestCase):
    def test_real_child_reports_events_and_exit_without_output_leak(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / '.ai-dev/state.json', {'id': 'task1', 'status': 'RUNNING'})
            log = root / 'events.jsonl'
            signal = LiveSignal(root, log)
            observations = []
            original = signal.update
            def observe(*args, **kwargs):
                original(*args, **kwargs)
                p = root / '.ai-dev/live.json'
                if p.exists():
                    observations.append(json.loads(p.read_text(encoding='utf-8')))
            signal.update = observe
            code, _ = run([sys.executable, '-c',
                           'import json,time; print(json.dumps({"type":"item.started","item":{"type":"command_execution","command":"SECRET"}}),flush=True); time.sleep(2.3)'],
                          root, output_path=log, live=signal)
            self.assertEqual(code, 0)
            self.assertTrue(any(x['process_state'] == 'running' for x in observations))
            self.assertEqual(observations[-1]['process_state'], 'exited')
            self.assertEqual(observations[-1]['returncode'], 0)
            self.assertGreater(observations[-1]['events'], 0)
            self.assertNotIn('SECRET', json.dumps(observations))
            self.assertEqual(observations[-1]['observed_operation'], 'command_execution')
            self.assertIsNone(observations[-1]['last_result'])
            self.assertIn('процесс завершился', render(dashboard([root])))

    def test_waiting_display_bounds_owner_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / '.ai-dev/state.json', {
                'id': 'waiting', 'status': 'WAITING_FOR_SLOT', 'guard': {
                    'wait_seconds': 9, 'owners': [{'project': 'other', 'task_id': 't',
                    'model': 'gpt-5.6-luna', 'reasoning': 'low', 'held_seconds': 4,
                    'prompt': 'SECRET' * 1000}]}})
            view = render(dashboard([root]))
            self.assertIn('Ожидание слота: 0 мин 09 с', view)
            self.assertIn('Владелец слота: other | t | gpt-5.6-luna / low | удерживает 4с', view)
            self.assertNotIn('SECRET', view)

    def test_file_change_shows_only_project_relative_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / '.ai-dev/state.json', {'id': 'task1', 'status': 'RUNNING'})
            log = root / 'events.jsonl'
            log.write_text(json.dumps({'type': 'item.completed', 'item': {
                'type': 'file_change', 'changes': [
                    {'path': str(root.parent / 'private.txt')},
                    {'path': 'ai_dev/example.py'}]}}) + '\n', encoding='utf-8')
            signal = LiveSignal(root, log)
            signal.update(os.getpid(), force=True)
            text = render(dashboard([root]))
            self.assertIn('Файл: ai_dev/example.py', text)
            self.assertNotIn('private.txt', text)

    def test_signal_from_previous_task_is_not_used(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / '.ai-dev/state.json', {'id': 'new', 'status': 'RUNNING'})
            write(root / '.ai-dev/live.json', {'task_id': 'old', 'pid': os.getpid()})
            self.assertEqual(project_status(root)['live'], {})

    def test_verification_has_own_signal_not_old_model_activity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / '.ai-dev/state.json', {'id': 'task1', 'status': 'VERIFY'})
            log = root / 'events.jsonl'; log.write_text('')
            LiveSignal(root, log).update(os.getpid(), force=True)
            self.assertEqual(project_status(root)['live'], {})
            signal = LiveSignal(root, phase='VERIFY', activity='Проверка 1 из 1')
            signal.update(os.getpid(), force=True)
            current = project_status(root)
            self.assertTrue(current['live']['alive'])
            self.assertIn('Проверка 1 из 1', render({'projects': [current]}))

    def test_overview_discovers_projects_added_after_start_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            a, b = base / 'a', base / 'b'
            a.mkdir(); b.mkdir()
            with patch.object(native_monitor, 'registry_root', return_value=base / 'registry'):
                native_monitor.register(a)
                self.assertEqual(native_monitor.registered_projects(), [str(a.resolve())])
                native_monitor.register(b); native_monitor.register(a)
                self.assertEqual(set(native_monitor.registered_projects()), {str(a.resolve()), str(b.resolve())})
