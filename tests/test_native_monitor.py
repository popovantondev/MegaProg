import os
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from ai_dev import native_monitor
from ai_dev import native_monitor_runner
from ai_dev.storage import lock


class NativeMonitorTests(unittest.TestCase):
    def test_monitor_lock_is_separate_and_released(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with lock(root):
                self.assertFalse(native_monitor.running(root))
                with lock(native_monitor.monitor_root(root)):
                    self.assertTrue(native_monitor.running(root))
                    with patch.object(native_monitor.sys, 'platform', 'darwin'), patch.object(native_monitor.subprocess, 'run') as run:
                        self.assertEqual(native_monitor.launch(root), 'REUSED')
                        run.assert_not_called()
                self.assertFalse(native_monitor.running(root))

    def test_mac_paths_are_shell_quoted(self):
        with tempfile.TemporaryDirectory(prefix="mp ' $(literal) ") as folder:
            with patch.object(native_monitor.sys, 'platform', 'darwin'), patch.object(native_monitor, 'running', side_effect=[False, True]), patch.object(native_monitor, 'is_ready', return_value=True), patch.object(native_monitor.subprocess, 'run') as run:
                self.assertEqual(native_monitor.launch(Path(folder)), 'STARTED')
                args = run.call_args.args[0]
                self.assertEqual(args[:2], ['/usr/bin/osascript', '-e'])
                self.assertIn('tell application "Terminal"', args[2])
                self.assertIn('$(literal)', args[2])

    def test_native_windows_override_inherited_headless_no_color(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'NO_COLOR': '1', 'MEGAPROG_COLOR': 'never'}), \
                 patch.object(native_monitor.sys, 'platform', 'win32'), \
                 patch.object(native_monitor, 'running', side_effect=[False, True]), \
                 patch.object(native_monitor, 'is_ready', return_value=True), \
                 patch.object(native_monitor.subprocess, 'CREATE_NEW_CONSOLE', 16, create=True), \
                 patch.object(native_monitor.subprocess, 'Popen') as popen:
                self.assertEqual(native_monitor.launch(Path(folder)), 'STARTED')
                env = popen.call_args.kwargs['env']
                self.assertNotIn('NO_COLOR', env)
                self.assertEqual(env['MEGAPROG_COLOR'], 'always')
                self.assertEqual(os.environ['NO_COLOR'], '1')

    def test_native_mac_removes_inherited_headless_no_color(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'NO_COLOR': '1', 'MEGAPROG_COLOR': 'never'}), \
                 patch.object(native_monitor.sys, 'platform', 'darwin'), \
                 patch.object(native_monitor, 'running', side_effect=[False, True]), \
                 patch.object(native_monitor, 'is_ready', return_value=True), \
                 patch.object(native_monitor.subprocess, 'run') as run:
                self.assertEqual(native_monitor.launch(Path(folder)), 'STARTED')
                script = run.call_args.args[0][2]
                self.assertIn('-u NO_COLOR', script)
                self.assertIn('MEGAPROG_COLOR=always', script)
                self.assertIn('MEGAPROG_THEME=light', script)

    def test_explicit_native_no_color_remains_available(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'NO_COLOR': '1', 'MEGAPROG_MONITOR_NO_COLOR': '1'}), \
                 patch.object(native_monitor.sys, 'platform', 'win32'), \
                 patch.object(native_monitor, 'running', side_effect=[False, True]), \
                 patch.object(native_monitor, 'is_ready', return_value=True), \
                 patch.object(native_monitor.subprocess, 'CREATE_NEW_CONSOLE', 16, create=True), \
                 patch.object(native_monitor.subprocess, 'Popen') as popen:
                self.assertEqual(native_monitor.launch(Path(folder)), 'STARTED')
                self.assertEqual(popen.call_args.kwargs['env']['NO_COLOR'], '1')

    def test_windows_paths_are_data_not_cmd_source(self):
        with tempfile.TemporaryDirectory(prefix='mp & test ') as folder:
            with patch.object(native_monitor.sys, 'platform', 'win32'), patch.object(native_monitor, 'running', side_effect=[False, True]), patch.object(native_monitor, 'is_ready', return_value=True), patch.object(native_monitor.subprocess, 'CREATE_NEW_CONSOLE', 16, create=True), patch.object(native_monitor.subprocess, 'Popen') as popen:
                self.assertEqual(native_monitor.launch(Path(folder)), 'STARTED')
                command = popen.call_args.args[0]
                self.assertNotIn(folder, command)
                self.assertIn('/v:off /c', command)
                self.assertIn('conhost.exe', command)
                self.assertTrue(command.endswith('/c native_monitor_windows.cmd'))
                self.assertEqual(popen.call_args.kwargs['cwd'], str(Path(native_monitor.__file__).resolve().parent))
                self.assertEqual(popen.call_args.kwargs['env']['MEGAPROG_MONITOR_ROOT'], str(Path(folder).resolve()))
                self.assertEqual(popen.call_args.kwargs['env']['MEGAPROG_MONITOR_KIND'], 'task')
                self.assertEqual(popen.call_args.kwargs['creationflags'], 16)

    def test_windows_overview_has_distinct_ascii_title(self):
        with patch.object(native_monitor.sys, 'platform', 'win32'), \
             patch.object(native_monitor, 'running', side_effect=[False, True]), \
             patch.object(native_monitor, 'is_ready', return_value=True), \
             patch.object(native_monitor.subprocess, 'CREATE_NEW_CONSOLE', 16, create=True), \
             patch.object(native_monitor.subprocess, 'Popen') as popen:
            self.assertEqual(native_monitor.launch(native_monitor.registry_root()), 'STARTED')
            self.assertEqual(popen.call_args.kwargs['env']['MEGAPROG_MONITOR_KIND'], 'overview')
        script = Path(native_monitor.__file__).with_name('native_monitor_windows.cmd').read_text(encoding='ascii')
        self.assertIn('MegaProg - ALL PROJECTS', script)
        self.assertIn('MegaProg - THIS TASK', script)

    def test_launch_waits_for_rendered_status_not_just_a_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(native_monitor.sys, 'platform', 'darwin'), \
                 patch.object(native_monitor, 'running', side_effect=[False, True, True]), \
                 patch.object(native_monitor, 'is_ready', side_effect=[False, True]), \
                 patch.object(native_monitor.subprocess, 'run'), \
                 patch.object(native_monitor.time, 'sleep'):
                self.assertEqual(native_monitor.launch(Path(folder)), 'STARTED')

    def test_closed_cmd_owner_is_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(native_monitor.sys, 'platform', 'win32'), \
                 patch.object(native_monitor, 'running', side_effect=[True, True, False, False, True]), \
                 patch.object(native_monitor, 'orphaned_window', return_value=True), \
                 patch.object(native_monitor, 'is_ready', return_value=True), \
                 patch.object(native_monitor.time, 'sleep'), \
                 patch.object(native_monitor.subprocess, 'CREATE_NEW_CONSOLE', 16, create=True), \
                 patch.object(native_monitor.subprocess, 'Popen') as popen:
                self.assertEqual(native_monitor.launch(root), 'STARTED')
        popen.assert_called_once()

    def test_closed_cmd_parent_stops_orphan_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {'MEGAPROG_MONITOR_PARENT_PID': '123'}), \
                 patch.object(native_monitor.sys, 'platform', 'win32'), \
                 patch.object(native_monitor, 'windows_process_alive', return_value=False), \
                 patch('ai_dev.terminal_style.interactive', return_value=False), \
                 patch('ai_dev.dashboard.dashboard') as dashboard:
                self.assertEqual(native_monitor._native_watch(root), 0)
            dashboard.assert_not_called()

    def test_first_render_marks_ready_with_the_window_token(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = {'projects': [], 'counts': {}}
            with patch.dict(os.environ, {'MEGAPROG_MONITOR_TOKEN': 'window-token'}), \
                 patch('ai_dev.dashboard.dashboard', return_value=result), \
                 patch('ai_dev.dashboard.render', return_value='status'), \
                 patch.object(native_monitor, 'should_auto_close', return_value=True), \
                 patch.object(native_monitor.time, 'sleep'), redirect_stdout(io.StringIO()):
                self.assertEqual(native_monitor._native_watch(root, close_delay=0), 0)
            self.assertTrue(native_monitor.is_ready(root, 'window-token'))

    def test_no_raw_screen_escapes_when_windows_vt_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = io.StringIO()
            with patch.dict(os.environ, {'NO_COLOR': '1'}), \
                 patch('ai_dev.dashboard.dashboard', return_value={'projects': [], 'counts': {}}), \
                 patch('ai_dev.dashboard.render', return_value='plain status'), \
                 patch('ai_dev.terminal_style.interactive', return_value=False), \
                 patch.object(native_monitor, 'should_auto_close', return_value=True), \
                 patch.object(native_monitor.time, 'sleep'), redirect_stdout(output):
                self.assertEqual(native_monitor._native_watch(root, close_delay=0), 0)
            self.assertIn('plain status', output.getvalue())
            self.assertNotIn('\x1b', output.getvalue())

    def test_auto_close_policy_preserves_error_windows(self):
        completed = {'projects': [{'status': 'COMPLETED', 'verify': 'PASS'}]}
        incomplete = {'projects': [{'status': 'COMPLETED', 'verify': 'NOT_RUN'}]}
        blocked = {'projects': [{'status': 'BLOCKED'}]}
        running = {'projects': [{'status': 'RUNNING'}]}
        self.assertTrue(native_monitor.should_auto_close(completed, overview=False))
        self.assertFalse(native_monitor.should_auto_close(incomplete, overview=False))
        self.assertFalse(native_monitor.should_auto_close(blocked, overview=False))
        self.assertFalse(native_monitor.should_auto_close(running, overview=False))
        self.assertFalse(native_monitor.should_auto_close(completed, overview=True))
        self.assertFalse(native_monitor.should_auto_close(blocked, overview=True))
        self.assertFalse(native_monitor.should_auto_close(running, overview=True))

    def test_auto_failure_does_not_block_worker_and_headless_optout(self):
        with patch.dict(os.environ, {'MEGAPROG_NO_MONITOR': '0'}), patch.object(native_monitor, 'open_monitors', side_effect=OSError('denied')) as launch, patch('sys.stderr') as stderr:
            native_monitor.auto_open(Path('.'))
            self.assertTrue(stderr.write.called)
            launch.assert_called_once()
        with patch.dict(os.environ, {'MEGAPROG_NO_MONITOR': '1'}), patch.object(native_monitor, 'open_monitors') as launch:
            native_monitor.auto_open(Path('.'))
            launch.assert_not_called()

    def test_mac_close_targets_only_its_unique_tab_and_reports_failure(self):
        completed = type('Completed', (), {'returncode': 0, 'stdout': 'CLOSED\n', 'stderr': ''})()
        with patch.object(native_monitor_runner.subprocess, 'run', return_value=completed) as run:
            self.assertTrue(native_monitor_runner.close_own_mac_tab('megaprog-test-token'))
        script = run.call_args.args[0][2]
        self.assertIn('if custom title of monitorTab is "megaprog-test-token" then', script)
        self.assertIn('if (count of tabs of terminalWindow) is 1 then', script)
        self.assertIn('close terminalWindow', script)
        self.assertNotIn('close every tab', script)
        failed = type('Completed', (), {'returncode': 1, 'stdout': '', 'stderr': 'permission denied'})()
        with patch.object(native_monitor_runner.subprocess, 'run', return_value=failed), \
             patch.object(native_monitor_runner.sys, 'stderr') as error:
            self.assertFalse(native_monitor_runner.close_own_mac_tab('megaprog-test-token'))
            self.assertTrue(error.write.called)

    def test_mac_tab_close_is_deferred_until_monitor_exits(self):
        with patch.object(native_monitor_runner.subprocess, 'Popen') as popen:
            native_monitor_runner.schedule_close_own_mac_tab('megaprog-test-token')
        args = popen.call_args.args[0]
        self.assertEqual(args[2], '--close-tab-after-pid')
        self.assertEqual(args[4], 'megaprog-test-token')
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        with patch.object(native_monitor_runner.os, 'kill', side_effect=ProcessLookupError), \
             patch.object(native_monitor_runner.time, 'sleep'), \
             patch.object(native_monitor_runner, 'close_own_mac_tab', return_value=True) as close:
            self.assertTrue(native_monitor_runner.close_tab_after_pid(123, 'megaprog-test-token'))
        close.assert_called_once_with('megaprog-test-token')
