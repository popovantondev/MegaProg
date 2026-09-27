import unittest

from ai_dev.context import assemble


class ContextAssemblyTests(unittest.TestCase):
    def test_common_budget_truncates_only_reference_context(self):
        task = 'Пользовательская задача: ' + ('важно ' * 300)
        text, metrics = assemble(
            task, [['python3', '-m', 'unittest']], 'MANDATORY POLICY',
            lessons='lesson ' * 10000,
            previous_verification={'checks': ['failure ' * 10000]},
            budget_bytes=12000)
        self.assertLessEqual(metrics['prompt_bytes'], 12000)
        self.assertEqual(metrics['prompt_bytes'], len(text.encode('utf-8')))
        self.assertIn(task, text)
        self.assertIn('MANDATORY POLICY', text)
        self.assertTrue(metrics['truncated'])
        self.assertIn('truncated', text)

    def test_utf8_budget_is_exact_and_task_is_never_truncated(self):
        task = 'Исправить: ' + ('ёжик ' * 300)
        text, metrics = assemble(task, [], 'SAFETY', budget_bytes=8192)
        self.assertIn(task, text)
        self.assertLessEqual(metrics['prompt_bytes'], metrics['budget_bytes'])
        self.assertEqual(metrics['task_bytes'], len(task.encode('utf-8')))

    def test_oversized_task_fails_closed(self):
        with self.assertRaises(ValueError):
            assemble('x' * 20000, [], 'MANDATORY', budget_bytes=8192)


if __name__ == '__main__':
    unittest.main()
