import tempfile
import unittest
import sys
from pathlib import Path

from ai_dev.orchestration import (approval_status, approve, claim_task, checkpoint_task,
                                  create_plan, create_task, handoff_report, parity_check,
                                  registry, status, verify_task)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = create_plan(self.root, 'Добавить проверяемый шаг', ['src/app.py'],
                                [[sys.executable, '-c', 'print("verified")']])

    def test_durable_roles_and_verifier_gate(self):
        task = create_task(self.root, self.plan['plan_id'], 'Изменить app', task_id='task-one')
        claimed = claim_task(self.root, task['task_id'], 'worker-1')
        self.assertEqual(claimed['status'], 'CLAIMED')
        with self.assertRaises(ValueError):
            verify_task(self.root, task['task_id'], 'verifier-1', [{'name': 'tests', 'status': 'PASS'}])
        checkpoint = checkpoint_task(self.root, task['task_id'], 'worker-1', 'Изменение готово', ['src/app.py'])
        self.assertEqual(checkpoint['handoff_role'], 'verifier')
        completed = verify_task(self.root, task['task_id'], 'verifier-1', None, 'Проверено')
        self.assertEqual(completed['status'], 'COMPLETED')
        self.assertEqual(status(self.root)['counts']['COMPLETED'], 1)

    def test_one_active_worker_owns_project(self):
        first = create_task(self.root, self.plan['plan_id'], 'first', task_id='task-first')
        second = create_task(self.root, self.plan['plan_id'], 'second', task_id='task-second')
        claim_task(self.root, first['task_id'], 'worker-1')
        with self.assertRaises(ValueError):
            claim_task(self.root, second['task_id'], 'worker-2')

    def test_failed_verification_never_completes_task(self):
        failing_plan = create_plan(self.root, 'fail', ['src/app.py'],
                                   [[sys.executable, '-c', 'raise SystemExit(7)']], plan_id='plan-fail')
        task = create_task(self.root, failing_plan['plan_id'], 'fail', task_id='task-fail')
        claim_task(self.root, task['task_id'], 'worker-1')
        checkpoint_task(self.root, task['task_id'], 'worker-1', 'ready', ['src/app.py'])
        result = verify_task(self.root, task['task_id'], 'verifier-1')
        self.assertEqual(result['status'], 'BLOCKED')

    def test_machine_readable_report_declares_no_automatic_threads(self):
        report = handoff_report(self.root, self.plan['plan_id'])
        self.assertEqual(report['roles'], ['chief', 'assistant', 'worker', 'verifier'])
        self.assertFalse(report['automatic_threads'])
        self.assertFalse(report['network'])
        self.assertIn('usage_summary', report)

    def test_approval_is_pending_until_explicit_actor_approval(self):
        self.assertEqual(approval_status(self.root, 'model_turn', self.plan['plan_id'])['status'],
                         'PENDING_APPROVAL')
        approved = approve(self.root, 'model_turn', 'owner', self.plan['plan_id'])
        self.assertEqual(approved['actor'], 'owner')
        self.assertEqual(approval_status(self.root, 'model_turn', self.plan['plan_id'])['status'], 'APPROVED')

    def test_registry_and_parity_are_machine_readable(self):
        data = registry(self.root)
        self.assertIn(self.plan['plan_id'], data['projects'])
        manifest = Path(__file__).parents[1] / 'docs' / 'ORCHESTRATION_PARITY_MANIFEST.json'
        result = parity_check(self.root, manifest)
        self.assertEqual(result['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
