# -*- coding: utf-8 -*-
"""配色与全局样式表。"""
from __future__ import annotations

from ..paths import ASSETS

PALETTES = {
    "dark": {
        "bg": "#14161c",
        "panel": "#1c1f27",
        "panel2": "#232734",
        "border": "#2c3140",
        "text": "#eef1f7",
        "muted": "#9aa3b5",
        "faint": "#6b7280",
        "titlebar": "#191c24",
        "scroll": "#39404f",
        "hover": "#2a2f3d",
    },
    "light": {
        "bg": "#f7f8fa",
        "panel": "#ffffff",
        "panel2": "#f6f7fa",
        "border": "#e5e7ed",
        "text": "#202637",
        "muted": "#657087",
        "faint": "#8b93a7",
        "titlebar": "#ffffff",
        "scroll": "#c3cadd",
        "hover": "#edf0f6",
    },
}

ACCENT_PRESETS = [
    ("樱花粉", "#ff86b3"),
    ("初音绿", "#3ddc97"),
    ("天依蓝", "#5aa9ff"),
    ("紫罗兰", "#a78bfa"),
    ("晚霞橙", "#ff9f5a"),
    ("赤红", "#ff6b6b"),
    ("青碧", "#2ec4b6"),
]

FONT_PRESETS = [
    "系统默认",
    "Microsoft YaHei UI",
    "Yu Gothic UI",
    "Noto Sans JP",
    "Source Han Sans SC",
    "HarmonyOS Sans SC",
    "SimHei",
    "SimSun",
]


def _clamp(v: float) -> int:
    return max(0, min(255, int(v)))


def shade(color: str, factor: float) -> str:
    """factor>1 变亮，factor<1 变暗。"""
    c = color.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    if factor >= 1:
        r = _clamp(r + (255 - r) * (factor - 1))
        g = _clamp(g + (255 - g) * (factor - 1))
        b = _clamp(b + (255 - b) * (factor - 1))
    else:
        r, g, b = _clamp(r * factor), _clamp(g * factor), _clamp(b * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def with_alpha(color: str, alpha: float) -> str:
    c = color.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:.2f})"


QSS = """
* { outline: none; }
QDialog { background: @bg; }
QLabel { background: transparent; border: none; }
QFrame#readingCard { background: @reading; border: 1px solid @border; border-radius: 16px; }
QFrame#controlCard { background: @panel; border: 1px solid @border; border-radius: 14px; }
QLabel#pageTitle { font-size: 23px; font-weight: 700; }
QLabel#captionSource { color: @muted; }
QLabel#captionTranslation { color: @text; font-weight: 600; }
QListWidget#settingsNav { background: transparent; border: none; font-size: 13px; outline: none; }
QListWidget#settingsNav::item { padding: 11px 9px; margin-bottom: 4px; border-radius: 8px; }
QListWidget#settingsNav::item:selected { background: @accentSoft; color: @accentText; font-weight: 600; }
QListWidget#settingsNav::item:hover { background: @hover; }
QTabBar::tab { padding: 8px 12px; border-radius: 6px; }
QTabBar::tab:selected { background: @accentSoft; color: @accentText; }
QTextBrowser { border: none; background: transparent; }
QProgressBar { background: @border; border: none; border-radius: 3px; }
QProgressBar::chunk { background: @accent; border-radius: 3px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QWidget { color: @text; font-family: "@font"; }
QToolTip {
    background: @panel2; color: @text; border: 1px solid @border;
    border-radius: 6px; padding: 6px 8px;
}
QFrame#card, QFrame#drawerCard {
    background: @panel; border: 1px solid @border; border-radius: 12px;
}
QFrame#titlebar { background: transparent; }
QLabel#muted { color: @muted; }
QLabel#faint { color: @faint; }
QLabel#sectionTitle { color: @accentText; font-weight: 600; font-size: 13px; }
QPushButton {
    background: @panel2; color: @text; border: 1px solid @border;
    border-radius: 9px; padding: 7px 14px;
}
QPushButton:hover { background: @hover; }
QPushButton:pressed { background: @panel2; }
QPushButton:disabled { color: @faint; }
QPushButton#accent {
    background: @accent; color: @onAccent; border: none; font-weight: 600;
    padding: 9px 22px; border-radius: 10px;
}
QPushButton#accent:hover { background: @accentHover; }
QPushButton#accent:disabled { background: @border; color: @faint; }
QPushButton#ghost {
    background: transparent; border: 1px solid @border; color: @muted;
    border-radius: 9px; padding: 5px 10px;
}
QPushButton#ghost:hover { color: @text; background: @hover; }
QPushButton#winBtn {
    background: transparent; border: none; border-radius: 8px;
    padding: 6px 10px; color: @muted;
}
QPushButton#winBtn:hover { background: @hover; color: @text; }
QPushButton#closeBtn {
    background: transparent; border: none; border-radius: 8px;
    padding: 6px 10px; color: @muted;
}
QPushButton#closeBtn:hover { background: #e5484d; color: white; }
QPushButton:checked {
    background: @accentSoft; border: 1px solid @accent; color: @text;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
    background: @panel2; border: 1px solid @border; border-radius: 8px;
    padding: 6px 9px; selection-background-color: @accent;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid @accent; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: @panel; border: 1px solid @border; border-radius: 8px;
    selection-background-color: @accentSoft; selection-color: @text; padding: 4px;
}
QSlider::groove:horizontal { height: 5px; background: @border; border-radius: 3px; }
QSlider::sub-page:horizontal { background: @accent; border-radius: 3px; }
QSlider::handle:horizontal {
    background: white; border: 2px solid @accent; width: 14px; height: 14px;
    margin: -6px 0; border-radius: 9px;
}
QSlider::handle:horizontal:hover { background: @accentSoft; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 17px; height: 17px; border-radius: 5px;
    border: 1px solid @border; background: @panel2;
}
QCheckBox::indicator:hover { border: 1px solid @accent; }
QCheckBox::indicator:checked {
    background: @accent; border: 1px solid @accent;
    image: url("@assets/check.png");
}
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: @scroll; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: @accent; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar:horizontal { background: transparent; height: 9px; margin: 2px; }
QScrollBar::handle:horizontal { background: @scroll; border-radius: 4px; min-width: 30px; }
QMenu { background: @panel; border: 1px solid @border; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 7px 22px; border-radius: 7px; }
QMenu::item:selected { background: @accentSoft; }
QTableWidget {
    background: @panel2; border: 1px solid @border; border-radius: 8px;
    gridline-color: @border;
}
QHeaderView::section { background: @panel; color: @muted; border: none; padding: 5px; }
QGroupBox { border: 1px solid @border; border-radius: 10px; margin-top: 12px; padding-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: @muted; }
"""


def build_qss(theme: str, accent: str, font_family: str, photo: bool = False) -> str:
    p = PALETTES.get(theme, PALETTES["dark"])
    return (
        QSS.replace("@reading", "rgba(255,255,255,0.89)" if theme == "light" and photo else p["panel"])
        .replace("@font", font_family or "Microsoft YaHei UI")
        .replace("@assets", ASSETS.as_posix())
        .replace("@accentHover", shade(accent, 1.18))
        .replace("@accentSoft", with_alpha(accent, 0.10 if theme == "light" else 0.22))
        .replace("@accentText", accent)
        .replace("@accent", accent)
        .replace("@onAccent", "#ffffff")
        .replace("@bg", p["bg"])
        .replace("@panel2", p["panel2"])
        .replace("@panel", p["panel"])
        .replace("@border", p["border"])
        .replace("@hover", p["hover"])
        .replace("@scroll", p["scroll"])
        .replace("@muted", p["muted"])
        .replace("@faint", p["faint"])
        .replace("@titlebar", p["titlebar"])
        .replace("@text", p["text"])
    )
