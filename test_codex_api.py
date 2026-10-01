"""Kiểm tra RPC chỉ đọc quota, kết nối dùng lại, timeout và ưu tiên dữ liệu trực tiếp."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from codex_api import AppServer, CodexUsageClient, RpcError, normalize
from usage import Collector

RESPONSE = {
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 20000},
        "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 30000},
    }
}


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transport = Mock()
        self.transport.read_limits.return_value = copy.deepcopy(RESPONSE)
        self.client = CodexUsageClient(self.root, self.root / "state", self.transport)
        self.addCleanup(self.client.close)

    def test_normalizes_100_and_zero_without_double_scaling(self):
        data, error = self.client.poll(100)
        self.assertIsNone(error)
        self.assertEqual(data["primary"]["used_percent"], 100)
        self.assertEqual(data["primary_minutes"], 300)
        self.transport.read_limits.return_value["rateLimits"]["primary"]["usedPercent"] = 0
        self.assertEqual(self.client.poll(105)[0]["primary"]["used_percent"], 0)

    def test_prefers_named_codex_bucket_and_rejects_unrelated_bucket(self):
        data = {
            "rateLimits": {"limitId": "other"},
            "rateLimitsByLimitId": {"codex": RESPONSE["rateLimits"]},
        }
        self.assertEqual(normalize(data, 100)["primary"]["used_percent"], 100)
        del data["rateLimitsByLimitId"]["codex"]
        with self.assertRaises(ValueError):
            normalize(data, 100)

    def test_null_window_is_unknown_and_weekly_is_not_labeled_5h(self):
        data = copy.deepcopy(RESPONSE)
        data["rateLimits"]["primary"]["windowDurationMins"] = 10080
        data["rateLimits"]["secondary"] = None
        value = normalize(data, 100)
        self.assertEqual(value["primary_minutes"], 10080)
        self.assertIsNone(value["secondary"])

    def test_invalid_quota_rejected(self):
        for bad in [None, True, -1, float("nan"), "100"]:
            data = copy.deepcopy(RESPONSE)
            data["rateLimits"]["primary"]["usedPercent"] = bad
            with self.assertRaises(ValueError):
                normalize(data, 100)

    def test_cache_private_and_poll_interval(self):
        first, _ = self.client.poll(100)
        self.client.poll(104)
        self.assertEqual(self.transport.read_limits.call_count, 1)
        self.assertEqual(self.client.cache_path.stat().st_mode & 0o777, 0o600)
        resumed = CodexUsageClient(self.root, self.root / "state", self.transport)
        self.assertEqual(resumed.snapshot, first)

    def test_failure_preserves_age_and_backoff(self):
        first, _ = self.client.poll(100)
        self.transport.read_limits.side_effect = TimeoutError()
        stale, error = self.client.poll(105)
        self.assertEqual(stale, first)
        self.assertIn("thử lại", error)
        self.assertGreater(self.client.next_poll, 105)
        self.transport.close.assert_called_once()

    def test_rate_limit_and_auth_errors(self):
        self.transport.read_limits.side_effect = RpcError(
            {"message": "HTTP 429", "data": {"retryAfterSeconds": 120}}
        )
        self.assertIsNone(self.client.poll(100)[0])
        self.assertEqual(self.client.next_poll, 220)
        self.transport.read_limits.side_effect = RpcError({"message": "authentication required"})
        self.assertIn("đăng nhập", self.client.poll(221)[1])

    def test_collector_prefers_direct_quota_over_newer_log(self):
        collector = Collector(self.root, self.root, self.root / "state", online=True)
        self.addCleanup(collector.close)
        direct = normalize(RESPONSE, 100)
        collector.codex_api.poll = Mock(return_value=(direct, None))
        collector.claude_api.poll = Mock(return_value=(None, None))
        collector.limits["codex"] = {"observed_at": 200, "primary": {"used_percent": 5}}
        actual = collector.scan()["providers"]["codex"]["limits"]
        self.assertEqual(actual, direct)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def start(self, script, timeout=1):
        path = self.root / "fake_server.py"
        path.write_text(script)
        transport = AppServer(self.root, [sys.executable, "-u", str(path)], timeout=timeout)
        self.addCleanup(transport.close)
        return transport

    def test_handshake_notifications_chunks_and_reused_process(self):
        transport = self.start("""import json,sys,os
for line in sys.stdin:
 data=json.loads(line)
 with open('requests.jsonl','a') as f:f.write(line)
 method=data.get('method')
 if method=='initialized':continue
 if method=='initialize':result={}
 elif method=='account/rateLimits/read':result={'rateLimits':{'limitId':'codex','primary':{'usedPercent':100,'windowDurationMins':300,'resetsAt':20000},'secondary':None}}
 else:raise RuntimeError('Unexpected method')
 print(json.dumps({'method':'unrelated/notification','params':{}}),flush=True)
 output=(json.dumps({'id':data['id'],'result':result})+'\\n').encode()
 os.write(1,output[:7]);os.write(1,output[7:])
""")
        self.assertEqual(transport.read_limits()["rateLimits"]["primary"]["usedPercent"], 100)
        process = transport.process
        transport.read_limits()
        self.assertIs(transport.process, process)
        methods = [
            json.loads(line)["method"]
            for line in (self.root / "requests.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            methods,
            ["initialize", "initialized", "account/rateLimits/read", "account/rateLimits/read"],
        )
        transport.close()
        self.assertIsNotNone(process.poll())

    def test_hung_server_times_out_and_is_reaped(self):
        transport = self.start("import time\ntime.sleep(30)\n", timeout=0.1)
        with self.assertRaises(TimeoutError):
            transport.read_limits()
        process = transport.process
        transport.close()
        self.assertIsNotNone(process.poll())

    def test_crashed_server_does_not_block(self):
        transport = self.start("raise SystemExit(1)\n")
        with self.assertRaises(ConnectionError):
            transport.read_limits()


if __name__ == "__main__":
    unittest.main()
