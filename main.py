"""Ứng dụng KDE/system tray, quét log trong worker để không chặn giao diện."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from alarms import ResetAlarms
from preferences import (
    config_path,
    edit_preferences,
    load_config,
    save_config,
    tray_icon,
    tray_value,
)
from usage import Collector, state_dir


def compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1000:
        return f"{value / 1000:.1f}K"
    return str(value)


def reset_text(reset: float | None, now: float) -> str:
    if reset is None:
        return "Chưa có thời điểm reset"
    if reset <= now:
        return "Đã đến giờ reset · chờ dữ liệu mới"
    seconds = int(reset - now)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    duration = f"{days} ngày {hours} giờ" if days else f"{hours} giờ {minutes:02d} phút"
    return f"Reset sau {duration} · {datetime.fromtimestamp(reset):%d/%m %H:%M}"


def run_gui(args) -> int:
    from PyQt6.QtCore import QLockFile, QProcess, Qt, QThread, QTimer, pyqtSignal
    from PyQt6.QtGui import QAction
    from PyQt6.QtWidgets import (
        QApplication,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMenu,
        QProgressBar,
        QPushButton,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )

    app = QApplication(sys.argv[:1])
    app.setApplicationName("AI Usage Tray")
    app.setDesktopFileName("ai-usage-tray")
    app.setQuitOnLastWindowClosed(False)
    settings, settings_error = load_config()
    if not config_path().exists():
        try:
            save_config(settings)
        except OSError:
            settings_error = "Chưa lưu được cấu hình mặc định; kiểm tra quyền thư mục config."
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    alarms = ResetAlarms(root / "reset-alarms.json")
    lock = QLockFile(str(root / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        print("AI Usage Tray đã chạy.", file=sys.stderr)
        return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print(
            "Desktop chưa có system tray/StatusNotifier. Hãy bật AppIndicator trên GNOME.",
            file=sys.stderr,
        )
        return 1

    class Worker(QThread):
        result = pyqtSignal(dict)
        failed = pyqtSignal(str)

        def __init__(self):
            super().__init__()
            self.collector = Collector()

        def run(self):
            try:
                self.result.emit(self.collector.scan())
            except Exception as exc:
                # Không đưa dữ liệu log hoặc nội dung exception lên giao diện.
                self.failed.emit(f"Quét thất bại ({type(exc).__name__}); sẽ thử lại sau 5 giây.")

    app.setStyleSheet("""
        QWidget { background: #101521; color: #ecf0f8; font-family: 'Noto Sans'; font-size: 13px; }
        QFrame#card { background: #1a2232; border: 1px solid #303b50; border-radius: 14px; }
        QFrame#card QLabel { background: transparent; border: none; }
        QLabel#title { font-size: 25px; font-weight: 700; }
        QLabel#muted { color: #a5b1c9; }
        QPushButton { background: #293650; border: 1px solid #435575; border-radius: 7px; padding: 9px 16px; }
        QPushButton:hover { background: #374a6b; }
        QMenu { background: #1a2232; padding: 7px; }
        QMenu::item { padding: 8px 20px; }
        QMenu::item:selected { background: #374a6b; }
    """)
    window_widget = QWidget()
    window_widget.setWindowTitle("AI Usage · Claude & Codex")
    window_widget.setMinimumSize(800, 710)
    layout = QVBoxLayout(window_widget)
    layout.setContentsMargins(24, 22, 24, 22)
    layout.setSpacing(16)
    title = QLabel("AI Usage")
    title.setObjectName("title")
    layout.addWidget(title)
    subtitle = QLabel("CLAUDE / CODEX     •     Tự cập nhật qua mạng, không cần mở terminal")
    subtitle.setObjectName("muted")
    layout.addWidget(subtitle)
    columns = QHBoxLayout()
    columns.setSpacing(16)
    layout.addLayout(columns)
    cards = {}
    names = {"claude": "Claude / Claude Code", "codex": "Codex · tài khoản ChatGPT"}
    colors = settings["colors"]
    for provider in names:
        card = QFrame()
        card.setObjectName("card")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(18, 18, 18, 18)
        inner.setSpacing(12)
        name = QLabel(names[provider])
        name.setStyleSheet(f"font-size: 17px; font-weight: 700; color: {colors[provider]}")
        inner.addWidget(name)
        widgets = {}
        for period, label in [("primary", "5 GIỜ"), ("secondary", "7 NGÀY")]:
            heading = QLabel(label)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFormat("Chưa có dữ liệu")
            bar.setFixedHeight(25)
            bar.setStyleSheet(f"""
                QProgressBar {{ background: #0e1521; border: none; border-radius: 5px; text-align: center; }}
                QProgressBar::chunk {{ background: {colors[provider]}; border-radius: 5px; }}
            """)
            reset = QLabel("Đang đọc nguồn…")
            reset.setObjectName("muted")
            reset.setWordWrap(True)
            inner.addWidget(heading)
            inner.addWidget(bar)
            inner.addWidget(reset)
            widgets[period] = (heading, bar, reset)
        tokens = QLabel("Token hôm nay: đang tính…")
        tokens.setWordWrap(True)
        tokens.setMinimumHeight(76)
        inner.addWidget(tokens)
        source = QLabel("Đang quét log lần đầu…")
        source.setObjectName("muted")
        source.setWordWrap(True)
        source.setMinimumHeight(84)
        inner.addWidget(source)
        inner.addStretch()
        widgets.update(tokens=tokens, source=source, name=name)
        cards[provider] = widgets
        columns.addWidget(card, 1)
    note = QLabel(
        "Token chỉ tính log trên máy này, input đã bao gồm cache. "
        "Quota trực tiếp: Claude mỗi 60 giây, Codex mỗi 5 giây (lùi lịch khi bị giới hạn). "
        "Hạn mức chat trên web ChatGPT không nằm trong log Codex."
    )
    note.setWordWrap(True)
    note.setObjectName("muted")
    layout.addWidget(note)
    status = QLabel("Đang quét…")
    status.setObjectName("muted")
    status.setWordWrap(True)
    layout.addWidget(status)
    buttons = QHBoxLayout()
    refresh = QPushButton("Quét ngay")
    preferences_button = QPushButton("Cấu hình")
    hide = QPushButton("Ẩn xuống khay")
    buttons.addWidget(refresh)
    buttons.addWidget(preferences_button)
    buttons.addStretch()
    buttons.addWidget(hide)
    layout.addLayout(buttons)
    hide.clicked.connect(window_widget.hide)

    def reveal():
        window_widget.show()
        window_widget.raise_()
        window_widget.activateWindow()

    def icon(provider, pct=None):
        return tray_icon(provider, settings, pct)

    trays = {}
    menus = []
    refresh_actions = []
    for provider in names:
        # Mỗi exporter DBus cần menu riêng để tránh xung đột ID và lỗi khi thoát.
        menu = QMenu(window_widget)
        open_action = QAction("Xem usage Claude & Codex", menu)
        open_action.triggered.connect(reveal)
        menu.addAction(open_action)
        refresh_action = QAction("Quét ngay", menu)
        menu.addAction(refresh_action)
        refresh_actions.append(refresh_action)
        settings_action = QAction("Cấu hình hiển thị…", menu)
        settings_action.triggered.connect(lambda: open_preferences())
        menu.addAction(settings_action)
        test_alarm = QAction("Thử thông báo reset", menu)
        test_alarm.triggered.connect(lambda: notify_reset("claude", "test"))
        menu.addAction(test_alarm)
        menu.addSeparator()
        quit_action = QAction("Thoát AI Usage Tray", menu)
        quit_action.triggered.connect(app.quit)
        menu.addAction(quit_action)
        menus.append(menu)
        tray = QSystemTrayIcon(icon(provider), app)
        tray.setToolTip(names[provider] + " · đang đọc dữ liệu")
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: (
                reveal()
                if reason
                in (
                    QSystemTrayIcon.ActivationReason.Trigger,
                    QSystemTrayIcon.ActivationReason.DoubleClick,
                )
                else None
            )
        )
        tray.show()
        trays[provider] = tray
    window_widget.setWindowIcon(icon("codex"))
    worker = Worker()
    last_data = None

    def notify_reset(provider, kind):
        title = f"{names[provider]} · reset 5 giờ"
        messages = {
            "confirmed": "Đã xác nhận hạn mức 5 giờ về 0% đã dùng. Bạn có thể tiếp tục sử dụng.",
            "due": "Đã đến giờ dự kiến reset 5 giờ. Đang chờ nguồn dữ liệu xác nhận về 0%.",
            "test": "Thông báo thử alarm reset. Đây không phải dữ liệu reset thật.",
        }
        trays[provider].showMessage(
            title, messages[kind], QSystemTrayIcon.MessageIcon.Information, 12000
        )
        if settings["alarm_sound"]:
            player = shutil.which("paplay") or shutil.which("pw-play")
            sound = next(
                (
                    p
                    for p in [
                        Path("/usr/share/sounds/freedesktop/stereo/complete.oga"),
                        Path("/usr/share/sounds/freedesktop/stereo/message.oga"),
                        Path("/usr/share/sounds/sound-icons/prompt.wav"),
                    ]
                    if p.exists()
                ),
                None,
            )
            if player and sound:
                process = QProcess(app)
                process.finished.connect(process.deleteLater)
                process.errorOccurred.connect(lambda: QApplication.beep())
                process.start(player, [str(sound)])
            else:
                QApplication.beep()

    def check_alarms(data):
        try:
            events = alarms.evaluate(
                data["providers"],
                time.time(),
                enabled=settings["alarm_enabled"],
                remind=settings["alarm_on_schedule"],
            )
        except OSError:
            status.setText("Không lưu được trạng thái alarm; kiểm tra quyền thư mục state.")
            return
        for event in events:
            notify_reset(event["provider"], event["kind"])

    def update(data):
        nonlocal last_data
        last_data = data
        now = time.time()
        for provider, details in data["providers"].items():
            c = cards[provider]
            limits = details["limits"] or {}
            tooltip = [
                names[provider],
                f"Quét lúc {datetime.fromtimestamp(data['scanned_at']):%H:%M:%S}",
            ]
            for period in ("primary", "secondary"):
                heading, bar, reset = c[period]
                minutes = limits.get(period + "_minutes")
                label = (
                    f"{minutes // 60:g} GIỜ"
                    if isinstance(minutes, (int, float)) and minutes
                    else ("5 GIỜ" if period == "primary" else "7 NGÀY")
                )
                if minutes == 10080:
                    label = "7 NGÀY"
                heading.setText(label)
                w = limits.get(period)
                expired = bool(w and w.get("resets_at") and w["resets_at"] <= now)
                pct = w["used_percent"] if w else None
                bar.setValue(min(100, round(pct)) if pct is not None else 0)
                bar.setFormat(
                    f"{pct:g}% đã dùng · còn {max(0, 100 - pct):g}%"
                    if pct is not None
                    else "Chưa có dữ liệu mới"
                )
                reset.setText(
                    reset_text(w.get("resets_at"), now) if w else "Chờ quota từ máy chủ"
                )
                tooltip.append(
                    f"{label}: {pct:g}% đã dùng"
                    + (" (snapshot cũ, chờ cập nhật)" if expired else "")
                    if pct is not None
                    else f"{label}: chưa có dữ liệu mới"
                )
            trays[provider].setIcon(icon(provider, tray_value(limits, settings, now)))
            if settings["show_percent"]:
                mode_label = "còn lại" if settings["percentage_mode"] == "remaining" else "đã dùng"
                period_label = "ngắn hạn" if settings["period"] == "primary" else "dài hạn"
                tooltip.append(f"Biểu tượng: % {mode_label} · hạn mức {period_label}")
            t = details["tokens_today"]
            c["tokens"].setText(
                f"TOKEN HÔM NAY   {compact(t['input'] + t['output'])}\n"
                f"Input {compact(t['input'])} · Output {compact(t['output'])}\n"
                f"Cache đọc {compact(t['cache'])} (nằm trong input)"
            )
            observed = limits.get("observed_at")
            if observed:
                age = max(0, int(now - observed))
                freshness = f"{age}s trước" if age < 60 else f"{age // 60} phút trước"
                stale = " · DỮ LIỆU CŨ" if age > 120 else ""
                c["source"].setText(
                    f"{limits['source']}\nNhận lúc {datetime.fromtimestamp(observed):%d/%m %H:%M:%S} · {freshness}{stale}"
                )
                tooltip.append(
                    f"Snapshot: {datetime.fromtimestamp(observed):%d/%m %H:%M:%S}{stale}"
                )
            else:
                c["source"].setText(
                    "Chưa đọc được quota. Kiểm tra đăng nhập Claude Code hoặc kết nối mạng."
                    if provider == "claude"
                    else "Chưa đọc được quota. Kiểm tra đăng nhập Codex CLI hoặc kết nối mạng."
                )
            trays[provider].setToolTip("\n".join(tooltip))
        status.setText(
            f"Quét lúc {datetime.fromtimestamp(data['scanned_at']):%H:%M:%S} · {data['files']} file log · chu kỳ 5 giây"
            + ("\n" + "; ".join(data["errors"]) if data["errors"] else "")
        )
        check_alarms(data)

    def apply_preferences(new_settings):
        nonlocal settings, settings_error
        settings = new_settings
        settings_error = None
        for provider, card in cards.items():
            color = settings["colors"][provider]
            card["name"].setStyleSheet(f"font-size: 17px; font-weight: 700; color: {color}")
            for period in ("primary", "secondary"):
                card[period][1].setStyleSheet(f"""
                    QProgressBar {{ background: #0e1521; border: none; border-radius: 5px; text-align: center; }}
                    QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}
                """)
            trays[provider].setIcon(icon(provider))
        window_widget.setWindowIcon(icon("codex"))
        if last_data is not None:
            update(last_data)

    def open_preferences():
        edit_preferences(window_widget, settings, apply_preferences, settings_error)

    preferences_button.clicked.connect(open_preferences)

    def scan_failed(message):
        status.setText(message)
        if last_data is not None:
            check_alarms(last_data)

    worker.result.connect(update)
    worker.failed.connect(scan_failed)

    def scan():
        if not worker.isRunning():
            worker.start()

    refresh.clicked.connect(scan)
    for action in refresh_actions:
        action.triggered.connect(scan)
    timer = QTimer(app)
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(scan)
    timer.start(5000)
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    scan()
    if args.show or args.screenshot:
        reveal()
    if args.settings:
        QTimer.singleShot(0, open_preferences)
    if args.screenshot:
        QTimer.singleShot(3500, lambda: window_widget.grab().save(args.screenshot))
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), app.quit)
    code = app.exec()
    timer.stop()
    worker.wait()
    worker.collector.close()
    for tray in trays.values():
        tray.hide()
        tray.setContextMenu(None)
    lock.unlock()
    return code


def main():
    parser = argparse.ArgumentParser(description="Theo dõi usage Claude Code / Codex mỗi 5 giây")
    parser.add_argument("--once", action="store_true", help="Xuất JSON usage một lần, không cần Qt")
    parser.add_argument("--show", action="store_true", help="Mở bảng chi tiết ngay")
    parser.add_argument(
        "--settings", action="store_true", help="Mở cấu hình hiển thị khi khởi động"
    )
    parser.add_argument("--screenshot", type=str, help="Lưu ảnh cửa sổ để kiểm tra giao diện")
    parser.add_argument("--quit-after", type=float, help="Tự thoát sau N giây, dùng khi kiểm tra")
    args = parser.parse_args()
    if args.once:
        collector = Collector()
        try:
            print(json.dumps(collector.scan(), ensure_ascii=False, indent=2))
        finally:
            collector.close()
        return 0
    return run_gui(args)


if __name__ == "__main__":
    raise SystemExit(main())
