"""TLS failure on one address must not prevent quota updates via another."""

import io
import json
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from claude_api import ClaudeUsageClient


class ClaudeNetworkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "test-access"},
        }))
        self.ipv6 = Mock(spec=socket.socket)
        self.ipv4 = Mock(spec=socket.socket)
        self.addresses = []
        self.handshakes = []
        self.tls_error = TimeoutError("TLS handshake timed out")
        self.reply(200, {"five_hour": {"utilization": 42, "resets_at": None}})

        def connect(address, timeout, source_address=None):
            self.addresses.append(address[0])
            return self.ipv4 if address[0] == "127.0.0.1" else self.ipv6

        def wrap(context, sock, *, server_hostname):
            self.assertEqual(server_hostname, "api.anthropic.com")
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.handshakes.append(sock)
            if sock is self.ipv6:
                raise self.tls_error
            return sock

        for target, kwargs in [
            ("urllib.request.getproxies", {"return_value": {}}),
            ("socket.getaddrinfo", {"return_value": [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]}),
            ("socket.create_connection", {"side_effect": connect}),
            ("ssl.SSLContext.wrap_socket", {"autospec": True, "side_effect": wrap}),
        ]:
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = ClaudeUsageClient(self.root, self.root / "state")

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.ipv4.makefile.return_value = io.BytesIO(
            f"HTTP/1.1 {status} Response\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            + body
        )

    def test_ipv6_tls_timeout_falls_back_and_updates_quota(self):
        snapshot, error = self.client.poll(100)
        self.assertIsNone(error)
        self.assertEqual(snapshot["rate_limits"]["five_hour"]["used_percentage"], 42)
        self.assertEqual(self.addresses, ["::1", "127.0.0.1"])
        self.ipv6.sendall.assert_not_called()
        self.ipv6.close.assert_called()
        self.assertEqual(json.loads(self.client.cache_path.read_text()), snapshot)

    def test_certificate_failure_is_not_retried_on_another_address(self):
        self.tls_error = ssl.SSLCertVerificationError("invalid certificate")
        snapshot, error = self.client.poll(100)
        self.assertIsNone(snapshot)
        self.assertIsNotNone(error)
        self.assertEqual(self.handshakes, [self.ipv6])
        self.ipv6.sendall.assert_not_called()

    def test_http_rate_limit_after_fallback_keeps_backoff(self):
        self.reply(429, {"error": "rate limited"})
        snapshot, error = self.client.poll(100)
        self.assertIsNone(snapshot)
        self.assertIn("429", error)
        self.assertEqual(self.client.next_poll, 160)
        self.assertTrue(self.client.retry_path.exists())
        self.client.poll(105)
        self.assertEqual(len(self.handshakes), 2)


if __name__ == "__main__":
    unittest.main()
