import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from ai_dev import self_health, self_repair, codex, supervisor
from ai_dev.self_improvement import POLICY_FILES, policy_digest
from ai_dev.storage import read, write
from ai_dev.version import __version__
from test_routing import catalog, ORDER


@contextmanager
def slot(*args, **kwargs):
    yield SimpleNamespace(evidence={'acquired': True})


class SelfRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.git('init')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@example.invalid')
        original = Path(__file__).resolve().parents[1]
        for name in POLICY_FILES:
            shutil.copyfile(original / name, self.source / name)
        (self.source / '.gitignore').write_text('.ai-dev/\n__pycache__/\n', encoding='utf-8')
        (self.source / 'ai_dev').mkdir()
        (self.source / 'tests').mkdir()
        (self.source / 'ai_dev/__init__.py').write_text('', encoding='utf-8')
        (self.source / 'ai_dev/feature.py').write_text('def answer():\n    return 0\n', encoding='utf-8')
        (self.source / 'tests/test_baseline.py').write_text(
            'import unittest\nclass Baseline(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-m', 'baseline')
        self.base = self.git('rev-parse', 'HEAD')
        self.policy = policy_digest(self.source)
        self_repair.configure(self.source, True)
        write(self_repair.home(self.source) / 'audit.json', {'revision': self.base, 'ok': True})
        for target, value in [('ai_dev.self_repair.SOURCE', self.source)]:
            p = patch(target, value)
            p.start(); self.addCleanup(p.stop)
        env = patch.dict(os.environ, {'MEGAPROG_NO_MONITOR': '1', 'MEGAPROG_SELF_REPAIR_CHILD': '0'})
        env.start(); self.addCleanup(env.stop)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.source,
                                       stderr=subprocess.STDOUT, text=True).strip()

    def incident(self, kind='internal', ident='one'):
        return self_health.observe(self.project, {'id': ident, 'updated_at': 1}, kind=kind,
                                   exception_type='TypeError',
                                   frames=[{'file': 'ai_dev/feature.py', 'line': 2, 'function': 'answer'}])

    def test_dedup_and_infrastructure_never_start_code_repair(self):
        for kind in ('discovery', 'verification', 'account_guard', 'infrastructure'):
            self.incident(kind)
        self.assertEqual(self_repair.queue(self.source), {})
        first = self.incident()
        self.incident()
        self.incident(ident='another-project-task')
        self.assertEqual(len(self_repair.queue(self.source)), 1)
        saved = read(Path(first['path']))[first['id']]
        self.assertEqual(saved['occurrences'], 2)
        self.assertNotIn('prompt', saved)

    def test_recursive_repair_is_suppressed(self):
        with patch.dict(os.environ, {'MEGAPROG_SELF_REPAIR_CHILD': '1'}):
            result = self.incident()
        self.assertEqual(result['repair_status'], 'RECURSION_PREVENTED')
        self.assertFalse(self_repair.queue(self.source))

    def test_campaign_budget_and_failure_do_not_loop(self):
        issue = self.incident()
        with patch('ai_dev.self_repair.build_candidate', side_effect=ValueError('cannot fix')) as build:
            result = self_repair.run_once(self.source)
            self.assertEqual(result['status'], 'REVIEW_REQUIRED')
            self.incident()  # Same failure never resets the status or budget.
            self.assertEqual(self_repair.run_once(self.source)['status'], 'DAILY_BUDGET')
        self.assertEqual(build.call_count, 1)
        self.assertIn(issue['id'], self_repair.queue(self.source))

    def test_historical_fault_never_uses_a_model(self):
        issue = self.incident()
        self_repair.update(self.source, issue['id'], evidence={'version': '0.0.1', 'kind': 'internal'})
        with patch('ai_dev.self_repair.build_candidate') as build:
            self.assertEqual(self_repair.run_once(self.source)['status'], 'HISTORICAL')
        build.assert_not_called()

    def test_old_tests_and_protected_engine_cannot_be_changed(self):
        (self.source / 'ai_dev/feature.py').write_text('def answer(): return 42\n', encoding='utf-8')
        (self.source / 'tests/test_baseline.py').write_text('pass\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Existing tests'):
            self_repair.validate_candidate(self.source, self.base, self.policy)
        self.git('restore', 'tests/test_baseline.py')
        (self.source / 'ai_dev/self_repair.py').write_text('pass\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Forbidden repair file'):
            self_repair.validate_candidate(self.source, self.base, self.policy)

    def test_active_lease_prevents_activation(self):
        issue = self.incident()
        item = self_repair.update(self.source, issue['id'], status='VERIFIED_WAITING_IDLE', base=self.base)
        with self_repair.activity(self.project, source=self.source):
            result = self_repair.promote(self.source, item)
        self.assertEqual(result['status'], 'VERIFIED_WAITING_IDLE')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)

    def run_fake_candidate(self, approved=True, regression=True):
        issue = self.incident()
        calls = []
        def execute(root, config, report, prompt, log, session=None, sandbox='workspace-write'):
            calls.append((config['model'], config['reasoning'], sandbox))
            if sandbox == 'workspace-write':
                (root / 'ai_dev/feature.py').write_text('def answer():\n    return 42\n', encoding='utf-8')
                test_body = (
                    'import unittest\nfrom ai_dev.feature import answer\n'
                    'class Repaired(unittest.TestCase):\n def test_answer(self): self.assertEqual(answer(), 42)\n'
                ) if regression else 'import unittest\nclass Empty(unittest.TestCase):\n def test_trivial(self): self.assertTrue(True)\n'
                (root / 'tests/test_repair_regression.py').write_text(test_body, encoding='utf-8')
                return {'ok': True, 'session_id': 'fake-worker', 'usage': {'input_tokens': 1}}
            self.assertIsNone(session)
            return {'ok': True, 'message': json.dumps({'approved': approved, 'reason': 'independent verdict'}),
                    'usage': {'input_tokens': 1}}
        with patch('ai_dev.codex.doctor', return_value={'ready': True, 'executable': 'fake', 'resume': True}), \
             patch('ai_dev.discovery.discover', return_value=catalog()), \
             patch('ai_dev.supervisor.discover', return_value=catalog()), \
             patch('ai_dev.supervisor.waiting_turn', side_effect=slot), \
             patch('ai_dev.codex.execute', side_effect=execute), \
             patch('ai_dev.self_repair.active_workers', return_value=True):
            result = self_repair.run_once(self.source)
        def cleanup_candidate():
            folder = Path(result['worktree'])
            for tree in (folder, folder.parent / 'regression-baseline'):
                if tree.exists():
                    self.git('worktree', 'remove', '--force', str(tree))
            shutil.rmtree(folder.parent)
        self.addCleanup(cleanup_candidate)
        return result, calls

    def test_full_repair_pipeline_isolates_checks_reviews_and_waits_for_idle(self):
        result, calls = self.run_fake_candidate()
        self.assertEqual(result['status'], 'VERIFIED_WAITING_IDLE', result)
        self.assertEqual(calls, [(ORDER[-1], 'medium', 'workspace-write'),
                                 (ORDER[-1], 'medium', 'read-only')])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertIn('return 0', (self.source / 'ai_dev/feature.py').read_text())
        with patch('ai_dev.self_repair.active_workers', return_value=False):
            result = self_repair.run_once(self.source)
        self.assertEqual(result['status'], 'APPLIED')
        self.assertIn('return 42', (self.source / 'ai_dev/feature.py').read_text())
        self.assertEqual(result['rollback_commit'], self.base)
        self.assertEqual(len(calls), 2)

    def test_reviewer_rejection_keeps_original_source(self):
        result, calls = self.run_fake_candidate(approved=False)
        self.assertEqual(result['status'], 'REVIEW_REQUIRED')
        self.assertIn('reviewer rejected', result['note'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('candidate_commit', result)

    def test_trivial_new_test_cannot_prove_a_repair(self):
        result, calls = self.run_fake_candidate(regression=False)
        self.assertEqual(result['status'], 'REVIEW_REQUIRED')
        self.assertIn('do not reproduce', result['note'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.base)
        self.assertEqual(len(calls), 1)  # Rejected before paying for review.

    def test_verified_candidate_is_not_applied_over_developer_changes(self):
        issue = self.incident()
        item = self_repair.update(self.source, issue['id'], status='VERIFIED_WAITING_IDLE',
                                  base=self.base, candidate_commit=self.base)
        (self.source / 'ai_dev/feature.py').write_text('user change\n', encoding='utf-8')
        with patch('ai_dev.self_repair.active_workers', return_value=False):
            result = self_repair.promote(self.source, item)
        self.assertEqual(result['status'], 'REVIEW_REQUIRED')
        self.assertEqual((self.source / 'ai_dev/feature.py').read_text(), 'user change\n')

    def test_audit_runs_once_per_revision_without_models(self):
        audit = self_repair.home(self.source) / 'audit.json'
        audit.unlink()
        with patch('ai_dev.process.run', return_value=(1, 'own test failed')) as run:
            self_repair.audit_revision(self.source)
            self_repair.audit_revision(self.source)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(self_repair.queue(self.source)), 1)
