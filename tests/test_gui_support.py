import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from ai_dev.process import run
from ai_dev.gui import checklist_lines, inspect_project, task_readiness_message


class CancellationTests(unittest.TestCase):
    def test_cancel_marker_stops_process(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'cancel'
            timer = threading.Timer(0.2, marker.touch)
            with patch.dict(os.environ, {'AI_DEV_CANCEL_FILE': str(marker)}):
                timer.start()
                try:
                    with self.assertRaises(KeyboardInterrupt):
                        run([sys.executable, '-c', 'import time; time.sleep(60)'], folder, timeout=10)
                finally:
                    timer.join()

    def test_repeated_communicate_preserves_stdin(self):
        with tempfile.TemporaryDirectory() as folder:
            code, output = run([sys.executable, '-c',
                'import sys,time; data=sys.stdin.read(); time.sleep(.4); print(data)'],
                folder, timeout=3, input_text='task with spaces')
            self.assertEqual(code, 0)
            self.assertIn('task with spaces', output)


class FirstRunChecklistTests(unittest.TestCase):
    def test_selection_reads_config_without_git_state(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / '.ai-dev').mkdir()
            (project / '.ai-dev/config.json').write_text(
                '{"verify": [["python3", "-m", "unittest"]]}', encoding='utf-8')
            facts = inspect_project(project)
            self.assertTrue(facts['ai_dev_configured'])
            self.assertTrue(facts['verification_configured'])
            self.assertIsNone(facts['working_tree_clean'])
            self.assertIsNone(facts['baseline_exists'])
            self.assertFalse(facts['git_checked'])

    def test_explicit_git_refresh_facts_complete_the_order(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / '.ai-dev').mkdir()
            (project / '.ai-dev/config.json').write_text(
                '{"verify": [["python3", "-m", "unittest"]]}', encoding='utf-8')
            facts = inspect_project(project, {
                'git_repository': True,
                'working_tree_clean': True,
                'baseline_exists': True,
            })
            rows = checklist_lines(facts)
            self.assertEqual([row[1] for row in rows], ['done'] * 6)

    def test_dirty_or_missing_baseline_keeps_task_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            facts = inspect_project(Path(folder), {
                'git_repository': True,
                'working_tree_clean': False,
                'baseline_exists': False,
            })
            rows = checklist_lines(facts)
            self.assertEqual(rows[4][1], 'todo')
            self.assertEqual(rows[5][1], 'todo')

    def test_task_readiness_names_the_next_required_action(self):
        base = {
            'project_selected': True,
            'verification_configured': True,
            'ai_dev_configured': True,
            'git_repository': True,
            'baseline_exists': True,
            'working_tree_clean': True,
        }
        self.assertIsNone(task_readiness_message(base))
        for field, expected in (
                ('verification_configured', 'Подготовить'),
                ('ai_dev_configured', 'Подготовить'),
                ('baseline_exists', 'Сохранить исходную версию'),
                ('working_tree_clean', 'чистой')):
            facts = dict(base)
            facts[field] = False
            self.assertIn(expected, task_readiness_message(facts))

    def test_unknown_git_refresh_is_not_ready(self):
        facts = {
            'project_selected': True,
            'verification_configured': True,
            'ai_dev_configured': True,
            'git_repository': True,
            'baseline_exists': None,
            'working_tree_clean': None,
        }
        self.assertIn('baseline-коммит', task_readiness_message(facts))
