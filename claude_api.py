"""Đọc quota từ endpoint mà Claude Code dùng; không gửi prompt hay refresh token."""

import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

URL = "https://api.anthropic.com/api/oauth/usage"


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".usage-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def normalize(data: dict, now: float) -> dict:
    if not isinstance(data, dict) or not any(k in data for k in ("five_hour", "seven_day")):
        raise ValueError("Không có trường quota")
    rates = {}
    for key in ("five_hour", "seven_day"):
        value = data.get(key)
        if value is None:
            rates[key] = None
            continue
        pct = value.get("utilization")
        if not isinstance(pct, (float, int)) or not math.isfinite(pct) or pct < 0:
            raise ValueError("Quota không hợp lệ")
        reset = value.get("resets_at")
        if reset is not None:
            reset = (
                datetime.fromisoformat(reset).timestamp()
                if isinstance(reset, str)
                else float(reset)
            )
            if not math.isfinite(reset):
                raise ValueError("Thời điểm reset không hợp lệ")
        rates[key] = {"used_percentage": pct, "resets_at": reset}
    return {"observed_at": now, "rate_limits": rates, "source": "Claude · quota trực tiếp"}


class ClaudeUsageClient:
    def __init__(self, home: Path, state: Path, opener=None):
        self.home = home
        self.cache_path = state / "claude-api-limits.json"
        self.opener = opener or urllib.request.build_opener(NoRedirect)
        self.snapshot = None
        self.next_poll = 0.0
        self.failures = 0
        self.error = None
        try:
            value = json.loads(self.cache_path.read_text())
            if isinstance(value, dict) and isinstance(value.get("rate_limits"), dict):
                self.snapshot = value
        except (OSError, ValueError):
            pass

    def poll(self, now=None):
        live_clock = now is None
        now = time.time() if now is None else now
        if now < self.next_poll:
            return self.snapshot, self.error
        self.next_poll = now + 5
        try:
            credential = json.loads((self.home / ".credentials.json").read_text())
            token = credential.get("claudeAiOauth", {}).get("accessToken")
            if not token:
                self.error = "Claude: cần đăng nhập Claude Code để đọc quota trực tiếp."
                return self.snapshot, self.error
            request = urllib.request.Request(
                URL,
                headers={
                    "Authorization": "Bearer " + token,
                    "anthropic-beta": "oauth-2025-04-20",
                    "Accept": "application/json",
                    "User-Agent": "ai-usage-tray/0.1.0",
                },
            )
            with self.opener.open(request, timeout=4) as response:
                data = json.load(response)
            self.snapshot = normalize(data, time.time() if live_clock else now)
            self.failures = 0
            self.error = None
            try:
                atomic_json(self.cache_path, self.snapshot)
            except OSError:
                self.error = "Claude: đọc quota thành công nhưng chưa lưu được cache."
        except FileNotFoundError:
            self.error = "Claude: chưa có đăng nhập cục bộ; dùng statusLine nếu có."
        except urllib.error.HTTPError as exc:
            self.failures += 1
            delay = min(900, 60 * 2 ** min(self.failures - 1, 4))
            if exc.code == 429:
                try:
                    retry = exc.headers.get("Retry-After", "")
                    delay = max(
                        delay,
                        float(retry)
                        if retry.isdigit()
                        else parsedate_to_datetime(retry).timestamp() - now,
                    )
                except (ValueError, TypeError, AttributeError, OverflowError):
                    pass
            if exc.code in (401, 403):
                self.error = f"Claude HTTP {exc.code}: mở Claude Code để cập nhật đăng nhập."
            else:
                self.error = f"Claude HTTP {exc.code}; thử lại sau {int(delay)} giây."
            self.next_poll = now + delay
        except (OSError, ValueError, TypeError, AttributeError):
            self.failures += 1
            delay = min(300, 5 * 2 ** min(self.failures, 6))
            self.next_poll = now + delay
            self.error = f"Claude: chưa cập nhật được quota; thử lại sau {delay} giây."
        return self.snapshot, self.error
