import queue
import unittest
from unittest.mock import patch
from ai_dev.discovery import Server, discover


class ProtocolTests(unittest.TestCase):
    def client(self, messages):
        server = Server.__new__(Server)
        server.serial = 0
        server.timeout = 0.01
        server.messages = queue.Queue()
        for message in messages:
            server.messages.put(message)
        server.sent = []
        server.send = server.sent.append
        return server

    def test_notifications_and_server_requests_do_not_hide_reply(self):
        server = self.client([{'method': 'notification'}, {'id': 10, 'method': 'approval'},
                              {'id': 1, 'result': {'data': []}}])
        self.assertEqual(server.call('model/list'), {'data': []})
        self.assertEqual(server.sent[1]['error']['code'], -32601)

    def test_timeout_and_eof_fail_closed(self):
        for messages in ([], [None]):
            with self.assertRaises(ValueError):
                self.client(messages).call('model/list')

    def test_protocol_error_is_not_success(self):
        with self.assertRaises(ValueError):
            self.client([{'id': 1, 'error': {'code': -32601}}]).call('model/list')

    def test_pagination_and_optional_limits_failure(self):
        with patch('ai_dev.discovery.Server') as factory:
            server = factory.return_value
            server.call.side_effect = [{}, {'data': [{'model': 'a'}], 'nextCursor': 'next'},
                                       {'data': [{'model': 'b'}]}, ValueError('unavailable')]
            result = discover('.', 'codex')
            self.assertEqual(len(result['models']), 2)
            self.assertIsNone(result['limits'])
            self.assertIn('limits', result['errors'])
            server.close.assert_called_once()

    def test_bad_page_discards_partial_catalog(self):
        with patch('ai_dev.discovery.Server') as factory:
            factory.return_value.call.side_effect = [{}, {'data': [{'model': 'a'}], 'nextCursor': 'a'}, {'data': None}]
            self.assertEqual(discover('.', 'codex')['models'], [])
