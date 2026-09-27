import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from ai_dev.process import run
from ai_dev import codex, supervisor, dashboard, cli
from ai_dev.terminal_style import styled
from ai_dev.storage import write


class TimeoutStyleTests(unittest.TestCase):
    def test_unlimited_process_and_explicit_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            argv = [sys.executable, '-c', 'import time;time.sleep(.1);print("done")']
            code, output = run(argv, folder, timeout=0)
            self.assertEqual((code, output.strip()), (0, 'done'))
            with self.assertRaises(subprocess.TimeoutExpired):
                run([sys.executable, '-c', 'import time;time.sleep(10)'], folder, timeout=.05)

    def test_unlimited_still_supports_cancellation(self):
        with tempfile.TemporaryDirectory() as folder:
            cancel = Path(folder) / 'cancel'; cancel.touch()
            with patch.dict(os.environ, {'AI_DEV_CANCEL_FILE': str(cancel)}):
                with self.assertRaises(KeyboardInterrupt):
                    run([sys.executable, '-c', 'import time;time.sleep(10)'], folder, timeout=0)

    def test_legacy_default_disabled_without_mutating_resume_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = dict(supervisor.DEFAULT, timeout_seconds=900, model='gpt-6-luna')
            write(root / '.ai-dev/config.json', config)
            self.assertEqual(supervisor.configure(root)['timeout_seconds'], 900)
            with patch('ai_dev.codex.run', return_value=(0, '{"type":"turn.completed"}\n')) as mocked:
                codex.execute(root, config, {'executable': 'codex'}, 'test', root / 'log', session='original-session')
                self.assertEqual(mocked.call_args.kwargs['timeout'], 0)
                self.assertIn('original-session', mocked.call_args.args[0])
            self.assertEqual(config['timeout_seconds'], 900)

    def test_colors_and_plain_output(self):
        value = '  Модель: gpt-6-luna | режим: medium\n  проверка: FAIL\n[COMPLETED] готово'
        with patch.dict(os.environ, {'MEGAPROG_THEME': 'dark'}):
            self.assertIn('\033[1;35m', styled(value, True))
            self.assertIn('\033[1;31m', styled(value, True))
        self.assertEqual(styled(value, False), value)
        with patch.dict(os.environ, {'NO_COLOR': '1'}), patch('ai_dev.terminal_style.interactive', return_value=True):
            self.assertEqual(styled(value), value)
        with patch.dict(os.environ, {'MEGAPROG_COLOR': 'never'}, clear=True):
            self.assertEqual(styled(value), value)
        with patch.dict(os.environ, {'MEGAPROG_COLOR': 'always', 'MEGAPROG_THEME': 'light'}, clear=True):
            self.assertIn('\033[35m', styled(value))
        with patch.dict(os.environ, {'NO_COLOR': '1', 'MEGAPROG_COLOR': 'always'}, clear=True):
            self.assertEqual(styled(value), value)
        with patch.dict(os.environ, {'MEGAPROG_COLOR': 'always', 'MEGAPROG_THEME': 'dark'}, clear=True):
            self.assertIn('\033[1;93m', styled('══ ТЕКУЩАЯ ЗАДАЧА: demo'))

    def test_theme_override_and_windows_capability_fallback(self):
        from ai_dev import terminal_style
        with patch.dict(os.environ, {'MEGAPROG_THEME': 'light'}, clear=True):
            self.assertEqual(terminal_style.theme(), 'light')
            self.assertIn('\033[1;34m', styled('MEGAPROG status', True))
        with patch.dict(os.environ, {'MEGAPROG_THEME': 'dark'}, clear=True):
            self.assertEqual(terminal_style.theme(), 'dark')
            self.assertIn('\033[1;36m', styled('MEGAPROG status', True))
        with patch.object(terminal_style.os, 'name', 'nt'), \
             patch.object(terminal_style.sys.stdout, 'isatty', return_value=True), \
             patch.dict(os.environ, {}, clear=True):
            self.assertEqual(terminal_style.theme(), 'light')
            with patch.dict(sys.modules, {'ctypes': None}):
                self.assertFalse(terminal_style.interactive())

    def test_failed_check_is_not_hidden_by_later_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / '.ai-dev/state.json', {'status': 'BLOCKED', 'verification': {'checks': [
                {'command': ['build'], 'exit_code': 1, 'tail': 'sandbox denied'},
                {'command': ['syntax'], 'exit_code': 0}]}})
            status = dashboard.project_status(root)
            self.assertEqual(status['verify'], 'FAIL')
            self.assertEqual(status['reason'], 'sandbox denied')

    def test_watch_restores_screen_and_json_has_no_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            stop = Path(folder)/'stop'; stop.touch()
            output = io.StringIO()
            with patch('ai_dev.cli.interactive', return_value=True), redirect_stdout(output):
                cli.main(['dashboard','--root',folder,'--watch','--stop-file',str(stop)])
            self.assertIn('\033[?1049h', output.getvalue())
            self.assertIn('\033[?1049l', output.getvalue())
            output = io.StringIO()
            with redirect_stdout(output):
                cli.main(['dashboard','--root',folder,'--json'])
            self.assertNotIn('\033', output.getvalue())
            self.assertIn('projects', json.loads(output.getvalue()))

    def test_native_console_does_not_inherit_headless_style(self):
        from ai_dev.native_monitor_runner import prepare_terminal
        with patch.dict(os.environ, {'TERM': 'dumb', 'NO_COLOR': '1'}, clear=True):
            with patch('sys.stdout.isatty', return_value=False):
                prepare_terminal()
            self.assertEqual(os.environ['NO_COLOR'], '1')
            with patch('sys.stdout.isatty', return_value=True):
                prepare_terminal()
                self.assertEqual(os.environ['NO_COLOR'], '1')
                self.assertNotEqual(os.environ.get('TERM'), 'dumb')
                os.environ['MEGAPROG_MONITOR_NO_COLOR'] = '1'
                prepare_terminal()
                self.assertEqual(os.environ['NO_COLOR'], '1')
