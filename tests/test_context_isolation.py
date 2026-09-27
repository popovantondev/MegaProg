import tempfile
import threading
import unittest
import os
from pathlib import Path

from ai_dev.context_isolation import isolate_generated_context, _staging_parent


class ContextIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.ai-dev' / 'schema').mkdir(parents=True)
        (self.root / '.ai-dev' / 'runs' / 'one').mkdir(parents=True)
        (self.root / '.ai-dev' / 'schema' / 'large.json').write_text('schema')
        (self.root / '.ai-dev' / 'runs' / 'one' / 'events.jsonl').write_text('events')
        (self.root / '.ai-dev' / 'config.json').write_text('{}')

    def test_paths_are_hidden_and_restored_after_exception(self):
        with self.assertRaises(RuntimeError):
            with isolate_generated_context(self.root) as isolated:
                self.assertFalse((self.root / '.ai-dev' / 'schema').exists())
                self.assertFalse((self.root / '.ai-dev' / 'runs').exists())
                self.assertTrue(isolated.relocate(self.root / '.ai-dev' / 'runs' / 'x').parent.exists())
                raise RuntimeError('turn failed')
        self.assertEqual((self.root / '.ai-dev' / 'schema' / 'large.json').read_text(), 'schema')
        self.assertEqual((self.root / '.ai-dev' / 'runs' / 'one' / 'events.jsonl').read_text(), 'events')
        self.assertTrue((self.root / '.ai-dev' / 'config.json').exists())

    def test_keyboard_interrupt_restores_and_preserves_new_path(self):
        with self.assertRaises(KeyboardInterrupt):
            with isolate_generated_context(self.root):
                (self.root / '.ai-dev' / 'runs').mkdir(parents=True)
                (self.root / '.ai-dev' / 'runs' / 'new.jsonl').write_text('new')
                raise KeyboardInterrupt()
        self.assertTrue((self.root / '.ai-dev' / 'runs' / 'one' / 'events.jsonl').exists())
        conflicts = list((self.root / '.ai-dev').glob('runs.during-turn-*'))
        self.assertEqual(len(conflicts), 1)
        self.assertEqual((conflicts[0] / 'new.jsonl').read_text(), 'new')

    def test_concurrent_isolation_is_rejected_without_moving_data(self):
        entered = threading.Event()
        release = threading.Event()
        first_error = []

        def first():
            try:
                with isolate_generated_context(self.root):
                    entered.set()
                    release.wait(2)
            except Exception as exc:
                first_error.append(exc)

        worker = threading.Thread(target=first)
        worker.start()
        self.assertTrue(entered.wait(2))
        with self.assertRaises(ValueError):
            with isolate_generated_context(self.root):
                pass
        release.set()
        worker.join(2)
        self.assertFalse(first_error)
        self.assertTrue((self.root / '.ai-dev' / 'runs' / 'one' / 'events.jsonl').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows staging selection')
    def test_windows_staging_prefers_local_temp_on_same_volume(self):
        local = Path(tempfile.gettempdir()).resolve()
        if os.stat(self.root).st_dev != os.stat(local).st_dev:
            self.skipTest('temporary directory is on a different volume')
        self.assertEqual(_staging_parent(self.root), local)
        with isolate_generated_context(self.root) as isolated:
            self.assertEqual(Path(isolated.metrics['staging_parent']), local)
            self.assertTrue(isolated.relocate(self.root / '.ai-dev/runs/one/events.jsonl').exists())
        self.assertTrue((self.root / '.ai-dev/runs/one/events.jsonl').exists())

    @unittest.skipUnless(os.name == 'nt' and os.environ.get('MEGAPROG_ONEDRIVE_SMOKE_ROOT'),
                         'Explicit opt-in real OneDrive fixture')
    def test_opt_in_real_onedrive_fixture_restores_run_directory(self):
        fixture = Path(os.environ['MEGAPROG_ONEDRIVE_SMOKE_ROOT']).resolve()
        runs = fixture / '.ai-dev/runs'
        self.assertTrue(runs.is_dir())
        before = sorted(item.name for item in runs.iterdir())
        with isolate_generated_context(fixture) as isolated:
            self.assertEqual(Path(isolated.metrics['staging_parent']), Path(tempfile.gettempdir()).resolve())
            self.assertFalse(runs.exists())
        self.assertEqual(sorted(item.name for item in runs.iterdir()), before)
