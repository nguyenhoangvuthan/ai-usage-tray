"""Cấu hình hiển thị khay, lưu theo user và xem trước màu trước khi áp dụng."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path

DEFAULTS = {
    "show_percent": True,
    "show_label": True,
    "percentage_mode": "used",
    "period": "primary",
    "colors": {"claude": "#df9a78", "codex": "#65d5ba"},
    "background_color": "#182231",
    "alarm_enabled": True,
    "alarm_sound": True,
    "alarm_on_schedule": True,
}


def config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "ai-usage-tray/config.json"


def validate(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Config phải là một JSON object.")
    result = copy.deepcopy(DEFAULTS)
    for key in ("show_percent", "show_label", "alarm_enabled", "alarm_sound", "alarm_on_schedule"):
        if key in data:
            if not isinstance(data[key], bool):
                raise ValueError(f"{key} phải là true hoặc false.")
            result[key] = data[key]
    for key, options in [
        ("percentage_mode", ("used", "remaining")),
        ("period", ("primary", "secondary")),
    ]:
        if key in data:
            if data[key] not in options:
                raise ValueError(f"{key} phải thuộc {options}.")
            result[key] = data[key]
    colors = data.get("colors", {})
    if not isinstance(colors, dict):
        raise ValueError("colors phải là một JSON object.")
    for provider in ("claude", "codex"):
        result["colors"][provider] = colors.get(provider, result["colors"][provider])
    result["background_color"] = data.get("background_color", result["background_color"])
    for color in [*result["colors"].values(), result["background_color"]]:
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Màu phải có dạng #RRGGBB, ví dụ #65d5ba.")
    return result


def load_config() -> tuple[dict, str | None]:
    try:
        return validate(json.loads(config_path().read_text())), None
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS), None
    except (OSError, ValueError) as exc:
        return copy.deepcopy(
            DEFAULTS
        ), f"Không đọc được cấu hình; đang dùng mặc định ({type(exc).__name__})."


def save_config(data: dict) -> None:
    data = validate(data)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def tray_value(limits: dict, settings: dict, now: float) -> float | None:
    window = limits.get(settings["period"])
    if not window:
        return None
    used = window["used_percent"]
    return max(0, 100 - used) if settings["percentage_mode"] == "remaining" else used


def tray_icon(provider: str, settings: dict, percent: float | None = None):
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(settings["background_color"]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(1, 1, 62, 62), 12, 12)
    painter.setPen(QColor(settings["colors"][provider]))

    def draw(text, rect, size):
        font = QFont("Noto Sans", size, QFont.Weight.Bold)
        painter.setFont(font)
        while painter.fontMetrics().horizontalAdvance(text) > 60 and size > 8:
            size -= 1
            font.setPointSize(size)
            painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    label = "CL" if provider == "claude" else "CX"
    if not settings["show_percent"]:
        draw(label, QRectF(0, 0, 64, 64), 26)
    else:
        text = "—" if percent is None else f"{percent:.0f}%"
        if settings["show_label"]:
            draw(label, QRectF(0, 0, 64, 27), 16)
            draw(text, QRectF(0, 26, 64, 35), 20)
        else:
            draw(text, QRectF(0, 0, 64, 64), 28)
    painter.end()
    return QIcon(pix)


def edit_preferences(parent, current: dict, apply, error: str | None = None):
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import (
        QCheckBox,
        QColorDialog,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QHBoxLayout,
        QLabel,
        QMessageBox,
        QPushButton,
        QVBoxLayout,
    )

    dialog = QDialog(parent)
    dialog.setWindowTitle("Cấu hình · AI Usage Tray")
    dialog.setMinimumWidth(510)
    layout = QVBoxLayout(dialog)
    layout.setSpacing(14)
    form = QFormLayout()
    show_percent = QCheckBox("Hiện % trên biểu tượng ở taskbar / khay hệ thống")
    show_percent.setObjectName("show_percent")
    show_percent.setChecked(current["show_percent"])
    show_label = QCheckBox("Hiện CL / CX phía trên % (tắt để số lớn hơn)")
    show_label.setObjectName("show_label")
    show_label.setChecked(current["show_label"])
    layout.addWidget(show_percent)
    layout.addWidget(show_label)
    mode = QComboBox()
    mode.setObjectName("percentage_mode")
    mode.addItem("Phần trăm đã dùng", "used")
    mode.addItem("Phần trăm còn lại", "remaining")
    mode.setCurrentIndex(mode.findData(current["percentage_mode"]))
    period = QComboBox()
    period.setObjectName("period")
    period.addItem("Hạn mức ngắn hạn (thường 5 giờ)", "primary")
    period.addItem("Hạn mức dài hạn (thường 7 ngày)", "secondary")
    period.setCurrentIndex(period.findData(current["period"]))
    form.addRow("Số hiển thị", mode)
    form.addRow("Cửa sổ hạn mức", period)
    draft_colors = {**current["colors"], "background": current["background_color"]}
    color_buttons = {}
    for key, name in [
        ("claude", "Màu Claude"),
        ("codex", "Màu Codex"),
        ("background", "Màu nền biểu tượng"),
    ]:
        button = QPushButton(draft_colors[key])
        button.setObjectName("color_" + key)
        color_buttons[key] = button

        def choose(checked=False, key=key):
            color = QColorDialog.getColor(QColor(draft_colors[key]), dialog, "Chọn màu")
            if color.isValid():
                draft_colors[key] = color.name()
                preview()

        button.clicked.connect(choose)
        form.addRow(name, button)
    layout.addLayout(form)
    alarm_enabled = QCheckBox("Alarm khi hạn mức 5 giờ được xác nhận về 0%")
    alarm_enabled.setChecked(current["alarm_enabled"])
    alarm_enabled.setObjectName("alarm_enabled")
    alarm_sound = QCheckBox("Phát âm thanh khi báo reset")
    alarm_sound.setChecked(current["alarm_sound"])
    alarm_on_schedule = QCheckBox("Nhắc thêm lúc đến giờ reset nếu chưa có xác nhận")
    alarm_on_schedule.setChecked(current["alarm_on_schedule"])
    for checkbox in (alarm_enabled, alarm_sound, alarm_on_schedule):
        layout.addWidget(checkbox)
    preview_row = QHBoxLayout()
    preview_row.addWidget(QLabel("Xem trước (số minh họa):"))
    samples = {}
    for provider in ("claude", "codex"):
        samples[provider] = QLabel()
        samples[provider].setObjectName("preview_" + provider)
        preview_row.addWidget(samples[provider])
    preview_row.addStretch()
    layout.addLayout(preview_row)
    note = QLabel("Màu áp dụng cho chữ/số trên khay và thanh usage. Khi thiếu dữ liệu sẽ hiện —.")
    note.setWordWrap(True)
    layout.addWidget(note)
    path_label = QLabel(str(config_path()))
    path_label.setObjectName("muted")
    path_label.setWordWrap(True)
    layout.addWidget(path_label)
    if error:
        warning = QLabel(error)
        warning.setWordWrap(True)
        layout.addWidget(warning)
    buttons = QDialogButtonBox()
    defaults = buttons.addButton("Mặc định", QDialogButtonBox.ButtonRole.ResetRole)
    buttons.addButton("Hủy", QDialogButtonBox.ButtonRole.RejectRole)
    save = buttons.addButton("Lưu và áp dụng", QDialogButtonBox.ButtonRole.AcceptRole)
    save.setObjectName("save_preferences")
    layout.addWidget(buttons)

    def values():
        return {
            "show_percent": show_percent.isChecked(),
            "show_label": show_label.isChecked(),
            "percentage_mode": mode.currentData(),
            "period": period.currentData(),
            "colors": {k: draft_colors[k] for k in ("claude", "codex")},
            "background_color": draft_colors["background"],
            "alarm_enabled": alarm_enabled.isChecked(),
            "alarm_sound": alarm_sound.isChecked(),
            "alarm_on_schedule": alarm_on_schedule.isChecked(),
        }

    def preview():
        settings = values()
        show_label.setEnabled(settings["show_percent"])
        mode.setEnabled(settings["show_percent"])
        period.setEnabled(settings["show_percent"])
        alarm_sound.setEnabled(settings["alarm_enabled"])
        alarm_on_schedule.setEnabled(settings["alarm_enabled"])
        for key, button in color_buttons.items():
            color = QColor(draft_colors[key])
            fg = "#101521" if color.lightness() > 145 else "#ffffff"
            button.setText(draft_colors[key])
            button.setStyleSheet(f"background: {color.name()}; color: {fg};")
        for provider, used in [("claude", 42), ("codex", 68)]:
            pct = 100 - used if settings["percentage_mode"] == "remaining" else used
            samples[provider].setPixmap(tray_icon(provider, settings, pct).pixmap(48, 48))

    def reset():
        draft_colors.update(DEFAULTS["colors"], background=DEFAULTS["background_color"])
        show_percent.setChecked(DEFAULTS["show_percent"])
        show_label.setChecked(DEFAULTS["show_label"])
        mode.setCurrentIndex(mode.findData(DEFAULTS["percentage_mode"]))
        period.setCurrentIndex(period.findData(DEFAULTS["period"]))
        alarm_enabled.setChecked(DEFAULTS["alarm_enabled"])
        alarm_sound.setChecked(DEFAULTS["alarm_sound"])
        alarm_on_schedule.setChecked(DEFAULTS["alarm_on_schedule"])
        preview()

    def accept():
        settings = values()
        try:
            save_config(settings)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(dialog, "Không lưu được", str(exc))
            return
        apply(settings)
        dialog.accept()

    for widget in (show_percent, show_label, alarm_enabled):
        widget.toggled.connect(preview)
    for widget in (mode, period):
        widget.currentIndexChanged.connect(preview)
    defaults.clicked.connect(reset)
    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)
    preview()
    dialog.exec()
