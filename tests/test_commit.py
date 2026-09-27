import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from ai_dev.storage import write, read
from ai_dev import supervisor


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.git('init')
        self.git('config', 'user.name', 'ai-dev test')
        self.git('config', 'user.email', 'test@localhost')
        (self.root / '.gitignore').write_text('.ai-dev/\n')
        (self.root / 'answer').write_text('old')
        self.git('add', '.')
        self.git('commit', '-m', 'baseline')
        base = self.git('rev-parse', 'HEAD')
        (self.root / 'answer').write_text('new')
        (self.root / 'new-file').write_text('new')
        config = dict(supervisor.DEFAULT, verify=[[sys.executable, '-c',
            'from pathlib import Path; assert Path("answer").read_text() == "new"']])
        write(self.root / '.ai-dev/config.json', config)
        write(self.root / '.ai-dev/state.json', {'id': 'test', 'status': 'COMPLETED',
             'base_head': base, 'config': config, 'snapshot': supervisor.snapshot(self.root)})

    def git(self, *args):
        return subprocess.check_output(['git'] + list(args), cwd=str(self.root), stderr=subprocess.DEVNULL, text=True).strip()

    def test_verified_result_committed(self):
        self.assertEqual(supervisor.commit(self.root, 'result'), 0)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(read(self.root / '.ai-dev/state.json')['commit'], self.git('rev-parse', 'HEAD'))

    def test_later_tracked_changes_rejected(self):
        (self.root / 'answer').write_text('manual')
        with self.assertRaises(ValueError):
            supervisor.commit(self.root, 'result')
        self.assertEqual(self.git('diff', '--cached'), '')

    def test_later_untracked_changes_rejected(self):
        (self.root / 'new-file').write_text('manual')
        with self.assertRaises(ValueError):
            supervisor.commit(self.root, 'result')

    def test_failed_reverification_does_not_stage(self):
        config = dict(supervisor.DEFAULT, verify=[[sys.executable, '-c', 'raise SystemExit(1)']])
        write(self.root / '.ai-dev/config.json', config)
        state = read(self.root / '.ai-dev/state.json')
        state['config'] = config
        write(self.root / '.ai-dev/state.json', state)
        with self.assertRaises(ValueError):
            supervisor.commit(self.root, 'result')
        self.assertEqual(self.git('diff', '--cached'), '')

    def test_changed_config_rejected(self):
        config = dict(supervisor.DEFAULT, verify=[[sys.executable, '-c', 'pass']])
        write(self.root / '.ai-dev/config.json', config)
        with self.assertRaises(ValueError):
            supervisor.commit(self.root, 'result')
