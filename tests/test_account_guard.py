import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from ai_dev.account_guard import (AccountGuard, AccountGuardError, DEFAULT_MAX_CONCURRENT,
                                  DEFAULT_TIER_LIMITS, normalize_legacy_defaults, settings)


class AccountGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / 'shared'
        self.config = {'account_guard': {'directory': str(self.directory),
                                          'max_concurrent': 1,
                                          'stale_seconds': 60}}

    def guard(self, config=None, model='gpt-6-luna', reasoning='medium'):
        return AccountGuard(config or self.config, model, reasoning)

    def test_partial_reservation_released_on_tier_error_or_interrupt(self):
        for error in (OSError('tier failed'), KeyboardInterrupt()):
            guard = self.guard()
            original = guard._claim
            def fail_tier(prefix, maximum):
                if prefix.startswith('tier-'):
                    raise error
                return original(prefix, maximum)
            with patch.object(guard, '_claim', side_effect=fail_tier):
                with self.assertRaises(type(error)):
                    guard.acquire()
            self.assertFalse(list(self.directory.glob('*.json')))
            with self.guard() as next_guard:
                self.assertTrue(next_guard.evidence['acquired'])

    def test_second_project_is_rejected_without_waiting(self):
        first = self.guard().acquire()
        self.addCleanup(first.release)
        with self.assertRaises(AccountGuardError) as raised:
            self.guard().acquire()
        self.assertEqual(raised.exception.evidence['reason'], 'no_free_global_slot')

    def test_slot_is_released_after_model_error(self):
        try:
            with self.guard():
                raise ValueError('model failed')
        except ValueError:
            pass
        with self.guard() as guard:
            self.assertTrue(guard.evidence['acquired'])

    def test_dead_owner_is_reclaimed_and_unknown_is_not_zero(self):
        self.directory.mkdir(parents=True)
        (self.directory / 'slot-0.json').write_text(json.dumps({
            'pid': 99999999, 'token': 'old', 'acquired_at': 1}), encoding='utf-8')
        with self.guard() as guard:
            self.assertTrue(guard.evidence['acquired'])

    def test_each_tier_fills_to_its_limit_and_reports_slots(self):
        config = {'account_guard': {'directory': str(self.directory), 'max_concurrent': 20,
                                    'tier_limits': {'cheap': 5, 'terra': 3, 'sol': 2, 'expensive': 1}}}
        cases = [('gpt-6-luna', 'medium', 5, 'cheap'),
                 ('gpt-6-sol', 'medium', 3, 'terra'),
                 ('gpt-6-sol', 'low', 2, 'sol'),
                 ('gpt-6-astra', 'low', 1, 'expensive')]
        for model, reasoning, limit, tier in cases:
            guards = [self.guard(config, model, reasoning).acquire() for _ in range(limit)]
            try:
                self.assertEqual(guards[-1].evidence['tier'], tier)
                self.assertEqual(guards[-1].evidence['occupied'][tier], limit)
                with self.assertRaises(AccountGuardError) as raised:
                    self.guard(config, model, reasoning).acquire()
                self.assertEqual(raised.exception.evidence['reason'], 'no_free_tier_slot')
            finally:
                for guard in guards:
                    guard.release()

    def test_default_global_limit_matches_all_tier_slots(self):
        config = {'account_guard': {'directory': str(self.directory)}}
        cases = [('gpt-6-luna', 'medium', 30),
                 ('gpt-6-sol', 'medium', 12),
                 ('gpt-6-sol', 'low', 4),
                 ('gpt-6-astra', 'low', 3)]
        guards = [self.guard(config, model, reasoning).acquire()
                  for model, reasoning, limit in cases
                  for _ in range(limit)]
        try:
            self.assertEqual(DEFAULT_MAX_CONCURRENT, 49)
            self.assertEqual(settings(config)['max_concurrent'], 49)
            self.assertEqual(guards[-1].evidence['occupied']['global'], 49)
        finally:
            for guard in guards:
                guard.release()

    def test_legacy_max_concurrent_caps_all_tiers(self):
        config = {'account_guard': {'directory': str(self.directory), 'max_concurrent': 1}}
        first = self.guard(config, 'gpt-6-sol', 'medium').acquire()
        self.addCleanup(first.release)
        with self.assertRaises(AccountGuardError) as raised:
            self.guard(config, 'gpt-6-luna', 'medium').acquire()
        self.assertEqual(raised.exception.evidence['reason'], 'no_free_global_slot')

    def test_complete_legacy_defaults_migrate_idempotently(self):
        legacy = {'account_guard': {
            'max_concurrent': 11,
            'tier_limits': {'cheap': 5, 'terra': 3, 'sol': 2, 'expensive': 1},
            'directory': str(self.directory),
        }}
        migrated = normalize_legacy_defaults(legacy)
        self.assertEqual(migrated['account_guard']['max_concurrent'], 49)
        self.assertEqual(migrated['account_guard']['tier_limits'], DEFAULT_TIER_LIMITS)
        self.assertEqual(migrated['account_guard']['directory'], str(self.directory))
        self.assertEqual(normalize_legacy_defaults(migrated), migrated)
        self.assertEqual(legacy['account_guard']['max_concurrent'], 11)

    def test_partial_or_custom_legacy_numbers_are_not_reinterpreted(self):
        partial = {'account_guard': {'max_concurrent': 11, 'tier_limits': {'cheap': 5}}}
        custom = {'account_guard': {'max_concurrent': 21,
                                    'tier_limits': {'cheap': 5, 'terra': 3, 'sol': 2, 'expensive': 1}}}
        self.assertEqual(normalize_legacy_defaults(partial), partial)
        self.assertEqual(normalize_legacy_defaults(custom), custom)
        self.assertEqual(settings(partial)['max_concurrent'], 11)
        self.assertEqual(settings(partial)['tier_limits']['cheap'], 5)

    def test_sol_medium_uses_implementation_tier_without_blocking_astra(self):
        config = {'account_guard': {'directory': str(self.directory), 'max_concurrent': 10,
                                    'tier_limits': {'terra': 1, 'expensive': 1}}}
        owner = self.guard(config, 'gpt-6-sol', 'medium').acquire()
        self.addCleanup(owner.release)
        self.assertEqual(owner.evidence['tier'], 'terra')
        with self.guard(config, 'gpt-6-astra', 'low') as strong:
            self.assertEqual(strong.evidence['tier'], 'expensive')
        with self.assertRaises(AccountGuardError):
            self.guard(config, 'gpt-6-sol', 'medium').acquire()

    def test_unknown_model_is_blocked_without_guessing_a_tier(self):
        with self.assertRaises(AccountGuardError) as raised:
            self.guard(model='future-model', reasoning='medium').acquire()
        self.assertEqual(raised.exception.evidence['reason'], 'unknown_model_or_reasoning')
        self.assertIsNone(raised.exception.evidence['tier'])

    def test_block_reports_one_bounded_owner_for_paired_reservation(self):
        owner = AccountGuard(self.config, 'gpt-6-luna', 'medium',
                             owner={'project': 'other-project', 'task_id': 'task-7'}).acquire()
        self.addCleanup(owner.release)
        with self.assertRaises(AccountGuardError) as raised:
            self.guard().acquire()
        owners = raised.exception.evidence['owners']
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0]['project'], 'other-project')
        self.assertEqual(owners[0]['task_id'], 'task-7')
        self.assertEqual(owners[0]['model'], 'gpt-6-luna')
        self.assertIsInstance(owners[0]['held_seconds'], int)

    def test_legacy_owner_record_is_explicitly_unknown(self):
        self.directory.mkdir(parents=True)
        (self.directory / 'global-slot-0.json').write_text(json.dumps({
            'pid': os.getpid(), 'token': 'legacy', 'acquired_at': time.time()}), encoding='utf-8')
        with self.assertRaises(AccountGuardError) as raised:
            self.guard().acquire()
        self.assertEqual(raised.exception.evidence['owners'][0]['project'], 'unknown')
        self.assertEqual(raised.exception.evidence['owners'][0]['task_id'], 'unknown')

    def test_invalid_owner_timestamp_cannot_break_capacity_diagnostic(self):
        self.directory.mkdir(parents=True)
        (self.directory / 'global-slot-0.json').write_text(json.dumps({
            'pid': os.getpid(), 'token': 'old', 'acquired_at': 10 ** 1000}), encoding='utf-8')
        with self.assertRaises(AccountGuardError) as raised:
            self.guard().acquire()
        self.assertEqual(raised.exception.evidence['reason'], 'no_free_global_slot')
        self.assertEqual(raised.exception.evidence['owners'][0]['held_seconds'], 'unknown')

    def test_pre_tier_global_slot_is_reported_as_unknown_owner(self):
        self.directory.mkdir(parents=True)
        (self.directory / 'slot-0.json').write_text(json.dumps({
            'pid': os.getpid(), 'acquired_at': time.time()}), encoding='utf-8')
        with self.assertRaises(AccountGuardError) as raised:
            self.guard().acquire()
        self.assertEqual(raised.exception.evidence['reason'], 'no_free_global_slot')
        self.assertEqual(raised.exception.evidence['owners'][0]['project'], 'unknown')


if __name__ == '__main__':
    unittest.main()
