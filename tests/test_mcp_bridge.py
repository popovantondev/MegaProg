import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_dev.mcp_bridge import serve
from ai_dev.mcp_advisory import diagnose_deployment, start_funnel


class MCPBridgeTests(unittest.TestCase):
    def test_bridge_accepts_only_the_fixed_advisory_operations(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = (json.dumps({"tool": "get_review_batch", "arguments": {
                "max_items": 1, "max_bytes": 12000}}) + "\n" +
                json.dumps({"tool": "shell", "arguments": {}}) + "\n").encode("utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                serve(Path(folder), io.BytesIO(payload))
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertTrue(replies[0]["ok"])
            self.assertFalse(replies[1]["ok"])
            self.assertIn("outside the fixed advisory contract", replies[1]["error"])

    def test_node_host_is_explicit_official_sdk_and_loopback_only(self):
        source = (Path(__file__).parents[1] / "mcp_host" / "megaprog-mcp-host.mjs").read_text()
        self.assertIn("@modelcontextprotocol/sdk", source)
        self.assertIn("StreamableHTTPServerTransport", source)
        self.assertIn('"/health"', source)
        self.assertIn("loopback address", source)
        self.assertIn("transport.handleRequest(request, response, parsedBody)", source)
        self.assertIn("Content-Length", source)
        self.assertIn("transport.onerror", source)
        self.assertIn("SDK error", source)
        self.assertIn("sessionIdGenerator: () => randomUUID()", source)
        self.assertIn("onsessioninitialized", source)
        self.assertIn("JSON_RESPONSE_MODE", source)
        self.assertIn('body.method === "notifications/initialized"', source)
        self.assertIn('!Object.hasOwn(body, "id")', source)
        self.assertIn("sendJsonNotificationAcknowledgement", source)
        self.assertIn('response.writeHead(200, { "content-type": "application/json" })', source)
        self.assertIn('request.method === "GET"', source)
        self.assertIn('response.writeHead(405', source)
        self.assertIn('request.headers["mcp-session-id"]', source)
        self.assertNotIn("Buffer.concat(chunks)", source)
        self.assertNotIn('server.tool("shell"', source)

    def test_real_sdk_probe_exercises_fixed_tools_and_validation(self):
        source = (Path(__file__).parents[1] / "mcp_host" / "verify.mjs").read_text()
        self.assertIn('client.callTool({ name: "get_review_batch"', source)
        self.assertIn('client.callTool({ name: "submit_review_batch"', source)
        self.assertIn('"0".repeat(64)', source)
        self.assertIn("invalidRejected", source)
        self.assertIn("transport.onerror", source)
        self.assertIn("host stderr", source)
        self.assertIn("preCloseSdkErrors", source)
        self.assertIn("clientRequestMethods", source)
        self.assertIn("clientResponseTypes", source)
        self.assertIn("JSON Streamable HTTP mode", source)
        self.assertIn("transport SDK|server|request", source)
        self.assertIn('error?.name === "AbortError"', source)

    def test_deployment_diagnose_is_blocked_without_tailscale(self):
        with tempfile.TemporaryDirectory() as folder, patch('ai_dev.mcp_advisory.shutil.which', return_value=None):
            result = diagnose_deployment(Path(folder))
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertFalse(result['client']['available'])
        self.assertIsNone(result['endpoint_template'])
        self.assertEqual(result['mutations'], [])

    def test_funnel_requires_explicit_public_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'confirm-public'):
                start_funnel(Path(folder), confirm_public=False)

    def test_deployment_diagnose_returns_safe_url_template(self):
        fake_status = {'BackendState': 'Running', 'Self': {
            'Online': True, 'DNSName': 'host.example.ts.net.'}}
        def fake_json(command, timeout=8):
            if command[-2:] == ['status', '--json']:
                return 0, fake_status
            return 0, None
        with patch('ai_dev.mcp_advisory.shutil.which', return_value='/usr/local/bin/tailscale'), \
             patch('ai_dev.mcp_advisory._command_json', side_effect=fake_json), \
             patch('ai_dev.mcp_advisory._tailscale_help', return_value=(True, 'funnel')):
            result = diagnose_deployment(Path('.'))
        self.assertEqual(result['state'], 'READY')
        self.assertEqual(result['endpoint_template'], 'https://host.example.ts.net/mcp')
        self.assertNotIn('AuthKey', repr(result))
