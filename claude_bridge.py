"""Nhận statusLine chính thức; chỉ lưu rate_limits, không lưu prompt/token đăng nhập."""

import json
import os
import sys
import tempfile
import time

from usage import state_dir


def capture(data: dict) -> str:
    rate = data.get("rate_limits")
    if isinstance(rate, dict) and rate:
        root = state_dir()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=".claude-limits-", dir=root)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"observed_at": time.time(), "rate_limits": rate}, f)
            os.replace(name, root / "claude-limits.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)
    pieces = ["AI Usage Tray"]
    for key, label in [("five_hour", "5h"), ("seven_day", "7d")]:
        value = (rate or {}).get(key) or {}
        pct = value.get("used_percentage")
        if isinstance(pct, (int, float)):
            pieces.append(f"{label}: {pct:g}% đã dùng")
    return " · ".join(pieces)


if __name__ == "__main__":
    try:
        print(capture(json.load(sys.stdin)))
    except (ValueError, OSError, TypeError, AttributeError):
        print("AI Usage Tray · đang chờ dữ liệu")
