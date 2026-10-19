"""Quota nền: gia hạn OAuth, tránh gọi dồn và phục hồi khi mạng trở lại."""

import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

from claude_api import URL, ClaudeUsageClient, refresh_lock
from codex_api import CodexUsageClient
from usage import Collector

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"


def response(data):
    return io.BytesIO(json.dumps(data).encode())


def quota(percent=24):
    return response({"five_hour": {"utilization": percent, "resets_at": None}})


def refreshed(**overrides):
    return response(
        {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 28800,
            "scope": "user:profile user:inference",
            **overrides,
        }
    )


class BackgroundQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "claude"
        self.home.mkdir()
        self.path = self.home / ".credentials.json"
        self.credentials = {
            "otherCredential": {"keep": True},
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 99000,
                "scopes": ["user:profile", "user:inference"],
                "subscriptionType": "max",
                "rateLimitTier": "default",
            },
        }
        self.path.write_text(json.dumps(self.credentials))
        self.opener = Mock()
        self.client = ClaudeUsageClient(self.home, self.root / "state", self.opener)

    def test_expired_token_refreshes_before_quota_and_preserves_credentials(self):
        def serve(request, timeout):
            if request.full_url == TOKEN_URL:
                body = json.loads(request.data)
                self.assertEqual(body["grant_type"], "refresh_token")
                self.assertEqual(body["refresh_token"], "old-refresh")
                self.assertEqual(body["scope"], "user:profile user:inference")
                self.assertEqual(request.get_method(), "POST")
                return refreshed()
            self.assertEqual(request.full_url, URL)
            self.assertEqual(request.get_header("Authorization"), "Bearer new-access")
            return quota()

        self.opener.open.side_effect = serve
        data, error = self.client.poll(100)
        self.assertIsNone(error)
        self.assertEqual(data["rate_limits"]["five_hour"]["used_percentage"], 24)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["claudeAiOauth"]["refreshToken"], "new-refresh")
        self.assertEqual(saved["claudeAiOauth"]["expiresAt"], 28900000)
        self.assertEqual(saved["claudeAiOauth"]["subscriptionType"], "max")
        self.assertEqual(saved["otherCredential"], {"keep": True})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("new-access", self.client.cache_path.read_text())
        self.assertFalse((self.home / ".oauth_refresh.lock").exists())

    def test_401_refreshes_once_and_retries_quota_without_terminal(self):
        self.credentials["claudeAiOauth"]["expiresAt"] = 999999999
        self.path.write_text(json.dumps(self.credentials))
        self.opener.open.side_effect = [
            urllib.error.HTTPError(URL, 401, "", {}, None),
            refreshed(),
            quota(32),
        ]
        data, error = self.client.poll(100)
        self.assertIsNone(error)
        self.assertEqual(data["rate_limits"]["five_hour"]["used_percentage"], 32)

    def test_invalid_refresh_does_not_overwrite_credentials_or_fake_zero(self):
        original = self.path.read_text()
        for bad in [
            {"access_token": "new-access"},
            {"access_token": "new-access", "expires_in": -1},
            {"access_token": "new-access", "expires_in": True},
        ]:
            with self.subTest(bad=bad):
                self.opener.open.return_value = response(bad)
                client = ClaudeUsageClient(self.home, self.root / "state", self.opener)
                data, error = client.poll(100)
                self.assertIsNone(data)
                self.assertIsNotNone(error)
                self.assertEqual(self.path.read_text(), original)

    def test_cli_refresh_lock_prevents_concurrent_rotation(self):
        for lock in [self.home / ".oauth_refresh.lock", Path(str(self.home.resolve()) + ".lock")]:
            with self.subTest(lock=lock.name):
                lock.mkdir()
                self.opener.open.side_effect = AssertionError("Must not rotate locked tokens")
                data, error = self.client.poll(100)
                self.assertIsNone(data)
                self.assertIsNotNone(error)
                self.assertEqual(json.loads(self.path.read_text()), self.credentials)
                self.assertTrue(lock.is_dir())
                lock.rmdir()
                self.client.next_poll = 0

    def test_account_changed_during_refresh_is_not_overwritten(self):
        def serve(request, timeout):
            if request.full_url == TOKEN_URL:
                self.path.write_text(
                    json.dumps({"claudeAiOauth": {"accessToken": "other-account"}})
                )
                return refreshed()
            self.assertEqual(request.get_header("Authorization"), "Bearer other-account")
            return quota(15)

        self.opener.open.side_effect = serve
        data, error = self.client.poll(100)
        self.assertIsNone(error)
        self.assertEqual(data["rate_limits"]["five_hour"]["used_percentage"], 15)
        self.assertEqual(
            json.loads(self.path.read_text())["claudeAiOauth"]["accessToken"], "other-account"
        )

    def test_stale_cli_locks_do_not_require_terminal_to_recover(self):
        for lock in [self.home / ".oauth_refresh.lock", Path(str(self.home.resolve()) + ".lock")]:
            lock.mkdir()
            os.utime(lock, (time.time() - 120, time.time() - 120))
        self.opener.open.side_effect = [refreshed(), quota(28)]
        data, error = self.client.poll(100)
        self.assertIsNone(error)
        self.assertEqual(data["rate_limits"]["five_hour"]["used_percentage"], 28)

    def test_second_401_stops_refresh_loop_and_retains_snapshot(self):
        self.path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old-access"}}))
        self.opener.open.return_value = quota(40)
        old, _ = self.client.poll(100)
        self.credentials["claudeAiOauth"]["expiresAt"] = 999999999
        self.path.write_text(json.dumps(self.credentials))
        self.opener.open.side_effect = [
            urllib.error.HTTPError(URL, 401, "", {}, None),
            refreshed(),
            urllib.error.HTTPError(URL, 401, "", {}, None),
            AssertionError("Must not loop on invalid login"),
        ]
        data, error = self.client.poll(160)
        self.assertEqual(data, old)
        self.assertIn("đăng nhập", error)

    def test_refresh_outage_still_reads_quota_with_unexpired_access_token(self):
        self.credentials["claudeAiOauth"]["expiresAt"] = 200000
        self.path.write_text(json.dumps(self.credentials))
        self.opener.open.side_effect = [urllib.error.URLError("refresh offline"), quota(31)]
        data, error = self.client.poll(100)
        self.assertIsNone(error)
        self.assertEqual(data["rate_limits"]["five_hour"]["used_percentage"], 31)
        self.assertEqual(json.loads(self.path.read_text()), self.credentials)

    def test_server_outage_does_not_delay_recovery_for_minutes(self):
        self.path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old-access"}}))

        def unavailable(*args, **kwargs):
            raise urllib.error.HTTPError(URL, 503, "", {}, None)

        self.opener.open.side_effect = unavailable
        for now in range(100, 340, 30):
            data, error = self.client.poll(now)
            self.assertIsNone(data)
            self.assertIn("503", error)
            self.assertLessEqual(self.client.next_poll - now, 30)

    def test_metadata_change_during_refresh_keeps_rotated_tokens(self):
        def serve(request, timeout):
            if request.full_url == TOKEN_URL:
                changed = json.loads(self.path.read_text())
                changed["claudeAiOauth"]["rateLimitTier"] = "new-tier"
                self.path.write_text(json.dumps(changed))
                return refreshed()
            self.assertEqual(request.get_header("Authorization"), "Bearer new-access")
            return quota(23)

        self.opener.open.side_effect = serve
        data, error = self.client.poll(100)
        self.assertIsNone(error)
        self.assertEqual(data["rate_limits"]["five_hour"]["used_percentage"], 23)
        saved = json.loads(self.path.read_text())["claudeAiOauth"]
        self.assertEqual(saved["refreshToken"], "new-refresh")
        self.assertEqual(saved["rateLimitTier"], "new-tier")

    def test_reclaimed_refresh_locks_are_not_removed_by_old_holder(self):
        with refresh_lock(self.home):
            for lock in [
                self.home / ".oauth_refresh.lock",
                Path(str(self.home.resolve()) + ".lock"),
            ]:
                lock.rmdir()
                lock.mkdir()
        self.assertTrue((self.home / ".oauth_refresh.lock").is_dir())
        self.assertTrue(Path(str(self.home.resolve()) + ".lock").is_dir())

    def test_quota_does_not_poll_again_at_each_five_second_ui_tick(self):
        self.credentials["claudeAiOauth"]["expiresAt"] = 999999999
        self.path.write_text(json.dumps(self.credentials))
        self.opener.open.side_effect = [quota(10), quota(20)]
        self.client.poll(100)
        for now in range(105, 160, 5):
            self.assertEqual(self.client.poll(now)[0]["observed_at"], 100)
        self.assertEqual(
            self.client.poll(160)[0]["rate_limits"]["five_hour"]["used_percentage"], 20
        )

    def test_restart_keeps_server_retry_after(self):
        # No expiry field: legacy credentials should still work without refresh.
        self.path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old-access"}}))
        self.opener.open.side_effect = urllib.error.HTTPError(
            URL, 429, "", {"Retry-After": "120"}, None
        )
        self.client.poll(100)
        resumed = ClaudeUsageClient(self.home, self.root / "state", self.opener)
        self.opener.open.side_effect = AssertionError("Retry-After must survive restart")
        data, error = resumed.poll(150)
        self.assertIsNone(data)
        self.assertIn("429", error)

    def test_network_recovery_retries_within_thirty_seconds_for_both_providers(self):
        self.path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old-access"}}))
        transport = Mock()
        codex = CodexUsageClient(self.root, self.root / "codex-state", transport)
        self.addCleanup(codex.close)
        self.opener.open.side_effect = urllib.error.URLError("offline")
        transport.read_limits.side_effect = ConnectionError("offline")
        now = 100
        for _ in range(8):
            self.client.poll(now)
            codex.poll(now)
            self.assertLessEqual(self.client.next_poll - now, 30)
            self.assertLessEqual(codex.next_poll - now, 30)
            now += 30
        self.opener.open.side_effect = None
        self.opener.open.return_value = quota()
        transport.read_limits.side_effect = None
        transport.read_limits.return_value = {
            "rateLimits": {
                "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 30000},
            }
        }
        self.assertIsNone(self.client.poll(now)[1])
        self.assertIsNone(codex.poll(now)[1])

    def test_collector_updates_web_quota_with_no_session_logs(self):
        self.path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old-access"}}))
        collector = Collector(self.root / "codex", self.home, self.root / "collector-state")
        self.addCleanup(collector.close)
        collector.claude_api.opener = self.opener
        self.opener.open.side_effect = [quota(21), quota(36)]
        collector.codex_api.transport = Mock()
        collector.codex_api.transport.read_limits.return_value = {
            "rateLimits": {
                "primary": {"usedPercent": 17, "windowDurationMins": 300, "resetsAt": 30000},
            }
        }
        first = collector.scan()
        self.assertEqual(first["files"], 0)
        self.assertEqual(first["errors"], [])
        self.assertEqual(first["providers"]["claude"]["limits"]["primary"]["used_percent"], 21)
        self.assertEqual(first["providers"]["codex"]["limits"]["primary"]["used_percent"], 17)
        collector.claude_api.next_poll = 0
        second = collector.scan()
        self.assertEqual(second["providers"]["claude"]["limits"]["primary"]["used_percent"], 36)

    def test_logged_out_claude_exposes_error_with_old_quota_and_recovers_after_login(self):
        state = self.root / "collector-state"
        state.mkdir()
        (state / "claude-api-limits.json").write_text(json.dumps({
            "observed_at": 100,
            "rate_limits": {"five_hour": {"used_percentage": 82}},
            "source": "Claude · quota trực tiếp",
        }))
        self.path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "", "refreshToken": "", "expiresAt": 0,
        }}))
        collector = Collector(self.root / "codex", self.home, state)
        self.addCleanup(collector.close)
        collector.claude_api.opener = self.opener
        collector.codex_api.transport = Mock()
        collector.codex_api.transport.read_limits.return_value = {
            "rateLimits": {"primary": {"usedPercent": 17, "windowDurationMins": 300}},
        }

        first = collector.scan()["providers"]
        self.assertIn("claude auth login", first["claude"].get("quota_error") or "")
        self.assertEqual(first["claude"]["limits"]["observed_at"], 100)
        self.assertEqual(first["claude"]["limits"]["primary"]["used_percent"], 82)
        self.assertIsNone(first["codex"]["quota_error"])

        self.path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "new-login"}}))
        collector.claude_api.next_poll = 0
        self.opener.open.return_value = quota(21)
        second = collector.scan()["providers"]["claude"]
        self.assertIsNone(second["quota_error"])
        self.assertEqual(second["limits"]["primary"]["used_percent"], 21)
        self.assertGreater(second["limits"]["observed_at"], 100)


if __name__ == "__main__":
    unittest.main()
