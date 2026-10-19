"""Đọc log token tăng dần và quota trực tiếp; không lưu nội dung hội thoại."""

from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_api import ClaudeUsageClient
from codex_api import CodexUsageClient


def state_dir() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "ai-usage-tray"
    )


def timestamp(value: Any) -> float | None:
    try:
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def number(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) and math.isfinite(value) else 0


def window(data: Any, percent_key: str) -> dict | None:
    if not isinstance(data, dict):
        return None
    pct = data.get(percent_key)
    if not isinstance(pct, (int, float)) or not math.isfinite(pct):
        return None
    return {"used_percent": max(0, float(pct)), "resets_at": timestamp(data.get("resets_at"))}


@dataclass
class FileState:
    inode: tuple[int, int]
    offset: int = 0
    session: str = ""
    previous: dict = field(default_factory=dict)


class Collector:
    def __init__(
        self,
        codex_home: Path | None = None,
        claude_home: Path | None = None,
        state: Path | None = None,
        online: bool = True,
    ):
        self.codex = codex_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self.claude = claude_home or Path(
            os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        )
        self.state = state or state_dir()
        self.claude_api = ClaudeUsageClient(self.claude, self.state)
        self.codex_api = CodexUsageClient(self.codex, self.state)
        self.online = online
        self.api_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quota")
        self.files: dict[Path, FileState] = {}
        self.records: dict[str, dict] = {}
        self.limits: dict[str, dict] = {}
        self.last_event: dict[str, float] = {}
        self.errors: list[str] = []

    def _event(self, provider: str, d: dict, f: FileState) -> None:
        ts = timestamp(d.get("timestamp"))
        payload = d.get("payload")
        if provider == "codex":
            if not isinstance(payload, dict):
                return
            if d.get("type") == "session_meta":
                f.session = str(payload.get("id") or f.session)
                return
            if d.get("type") != "event_msg" or payload.get("type") != "token_count" or ts is None:
                return
            rate = payload.get("rate_limits")
            # Các model có thể có bucket riêng; chỉ gộp bucket Codex chung.
            if isinstance(rate, dict) and rate.get("limit_id") in (None, "codex"):
                candidate = {
                    "observed_at": ts,
                    "primary": window(rate.get("primary"), "used_percent"),
                    "secondary": window(rate.get("secondary"), "used_percent"),
                    "primary_minutes": (rate.get("primary") or {}).get("window_minutes"),
                    "secondary_minutes": (rate.get("secondary") or {}).get("window_minutes"),
                    "source": "Log Codex · token_count",
                }
                if ts >= self.limits.get(provider, {}).get("observed_at", 0):
                    self.limits[provider] = candidate
            info = payload.get("info")
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if not isinstance(total, dict):
                return
            values = {
                k: number(total.get(k))
                for k in ("input_tokens", "cached_input_tokens", "output_tokens")
            }
            # Cumulative counter có thể reset khi phiên được compact/resume.
            reset = values["input_tokens"] < f.previous.get("input_tokens", 0)
            delta = {
                k: max(0, v - (0 if reset else f.previous.get(k, 0))) for k, v in values.items()
            }
            f.previous = values
            # Dòng lặp/copy cùng snapshot không được cộng lại.
            key = f"codex:{f.session}:{ts}:{values}"
            if key not in self.records:
                self.records[key] = {
                    "provider": provider,
                    "timestamp": ts,
                    "input": delta["input_tokens"],
                    "cache": delta["cached_input_tokens"],
                    "output": delta["output_tokens"],
                }
        else:
            msg = d.get("message")
            if d.get("type") != "assistant" or not isinstance(msg, dict) or ts is None:
                return
            usage = msg.get("usage")
            if not isinstance(usage, dict) or not msg.get("id"):
                return
            # Một message có nhiều block/stream update, chỉ giữ số lớn nhất.
            key = f"claude:{msg['id']}"
            record = {
                "provider": provider,
                "timestamp": ts,
                "input": sum(
                    number(usage.get(k))
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                ),
                "cache": number(usage.get("cache_read_input_tokens")),
                "output": number(usage.get("output_tokens")),
            }
            old = self.records.get(key)
            if old:
                record["timestamp"] = min(old["timestamp"], ts)
                for k in ("input", "cache", "output"):
                    record[k] = max(record[k], old[k])
            self.records[key] = record
        self.last_event[provider] = max(ts, self.last_event.get(provider, 0))

    def scan(self) -> dict:
        self.errors = []
        paths: list[tuple[str, Path]] = []
        for provider, root in [
            ("codex", self.codex / "sessions"),
            ("codex", self.codex / "archived_sessions"),
            ("claude", self.claude / "projects"),
        ]:
            try:
                paths.extend((provider, p) for p in sorted(root.rglob("*.jsonl")))
            except OSError:
                self.errors.append(f"Không đọc được thư mục log {provider}")
        # Rebuild khi file bị thay/thu ngắn/xóa để không giữ số đếm cũ.
        rebuild = bool(set(self.files) - {p for _, p in paths})
        for _, path in paths:
            try:
                st = path.stat()
                f = self.files.get(path)
                if f and (f.inode != (st.st_dev, st.st_ino) or st.st_size < f.offset):
                    rebuild = True
            except OSError:
                pass
        if rebuild:
            self.files.clear()
            self.records.clear()
            self.limits.clear()
            self.last_event.clear()
        for provider, path in paths:
            try:
                st = path.stat()
                f = self.files.setdefault(
                    path, FileState((st.st_dev, st.st_ino), session=str(path))
                )
                if st.st_size == f.offset:
                    continue
                with path.open("rb") as stream:
                    stream.seek(f.offset)
                    while line := stream.readline():
                        if not line.endswith(b"\n"):
                            break  # Chờ writer hoàn tất JSON ở lần quét sau.
                        f.offset = stream.tell()
                        try:
                            data = json.loads(line)
                            if isinstance(data, dict):
                                self._event(provider, data, f)
                        except (ValueError, TypeError, AttributeError, OverflowError):
                            self.errors.append(f"Bỏ qua một dòng log {provider} không hợp lệ")
            except OSError:
                self.errors.append(f"Không đọc được một file log {provider}")
        bridge = None
        try:
            bridge = json.loads((self.state / "claude-limits.json").read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.errors.append("Không đọc được snapshot hạn mức Claude")
        remote = codex_remote = None
        quota_errors = {"claude": None, "codex": None}
        if self.online:
            claude_future = self.api_pool.submit(self.claude_api.poll)
            codex_future = self.api_pool.submit(self.codex_api.poll)
            remote, error = claude_future.result()
            quota_errors["claude"] = error
            if error:
                self.errors.append(error)
            codex_remote, error = codex_future.result()
            quota_errors["codex"] = error
            if error:
                self.errors.append(error)
        if codex_remote is not None:
            self.limits["codex"] = codex_remote
        # API là nguồn trực tiếp; statusLine chỉ fallback nếu chưa từng có API snapshot.
        bridge = remote or bridge
        try:
            if not bridge:
                bridge = {}
            ts = timestamp(bridge.get("observed_at"))
            rate = bridge.get("rate_limits")
            if ts and isinstance(rate, dict):
                self.limits["claude"] = {
                    "observed_at": ts,
                    "primary": window(rate.get("five_hour"), "used_percentage"),
                    "secondary": window(rate.get("seven_day"), "used_percentage"),
                    "primary_minutes": 300,
                    "secondary_minutes": 10080,
                    "source": bridge.get("source", "Claude Code · statusLine (snapshot của phiên)"),
                }
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError):
            self.errors.append("Không đọc được snapshot hạn mức Claude")
        now = datetime.now().astimezone()
        result = {
            "scanned_at": now.timestamp(),
            "interval_seconds": 5,
            "errors": sorted(set(self.errors)),
            "files": len(self.files),
            "providers": {},
        }
        for provider in ("claude", "codex"):
            totals = {"input": 0, "cache": 0, "output": 0}
            for record in self.records.values():
                if (
                    record["provider"] == provider
                    and datetime.fromtimestamp(record["timestamp"]).date() == now.date()
                ):
                    for k in totals:
                        totals[k] += record[k]
            result["providers"][provider] = {
                "tokens_today": totals,
                "limits": self.limits.get(provider),
                "quota_error": quota_errors[provider],
                "last_event": self.last_event.get(provider),
            }
        return result

    def close(self):
        self.api_pool.shutdown(wait=True)
        self.codex_api.close()
