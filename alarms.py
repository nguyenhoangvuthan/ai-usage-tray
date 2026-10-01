"""Theo dõi reset 5 giờ; lưu dấu đã báo để không lặp sau mỗi lần quét/khởi động."""

import json
from pathlib import Path

from claude_api import atomic_json


class ResetAlarms:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.state = json.loads(path.read_text())
            if not isinstance(self.state, dict):
                self.state = {}
        except (OSError, ValueError):
            self.state = {}

    def evaluate(self, providers: dict, now: float, enabled=True, remind=True) -> list[dict]:
        before = json.dumps(self.state, sort_keys=True)
        events = []
        for provider, details in providers.items():
            row = self.state.setdefault(provider, {})
            limits = details.get("limits") or {}
            window = limits.get("primary")
            observed = limits.get("observed_at", 0)
            reset = row.get("reset_at")
            # Chỉ snapshot mới sau mốc reset mới xác nhận về 0%; không suy từ đồng hồ.
            if (
                window
                and limits.get("primary_minutes") == 300
                and observed > row.get("last_observed", 0)
            ):
                used = window["used_percent"]
                if (
                    reset
                    and row.get("had_usage")
                    and observed >= reset
                    and used == 0
                    and row.get("confirmed") != reset
                    and now - reset <= 18000
                ):
                    row["confirmed"] = reset
                    if enabled:
                        events.append(
                            {"provider": provider, "kind": "confirmed", "reset_at": reset}
                        )
                row["last_observed"] = observed
            if (
                reset
                and row.get("had_usage")
                and 0 <= now - reset <= 18000
                and row.get("due") != reset
                and row.get("confirmed") != reset
            ):
                row["due"] = reset
                if enabled and remind:
                    events.append({"provider": provider, "kind": "due", "reset_at": reset})
            if window and limits.get("primary_minutes") == 300:
                next_reset = window.get("resets_at")
                # Không arm lại từ snapshot cũ hoặc cửa sổ lịch sử khi cold start.
                if next_reset and next_reset > now and observed >= row.get("last_observed", 0):
                    # API có thể lệch vài phần giây giữa hai lần trả cùng một mốc.
                    if reset is None or abs(next_reset - reset) > 60:
                        row["reset_at"] = next_reset
                        row["had_usage"] = window["used_percent"] > 0
                    else:
                        row["had_usage"] = row.get("had_usage", False) or window["used_percent"] > 0
        if json.dumps(self.state, sort_keys=True) != before:
            atomic_json(self.path, self.state)
        return events
