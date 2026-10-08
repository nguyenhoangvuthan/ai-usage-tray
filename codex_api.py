"""Hỏi quota qua Codex app-server; chỉ initialize và account/rateLimits/read."""

import json
import math
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path

from claude_api import atomic_json


class RpcError(Exception):
    def __init__(self, error):
        super().__init__("Codex app-server từ chối yêu cầu quota")
        message = str(error.get("message", ""))
        self.rate_limited = "429" in message or "too many requests" in message.lower()
        self.auth_required = any(
            s in message.lower() for s in ("401", "403", "authentication", "unauthorized")
        )
        self.retry_after = 0
        data = error.get("data")
        if isinstance(data, dict):
            value = data.get("retryAfterSeconds")
            if isinstance(value, (float, int)) and math.isfinite(value):
                self.retry_after = max(0, value)


class AppServer:
    def __init__(self, home: Path, command=None, timeout=4):
        self.home = home
        self.command = command
        self.timeout = timeout
        self.process = None
        self.selector = None
        self.buffer = b""
        self.request_id = 0

    def _send(self, message):
        line = (json.dumps(message) + "\n").encode()
        self.process.stdin.write(line)
        self.process.stdin.flush()

    def _request(self, method, params=None):
        self.request_id += 1
        request_id = self.request_id
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    raise TimeoutError("Codex quota timeout")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise ConnectionError("Codex app-server đã đóng kết nối")
                self.buffer += chunk
                if len(self.buffer) > 4 * 1024 * 1024:
                    raise ValueError("Phản hồi app-server quá lớn")
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Phản hồi RPC không hợp lệ")
                if "method" in value:
                    if "id" in value:
                        self._send(
                            {
                                "id": value["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "Usage monitor supports quota reads only",
                                },
                            }
                        )
                    continue
                if value.get("id") != request_id:
                    continue
                if "error" in value:
                    raise RpcError(value["error"])
                return value["result"]
        raise TimeoutError("Codex quota timeout")

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        command = self.command
        if command is None:
            executable = shutil.which("codex")
            if executable is None:
                fallback = Path.home() / ".local/bin/codex"
                if fallback.is_file():
                    executable = str(fallback)
            if executable is None:
                raise FileNotFoundError("Không tìm thấy Codex CLI")
            command = [executable, "app-server", "--listen", "stdio://"]
        env = dict(os.environ, CODEX_HOME=str(self.home))
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=env,
            cwd=str(self.home),
            start_new_session=True,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self._request(
            "initialize",
            {
                "clientInfo": {"name": "ai_usage_tray", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False},
            },
        )
        self._send({"method": "initialized"})

    def read_limits(self):
        self._start()
        return self._request("account/rateLimits/read")

    def close(self):
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()
            self.process = None
        self.buffer = b""


def normalize(data: dict, now: float) -> dict:
    buckets = data.get("rateLimitsByLimitId") or {}
    bucket = buckets.get("codex") or data.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, "codex"):
        raise ValueError("Thiếu bucket Codex")
    if not any(k in bucket for k in ("primary", "secondary")):
        raise ValueError("Thiếu cửa sổ quota")
    result = {"observed_at": now, "source": "Codex · quota trực tiếp (app-server)"}
    for key in ("primary", "secondary"):
        window = bucket.get(key)
        result[key] = None
        result[key + "_minutes"] = None
        if window is None:
            continue
        percent = window.get("usedPercent")
        duration = window.get("windowDurationMins")
        reset = window.get("resetsAt")
        for value in (percent, duration):
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("Quota Codex không hợp lệ")
        if reset is not None and (not isinstance(reset, (int, float)) or not math.isfinite(reset)):
            raise ValueError("Mốc reset không hợp lệ")
        result[key] = {"used_percent": percent, "resets_at": reset}
        result[key + "_minutes"] = duration
    return result


class CodexUsageClient:
    def __init__(self, home: Path, state: Path, transport=None):
        self.transport = transport or AppServer(home)
        self.cache_path = state / "codex-api-limits.json"
        self.snapshot = None
        self.next_poll = 0
        self.failures = 0
        self.error = None
        try:
            value = json.loads(self.cache_path.read_text())
            if isinstance(value, dict) and "observed_at" in value and "primary" in value:
                self.snapshot = value
        except (OSError, ValueError):
            pass

    def poll(self, now=None):
        live_clock = now is None
        now = time.time() if live_clock else now
        if now < self.next_poll:
            return self.snapshot, self.error
        self.next_poll = now + 5
        try:
            value = self.transport.read_limits()
            self.snapshot = normalize(value, time.time() if live_clock else now)
            self.failures = 0
            self.error = None
            try:
                atomic_json(self.cache_path, self.snapshot)
            except OSError:
                self.error = "Codex: đọc quota thành công nhưng không lưu được cache."
        except (OSError, ValueError, TypeError, AttributeError, KeyError, RpcError) as exc:
            self.failures += 1
            delay = min(30, 5 * 2 ** min(self.failures, 3))
            if isinstance(exc, RpcError) and exc.auth_required:
                delay = 60
            if isinstance(exc, RpcError) and exc.rate_limited:
                delay = max(60, delay, exc.retry_after)
            self.next_poll = now + delay
            if isinstance(exc, FileNotFoundError):
                self.error = "Codex: chưa có CLI hoặc thư mục CODEX_HOME."
            elif isinstance(exc, RpcError) and exc.auth_required:
                self.error = "Codex: cần đăng nhập tài khoản ChatGPT qua Codex CLI."
            else:
                self.error = f"Codex: chưa cập nhật được quota; thử lại sau {int(delay)} giây."
            self.transport.close()
        return self.snapshot, self.error

    def close(self):
        self.transport.close()
