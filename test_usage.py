"""Kiểm tra số đếm, file đang ghi dở, log xoay vòng và snapshot hạn mức."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from claude_bridge import capture
from main import reset_text
from usage import Collector


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.claude = self.root / "claude"
        self.state = self.root / "state"
        self.log = self.codex / "sessions/session.jsonl"
        self.log.parent.mkdir(parents=True)
        self.clog = self.claude / "projects/p/session.jsonl"
        self.clog.parent.mkdir(parents=True)
        self.state.mkdir()
        self.collector = Collector(self.codex, self.claude, self.state, online=False)
        self.addCleanup(self.collector.close)
        self.ts = datetime.now(timezone.utc).isoformat()

    def append(self, path, data):
        with path.open("a") as f:
            f.write(json.dumps(data) + "\n")

    def codex_event(self, inp, out, ts=None, rate=None):
        return {
            "type": "event_msg",
            "timestamp": ts or self.ts,
            "payload": {
                "type": "token_count",
                "rate_limits": rate,
                "info": {
                    "total_token_usage": {
                        "input_tokens": inp,
                        "output_tokens": out,
                        "cached_input_tokens": inp // 2,
                    }
                },
            },
        }

    def claude_event(self, out):
        return {
            "type": "assistant",
            "timestamp": self.ts,
            "message": {
                "id": "msg1",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                    "output_tokens": out,
                },
            },
        }

    def total(self, provider):
        return self.collector.scan()["providers"][provider]["tokens_today"]

    def test_incremental_cumulative_and_repeated_snapshot(self):
        first = self.codex_event(100, 10)
        self.append(self.log, first)
        self.append(self.log, first)
        self.append(self.log, self.codex_event(160, 30))
        self.assertEqual(self.total("codex"), {"input": 160, "output": 30, "cache": 80})
        self.assertEqual(self.total("codex")["input"], 160)
        self.append(self.log, self.codex_event(200, 40))
        self.assertEqual(self.total("codex")["input"], 200)

    def test_claude_blocks_and_copied_transcript_not_double_counted(self):
        self.append(self.clog, self.claude_event(20))
        self.append(self.clog, self.claude_event(20))
        self.append(self.clog, self.claude_event(30))
        copied = self.clog.with_name("copy.jsonl")
        self.append(copied, self.claude_event(20))
        self.assertEqual(self.total("claude"), {"input": 130, "cache": 100, "output": 30})

    def test_partial_line_waits_until_finished(self):
        line = json.dumps(self.codex_event(100, 10))
        self.log.write_text(line[:40])
        self.assertEqual(self.total("codex")["input"], 0)
        with self.log.open("a") as f:
            f.write(line[40:] + "\n")
        self.assertEqual(self.total("codex")["input"], 100)

    def test_truncate_and_delete_rebuild(self):
        self.append(self.log, self.codex_event(100000, 10000))
        self.assertEqual(self.total("codex")["input"], 100000)
        self.log.write_text("")
        self.append(self.log, self.codex_event(10, 1))
        self.assertEqual(self.total("codex")["input"], 10)
        self.log.unlink()
        self.assertEqual(self.total("codex")["input"], 0)

    def test_counter_reset(self):
        self.append(self.log, self.codex_event(100, 10))
        self.append(self.log, self.codex_event(20, 3))
        self.assertEqual(self.total("codex")["input"], 120)

    def test_archive_copy_does_not_double_count_session(self):
        meta = {"type": "session_meta", "payload": {"id": "same-session"}}
        self.append(self.log, meta)
        self.append(self.log, self.codex_event(100, 10))
        self.append(self.log, self.codex_event(200, 20))
        archive = self.codex / "archived_sessions/copy.jsonl"
        archive.parent.mkdir()
        archive.write_bytes(self.log.read_bytes())
        self.assertEqual(self.total("codex")["input"], 200)

    def test_today_uses_delta_from_yesterday(self):
        from datetime import timedelta

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.append(self.log, self.codex_event(100, 10, ts=yesterday))
        self.append(self.log, self.codex_event(160, 25))
        self.assertEqual(self.total("codex"), {"input": 60, "cache": 30, "output": 15})

    def test_latest_rate_timestamp_wins_over_file_scan_order(self):
        from datetime import timedelta

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.append(self.log, self.codex_event(100, 10, rate={"primary": {"used_percent": 42}}))
        self.append(
            self.log,
            self.codex_event(100, 10, ts=yesterday, rate={"primary": {"used_percent": 20}}),
        )
        self.assertEqual(
            self.collector.scan()["providers"]["codex"]["limits"]["primary"]["used_percent"], 42
        )

    def test_malformed_line_isolated(self):
        self.log.write_text("{broken}\nnull\n")
        self.append(self.log, self.codex_event(100, 10))
        result = self.collector.scan()
        self.assertEqual(result["providers"]["codex"]["tokens_today"]["input"], 100)
        self.assertTrue(result["errors"])

    def test_rates_with_no_token_info(self):
        event = self.codex_event(
            1, 1, rate={"primary": {"used_percent": 42, "resets_at": 2000000000}}
        )
        event["payload"]["info"] = None
        self.append(self.log, event)
        rate = self.collector.scan()["providers"]["codex"]["limits"]
        self.assertEqual(rate["primary"]["used_percent"], 42)

    def test_model_bucket_does_not_replace_codex_bucket(self):
        self.append(self.log, self.codex_event(1, 1, rate={"primary": {"used_percent": 42}}))
        self.append(
            self.log,
            self.codex_event(2, 2, rate={"limit_id": "model-x", "primary": {"used_percent": 99}}),
        )
        self.assertEqual(
            self.collector.scan()["providers"]["codex"]["limits"]["primary"]["used_percent"], 42
        )

    def test_bridge_only_saves_limits_and_preserves_snapshot_when_missing(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            capture(
                {
                    "prompt": "must not persist",
                    "rate_limits": {"five_hour": {"used_percentage": 23, "resets_at": 2000000000}},
                }
            )
            path = self.root / "ai-usage-tray/claude-limits.json"
            self.assertNotIn("prompt", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = path.read_text()
            capture({})
            self.assertEqual(path.read_text(), original)
            self.collector.state = path.parent
            self.assertEqual(
                self.collector.scan()["providers"]["claude"]["limits"]["primary"]["used_percent"],
                23,
            )

    def test_expired_reset_not_presented_as_zero_usage(self):
        self.assertIn("chờ dữ liệu mới", reset_text(100, 200))
        self.assertIn("1 giờ", reset_text(3800, 100))


if __name__ == "__main__":
    unittest.main()
