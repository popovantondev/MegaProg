import unittest

from ai_dev.chatgpt_policy import (MAX_BATCH_ITEMS, batch_limit, cache_key,
                                    cache_status, decide)


class ChatGPTPolicyTests(unittest.TestCase):
    def test_only_explicit_eligible_reasons_are_enabled(self):
        self.assertEqual(decide({'advisory_reason': 'diagnosis'})['level'], 'normal')
        self.assertEqual(decide({'advisory_reason': 'architecture deadlock'})['level'], 'deep')
        self.assertTrue(decide({'status': 'STALLED', 'attempt_count': 2})['enabled'])
        self.assertFalse(decide({'prompt': 'ordinary implementation'})['enabled'])
        self.assertFalse(decide({'prompt': 'obvious lint type fix'})['enabled'])
        self.assertFalse(decide({'prompt': 'tests only'})['enabled'])

    def test_negative_reason_wins_and_model_boundary_is_visible(self):
        decision = decide({'advisory_reason': 'diagnosis ordinary implementation'})
        self.assertFalse(decision['enabled'])
        self.assertEqual(decision['reason'], 'ordinary_implementation')
        self.assertTrue(decision['non_blocking'])
        self.assertEqual(decision['resume_models'], [])
        self.assertEqual(decision['codex_only_models'], ['Terra', 'Sol', 'Astra'])

    def test_batch_and_exact_negative_stale_cache(self):
        self.assertEqual(batch_limit(None), 4)
        self.assertEqual(batch_limit(6), 6)
        with self.assertRaises(ValueError):
            batch_limit(MAX_BATCH_ITEMS + 1)
        task_hash, repo_hash = 'a' * 64, 'b' * 64
        key = cache_key(task_hash, repo_hash)
        self.assertEqual(cache_status({key: {'negative': True}}, task_hash, repo_hash), 'negative_hit')
        self.assertEqual(cache_status({'old': {'task_hash': task_hash}}, task_hash, repo_hash), 'stale')


if __name__ == '__main__':
    unittest.main()
