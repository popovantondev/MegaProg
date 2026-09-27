import time
import unittest
from ai_dev.routing import choose, ORDER, exhausted


def catalog(names=ORDER):
    return {'models': [{'model': name, 'supportedReasoningEfforts':
            [{'reasoningEffort': effort} for effort in ('low', 'medium', 'high', 'ultra')]} for name in names], 'limits': None}


class RoutingTests(unittest.TestCase):
    def test_small_uses_luna_low(self):
        self.assertEqual(choose(catalog(), 'Исправить опечатку')['model'], ORDER[0])
        self.assertEqual(choose(catalog(), 'Исправить опечатку')['reasoning'], 'low')

    def test_normal_uses_luna(self):
        self.assertEqual(choose(catalog(), 'Добавь экспорт в CSV')['model'], ORDER[0])

    def test_hard_starts_at_sol(self):
        self.assertEqual(choose(catalog(), 'Fix a deadlock')['model'], ORDER[1])

    def test_missing_catalog_blocks(self):
        with self.assertRaises(ValueError):
            choose(catalog([]), 'hello')

    def test_missing_model_skips(self):
        self.assertEqual(choose(catalog([ORDER[1]]), 'hello')['model'], ORDER[1])

    def test_explicit_unknown_model_blocks(self):
        with self.assertRaises(ValueError):
            choose(catalog(), 'hello', model='invented')

    def test_escalation_and_next_task_downgrade(self):
        first = choose(catalog(), 'normal task')
        self.assertEqual(choose(catalog(), 'normal task', previous=first)['model'], ORDER[1])
        self.assertEqual(choose(catalog(), 'normal task')['model'], ORDER[0])

    def test_astra_expensive_modes_blocked(self):
        for effort in ('high', 'ultra'):
            with self.assertRaises(ValueError):
                choose(catalog(), 'hello', model=ORDER[-1], reasoning=effort)
        self.assertEqual(choose(catalog(), 'hello', kind='expert')['reasoning'], 'low')

    def test_explicit_astra_is_sufficient_without_second_expert_flag(self):
        for kind in ('auto', 'small', 'normal', 'hard', 'expert'):
            with self.subTest(kind=kind):
                result = choose(catalog(), 'hello', kind=kind, model=ORDER[-1], reasoning='medium')
                self.assertEqual((result['model'], result['reasoning'], result['kind']),
                                 (ORDER[-1], 'medium', 'expert'))

    def test_auto_worker_does_not_silently_fall_back_to_astra(self):
        with self.assertRaises(ValueError):
            choose(catalog([ORDER[-1]]), 'hello')

    def test_selection_errors_distinguish_quota_catalog_and_effort(self):
        c = catalog([ORDER[-1]])
        with self.assertRaisesRegex(ValueError, 'отсутствует в доступном каталоге'):
            choose(c, 'hello', model=ORDER[1])
        with self.assertRaisesRegex(ValueError, 'режим high не заявлен'):
            low = catalog([ORDER[1]])
            low['models'][0]['supportedReasoningEfforts'] = [{'reasoningEffort': 'low'}]
            choose(low, 'hello', model=ORDER[1], reasoning='high')
        c['limits'] = {'rateLimits': {'spendControlReached': True}}
        with self.assertRaisesRegex(ValueError, 'подтверждено исчерпание квоты'):
            choose(c, 'hello', model=ORDER[-1])
        c['errors'] = {'models': 'app-server initialize failed'}
        with self.assertRaisesRegex(ValueError, 'Сбой получения каталога Codex: app-server initialize failed'):
            choose(c, 'hello', model=ORDER[-1])

    def test_efforts_must_be_advertised(self):
        c = catalog([ORDER[1]])
        c['models'][0]['supportedReasoningEfforts'] = [{'reasoningEffort': 'low'}]
        self.assertEqual(choose(c, 'hello')['reasoning'], 'low')
        with self.assertRaises(ValueError):
            choose(c, 'hello', reasoning='medium')

    def test_exhausted_quota_does_not_become_new_pool(self):
        c = catalog()
        c['limits'] = {'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 100, 'resetsAt': time.time()+100}}}}
        with self.assertRaises(ValueError):
            choose(c, 'normal task')
        with self.assertRaises(ValueError):
            choose(c, 'опечатка')

    def test_unknown_or_expired_quota_not_zero(self):
        self.assertFalse(exhausted(None, ORDER[1]))
        self.assertFalse(exhausted({'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': 1}}}, ORDER[1]))

    def test_model_specific_exhausted_limit_is_skipped(self):
        c = catalog()
        c['limits'] = {'rateLimitsByLimitId': {
            'luna-limit': {'normalModelSlug': ORDER[0], 'spendControlReached': True},
        }}
        self.assertEqual(choose(c, 'normal task')['model'], ORDER[1])

    def test_retired_model_is_rejected_even_if_catalog_still_lists_it(self):
        c = catalog(ORDER + ['gpt-5.6-luna'])
        with self.assertRaisesRegex(ValueError, 'только GPT-6'):
            choose(c, 'hello', model='gpt-5.6-luna')

    def test_unrelated_or_malformed_limit_is_unknown(self):
        limits = {'rateLimitsByLimitId': {
            'other': {'normalModelSlug': 'other-model',
                      'primary': {'usedPercent': 100}},
        }}
        self.assertFalse(exhausted(limits, ORDER[1]))

    def test_hidden_model_excluded(self):
        c = catalog([ORDER[1]])
        c['models'][0]['hidden'] = True
        with self.assertRaises(ValueError):
            choose(c, 'hello')
