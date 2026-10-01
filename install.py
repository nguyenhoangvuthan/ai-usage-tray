"""Cài launcher, autostart hoặc systemd user service và Claude statusLine bridge."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from usage import state_dir

ROOT = Path(__file__).resolve().parent
SERVICE = "ai-usage-tray.service"
SESSION_ENV = (
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_CURRENT_DESKTOP", "QT_QPA_PLATFORM",
)


def systemctl(*args):
    subprocess.run(["systemctl", "--user", *args], check=True)


def unit_quote(value: str, *, exec_arg=False) -> str:
    """Quote systemd syntax; ExecStart additionally expands dollar signs."""
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    value = value.replace("%", "%%")
    if exec_arg:
        value = value.replace("$", "$$")
    return f'"{value}"'


def service_unit(launcher: Path) -> str:
    # Only persist paths needed by the collector, never the whole environment.
    environment = {"PYTHONUNBUFFERED": "1", "PATH": os.defpath}
    for name in (
        "PATH", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_STATE_HOME",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    ):
        if name in os.environ:
            environment[name] = os.environ[name]
    lines = "".join(
        f"Environment={unit_quote(f'{name}={value}')}\n"
        for name, value in environment.items()
    )
    return (
        "[Unit]\nDescription=AI Usage Tray (Claude and Codex)\n"
        "PartOf=graphical-session.target\nAfter=graphical-session-pre.target\n"
        "StartLimitIntervalSec=0\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={unit_quote(str(launcher), exec_arg=True)}\n"
        f"{lines}"
        "Restart=on-failure\nRestartSec=10\nTimeoutStopSec=30\n"
        "UMask=0077\nStandardOutput=journal\nStandardError=journal\n\n"
        "[Install]\nWantedBy=graphical-session.target\n"
    )


def atomic(path: Path, text: str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument(
        "--service", action="store_true", help="Chạy nền bằng systemd user service"
    )
    args = parser.parse_args()
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    settings = claude / "settings.json"
    launcher = Path.home() / ".local/bin/ai-usage-tray"
    bridge = Path.home() / ".local/bin/ai-usage-claude-bridge"
    autostart = config / "autostart/ai-usage-tray.desktop"
    desktop = data / "applications/ai-usage-tray.desktop"
    unit = config / "systemd/user" / SERVICE
    use_service = args.service or unit.exists()
    values = json.loads(settings.read_text()) if settings.exists() else {}
    command = shlex.quote(str(bridge))
    if use_service:
        if not shutil.which("systemctl"):
            raise SystemExit("Cần systemd và systemctl để cài/gỡ user service.")
        # Check the user manager without printing its potentially sensitive environment.
        subprocess.run(
            ["systemctl", "--user", "show-environment"], stdout=subprocess.DEVNULL, check=True
        )
    if args.uninstall:
        if unit.exists():
            systemctl("disable", "--now", SERVICE)
            unit.unlink()
            systemctl("daemon-reload")
        for path in (launcher, bridge, autostart, desktop):
            path.unlink(missing_ok=True)
        if values.get("statusLine", {}).get("command") == command:
            del values["statusLine"]
            atomic(settings, json.dumps(values, ensure_ascii=False, indent=2) + "\n")
        print("Đã gỡ service/launcher/autostart/bridge. Giữ nguyên dữ liệu và cấu hình ứng dụng.")
        return
    try:
        import PyQt6.QtWidgets  # noqa: F401
    except ImportError:
        raise SystemExit(
            "Thiếu PyQt6. Chạy uv sync rồi uv run python install.py --start."
        ) from None
    for path, script in [(launcher, "main.py"), (bridge, "claude_bridge.py")]:
        content = "#!/bin/sh\nexec " + shlex.join([sys.executable, str(ROOT / script)]) + ' "$@"\n'
        atomic(path, content, 0o755)
    # Desktop Entry yêu cầu escape riêng, không dùng shell quoting cho Exec.
    exec_path = (
        str(launcher)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("%", "%%")
    )
    entry = (
        "[Desktop Entry]\nType=Application\nName=AI Usage Tray\n"
        "Comment=Claude Code và Codex usage · cập nhật mỗi 5 giây\n"
        f'Exec="{exec_path}"\nIcon=utilities-system-monitor\n'
        "Terminal=false\nCategories=Utility;Monitor;\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    if use_service:
        atomic(unit, service_unit(launcher), 0o600)
        systemctl("daemon-reload")
        systemctl("enable", SERVICE)
        autostart.unlink(missing_ok=True)
        # Desktop launch must go through the manager to preserve service ownership.
        entry = entry.replace(f'Exec="{exec_path}"', f"Exec=systemctl --user start {SERVICE}")
    else:
        atomic(autostart, entry, 0o644)
    atomic(desktop, entry, 0o644)
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if "statusLine" not in values:
        if settings.exists():
            backup = state / "claude-settings.before-tray.json"
            if not backup.exists():
                atomic(backup, settings.read_text())
        values["statusLine"] = {"type": "command", "command": command, "refreshInterval": 5}
        atomic(settings, json.dumps(values, ensure_ascii=False, indent=2) + "\n")
        print("Đã cấu hình Claude statusLine bridge; bản sao settings cũ trong thư mục state.")
    elif values.get("statusLine", {}).get("command") != command:
        print("Giữ nguyên statusLine hiện có. Xem README để nối bridge và nhận hạn mức Claude.")
    print(f"Đã cài {launcher}; tự chạy khi đăng nhập desktop.")
    if use_service:
        print(f"Service: {unit}\nLog: journalctl --user -u {SERVICE} -f")
        if args.start:
            session_vars = [name for name in SESSION_ENV if name in os.environ]
            if session_vars:
                systemctl("import-environment", *session_vars)
            systemctl("restart", SERVICE)
            print(f"Đã yêu cầu khởi chạy. Kiểm tra: systemctl --user status {SERVICE}")
    elif args.start:
        log_path = state / "app.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as log:
            process = subprocess.Popen(
                [str(launcher)], stdout=log, stderr=log, start_new_session=True
            )
        print(f"Đã khởi chạy PID {process.pid}. Log: {log_path}")


if __name__ == "__main__":
    main()
