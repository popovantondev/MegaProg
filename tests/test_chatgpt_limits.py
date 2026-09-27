import json
import tempfile
import unittest
from pathlib import Path

from ai_dev.chatgpt_limits import availability, load, report, update
from ai_dev.review import build_chatgpt_request, build_review_batch, import_review, repo_state_hash, CONTRACT_VERSION


class ChatGPTLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.git').mkdir()

    def eligible(self):
        runs = self.root / '.ai-dev' / 'runs' / 'blocked'
        runs.mkdir(parents=True)
        (runs / 'report.json').write_text(json.dumps({
            'id': 'blocked', 'status': 'BLOCKED', 'advisory_reason': 'diagnosis'
        }), encoding='utf-8')

    def test_default_unknown_and_all_status_actions(self):
        self.assertEqual(report(self.root)['status'], 'UNKNOWN')
        self.assertEqual(availability({'manual_status': 'available'})['status'], 'AVAILABLE')
        self.assertEqual(availability({'manual_status': 'exhausted'})['status'], 'EXHAUSTED')
        self.assertEqual(availability({'manual_status': 'unknown'})['status'], 'UNKNOWN')

    def test_manual_ledger_and_limit_exhaustion(self):
        result = update(self.root, status='available', limit_turns=3,
                        reserved_turns=1, spent_turns=1, reset_at='tomorrow')
        self.assertEqual(result['status'], 'AVAILABLE')
        self.assertEqual(result['remaining_turns'], 1)
        result = update(self.root, spent_turns=2)
        self.assertEqual(result['status'], 'EXHAUSTED')
        self.assertEqual(load(self.root)['reset_at'], 'tomorrow')

    def test_card_is_skipped_without_ledger_and_generation_does_not_spend(self):
        self.eligible()
        card = build_chatgpt_request(self.root)
        self.assertEqual(card['status'], 'CHATGPT_ADVISORY_SKIPPED')
        self.assertEqual(card['availability'], 'UNKNOWN')
        self.assertFalse((self.root / '.ai-dev' / 'chatgpt-ledger.json').exists())

    def test_import_is_the_only_spend_event(self):
        self.eligible()
        update(self.root, status='available')
        card = build_chatgpt_request(self.root)
        self.assertEqual(card['status'], 'READY')
        self.assertEqual(report(self.root)['spent_turns'], 0)
        item = build_review_batch(self.root, max_items=1)['items'][0]
        response_path = self.root / 'response.json'
        response_path.write_text(json.dumps({
            'contract_version': CONTRACT_VERSION, 'task_id': item['task_id'],
            'task_hash': item['task_hash'], 'bundle_id': item['bundle_id'] if 'bundle_id' in item else card['packet']['bundle_id'],
            'repo_state_hash': repo_state_hash(self.root), 'advisory': 'hint'
        }), encoding='utf-8')
        imported = import_review(self.root, response_path)
        self.assertEqual(imported['status'], 'ACCEPTED_AS_HINT')
        self.assertEqual(report(self.root)['spent_turns'], 1)

        duplicate = import_review(self.root, response_path)
        self.assertEqual(duplicate['status'], 'DUPLICATE')
        self.assertEqual(report(self.root)['spent_turns'], 1)

    def test_availability_tolerates_malformed_read_only_input(self):
        self.assertEqual(availability({'manual_status': 'available',
                                       'reserved_turns': 'bad', 'spent_turns': None})['status'],
                         'AVAILABLE')

    def test_independent_configurable_buckets_and_expired_snapshot_fail_closed(self):
        update(self.root, bucket='chat_reasoning', status='available', observed_at='2026-09-15T00:00:00Z',
               reset_at='2099-01-01T00:00:00Z', limit_turns=4, source='manual_ui', confidence='medium')
        update(self.root, bucket='chat_instant', status='exhausted', observed_at='2026-09-15T00:00:00Z',
               reset_at='2099-01-01T00:00:00Z', used=2, source='unknown', notes='separate bucket')
        self.assertEqual(report(self.root)['buckets']['chat_reasoning']['status'], 'AVAILABLE')
        self.assertEqual(report(self.root)['buckets']['chat_instant']['status'], 'EXHAUSTED')
        update(self.root, bucket='chat_reasoning', reset_at='2000-01-01T00:00:00Z')
        self.assertEqual(report(self.root)['buckets']['chat_reasoning']['status'], 'UNKNOWN')
        self.assertIn('reset_at', report(self.root)['buckets']['chat_reasoning']['reason'])

    def test_legacy_v1_ledger_migrates_to_default_bucket(self):
        path = self.root / '.ai-dev' / 'chatgpt-ledger.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'version': 1, 'limit_known': True, 'limit_turns': 5,
                                    'reserved_turns': 1, 'spent_turns': 2,
                                    'manual_status': 'available', 'reset_at': 'tomorrow'}), encoding='utf-8')
        migrated = load(self.root)
        self.assertEqual(migrated['default_bucket'], 'chat_pro')
        self.assertEqual(migrated['buckets']['chat_pro']['limit'], 5)
        self.assertEqual(migrated['buckets']['chat_pro']['used'], 3)
        self.assertEqual(report(self.root)['status'], 'AVAILABLE')

    def test_fresh_import_charges_only_selected_bucket_and_duplicate_is_idempotent(self):
        self.eligible()
        update(self.root, bucket='chat_reasoning', status='available', observed_at='2026-09-15T00:00:00Z')
        card = build_chatgpt_request(self.root, bucket='chat_reasoning')
        item = build_review_batch(self.root, max_items=1)['items'][0]
        response_path = self.root / 'selected-response.json'
        response_path.write_text(json.dumps({
            'contract_version': CONTRACT_VERSION, 'task_id': item['task_id'],
            'task_hash': item['task_hash'], 'bundle_id': card['packet']['bundle_id'],
            'repo_state_hash': repo_state_hash(self.root), 'advisory': 'hint',
            'chatgpt_bucket': 'chat_reasoning'}), encoding='utf-8')
        self.assertEqual(import_review(self.root, response_path)['status'], 'ACCEPTED_AS_HINT')
        snapshot = report(self.root)
        self.assertEqual(snapshot['buckets']['chat_reasoning']['used'], 1)
        self.assertIsNone(snapshot['buckets']['chat_pro']['used'])
        self.assertEqual(import_review(self.root, response_path)['status'], 'DUPLICATE')
        self.assertEqual(report(self.root)['buckets']['chat_reasoning']['used'], 1)


if __name__ == '__main__':
    unittest.main()
