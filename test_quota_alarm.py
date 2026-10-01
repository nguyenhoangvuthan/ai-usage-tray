"""Kiểm tra quota thật/giả lập tách biệt, backoff và alarm không báo lặp."""

import copy
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

from alarms import ResetAlarms
from claude_api import URL, ClaudeUsageClient, NoRedirect, normalize
from preferences import DEFAULTS, tray_value


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "claude"
        self.home.mkdir()
        (self.home / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "test-secret"}})
        )
        self.opener = Mock()
        self.client = ClaudeUsageClient(self.home, self.root / "state", self.opener)

    def response(self, pct=100):
        return io.BytesIO(
            json.dumps(
                {
                    "five_hour": {"utilization": pct, "resets_at": "2026-09-30T14:30:00+00:00"},
                    "seven_day": {"utilization": 44, "resets_at": None},
                }
            ).encode()
        )

    def test_100_percent_and_44_weekly_persist_without_credentials(self):
        self.opener.open.return_value = self.response()
        data, error = self.client.poll(100)
        self.assertIsNone(error)
        self.assertEqual(data["rate_limits"]["five_hour"]["used_percentage"], 100)
        self.assertEqual(data["rate_limits"]["seven_day"]["used_percentage"], 44)
        self.assertNotIn("test-secret", self.client.cache_path.read_text())
        self.assertEqual(self.client.cache_path.stat().st_mode & 0o777, 0o600)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, URL)
        self.client.poll(104)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_429_honors_retry_after_and_keeps_last_snapshot(self):
        self.opener.open.return_value = self.response()
        old, _ = self.client.poll(100)
        self.opener.open.side_effect = urllib.error.HTTPError(
            URL, 429, "", {"Retry-After": "120"}, None
        )
        retained, error = self.client.poll(105)
        self.assertEqual(retained, old)
        self.assertIn("429", error)
        self.assertEqual(self.client.next_poll, 225)
        self.client.poll(224)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_expired_credentials_and_malformed_json_are_not_zero(self):
        self.opener.open.side_effect = urllib.error.HTTPError(URL, 401, "", {}, None)
        value, error = self.client.poll(100)
        self.assertIsNone(value)
        self.assertIn("401", error)
        self.opener.open.side_effect = None
        self.opener.open.return_value = io.BytesIO(b"{broken}")
        value, error = self.client.poll(1000)
        self.assertIsNone(value)
        self.assertIsNotNone(error)

    def test_no_redirect_and_null_quota_stays_unknown(self):
        self.assertIsNone(
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")
        )
        data = normalize({"five_hour": None, "seven_day": None}, 100)
        self.assertIsNone(data["rate_limits"]["five_hour"])

    def test_expired_100_snapshot_stays_100_until_new_data(self):
        settings = copy.deepcopy(DEFAULTS)
        self.assertEqual(
            tray_value({"primary": {"used_percent": 100, "resets_at": 50}}, settings, 100), 100
        )
        settings["percentage_mode"] = "remaining"
        self.assertEqual(tray_value({"primary": {"used_percent": 100}}, settings, 100), 0)
        self.assertIsNone(tray_value({}, settings, 100))


class AlarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "alarms.json"
        self.alarm = ResetAlarms(self.path)

    def snapshot(self, used=100, reset=100, observed=90, minutes=300):
        return {
            "claude": {
                "limits": {
                    "primary": {"used_percent": used, "resets_at": reset},
                    "primary_minutes": minutes,
                    "observed_at": observed,
                }
            }
        }

    def test_due_and_confirmed_once_even_after_restart(self):
        old = self.snapshot()
        self.assertEqual(self.alarm.evaluate(old, 90), [])
        events = self.alarm.evaluate(old, 101)
        self.assertEqual([e["kind"] for e in events], ["due"])
        self.alarm = ResetAlarms(self.path)
        self.assertEqual(self.alarm.evaluate(old, 102), [])
        new = self.snapshot(0, 200, 103)
        self.assertEqual([e["kind"] for e in self.alarm.evaluate(new, 103)], ["confirmed"])
        self.alarm = ResetAlarms(self.path)
        self.assertEqual(self.alarm.evaluate(new, 104), [])

    def test_confirmed_skips_due_if_fresh_zero_arrives_first(self):
        self.alarm.evaluate(self.snapshot(), 90)
        events = self.alarm.evaluate(self.snapshot(0, None, 101), 101)
        self.assertEqual([e["kind"] for e in events], ["confirmed"])

    def test_stale_zero_does_not_confirm_reset(self):
        self.alarm.evaluate(self.snapshot(), 90)
        events = self.alarm.evaluate(self.snapshot(0, 100, 95), 101)
        self.assertEqual([e["kind"] for e in events], ["due"])

    def test_next_used_window_arms_again(self):
        self.alarm.evaluate(self.snapshot(), 90)
        self.alarm.evaluate(self.snapshot(0, 200, 101), 101)
        self.alarm.evaluate(self.snapshot(20, 200, 150), 150)
        events = self.alarm.evaluate(self.snapshot(0, 300, 201), 201)
        self.assertEqual([e["kind"] for e in events], ["confirmed"])

    def test_reset_timestamp_jitter_does_not_rearm_alarm(self):
        self.alarm.evaluate(self.snapshot(reset=100.1), 90)
        self.alarm.evaluate(self.snapshot(reset=100.5, observed=95), 95)
        self.assertEqual(self.alarm.state["claude"]["reset_at"], 100.1)
        self.assertEqual(
            [
                e["kind"]
                for e in self.alarm.evaluate(self.snapshot(reset=100.5, observed=100.2), 100.2)
            ],
            ["due"],
        )
        self.assertEqual(self.alarm.evaluate(self.snapshot(reset=100.7, observed=100.6), 100.6), [])

    def test_disabled_and_old_history_do_not_alarm(self):
        self.assertEqual(self.alarm.evaluate(self.snapshot(), 200), [])
        self.alarm.evaluate(self.snapshot(reset=300, observed=210), 210)
        self.assertEqual(
            self.alarm.evaluate(self.snapshot(reset=300, observed=210), 301, enabled=False), []
        )
        self.assertEqual(self.alarm.evaluate(self.snapshot(reset=300, observed=210), 302), [])

    def test_only_five_hour_not_weekly_and_reminder_optional(self):
        self.alarm.evaluate(self.snapshot(minutes=10080), 90)
        self.assertEqual(self.alarm.evaluate(self.snapshot(minutes=10080), 101), [])
        self.alarm.evaluate(self.snapshot(reset=200, observed=110), 110)
        self.assertEqual(
            self.alarm.evaluate(self.snapshot(reset=200, observed=110), 201, remind=False), []
        )
        self.assertEqual(
            [e["kind"] for e in self.alarm.evaluate(self.snapshot(0, 300, 202), 202, remind=False)],
            ["confirmed"],
        )


if __name__ == "__main__":
    unittest.main()
