"""ui.desktop.theme —— LivingMemory 风格深色主题（统一 QSS，页面不写内联样式）。"""
from __future__ import annotations

__all__ = ["COLORS", "stylesheet"]

COLORS = {
    "bg": "#05070C",
    "sidebar": "#070A10",
    "panel": "#0A0F18",
    "panel_alt": "#0C121C",
    "border": "#18202E",
    "border_strong": "#243349",
    "text": "#C9D6E8",
    "muted": "#6B7C93",
    "accent": "#4FC3F7",
    "accent_dim": "#2E6E8E",
    "gold": "#FFCB6B",
    "danger": "#FF5370",
    "ok": "#7EE787",
}


def stylesheet() -> str:
    c = COLORS
    return f"""
    QWidget {{
        background-color: {c['bg']};
        color: {c['text']};
        font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
        font-size: 12px;
    }}
    QFrame#sidebar {{ background-color: {c['sidebar']}; border-right: 1px solid {c['border']}; }}
    QFrame#topbar {{ background-color: {c['sidebar']}; border-bottom: 1px solid {c['border']}; }}
    QFrame#bottombar {{ background-color: {c['sidebar']}; border-top: 1px solid {c['border']}; }}
    QFrame#card {{ background-color: {c['panel']}; border: 1px solid {c['border']}; border-radius: 10px; }}
    QFrame#divider {{ background-color: {c['border']}; max-height: 1px; border: none; }}

    QLabel#brand {{ font-size: 15px; font-weight: 600; color: #EAF3FF; }}
    QLabel#brandSub {{ color: {c['muted']}; font-size: 10px; }}
    QLabel#navSection {{ color: {c['muted']}; font-size: 10px; padding: 10px 0 4px 18px; }}
    QLabel#cardTitle {{ color: {c['muted']}; font-size: 11px; letter-spacing: 1px; }}
    QLabel#kpiValue {{ font-size: 22px; font-weight: 600; color: #EAF3FF; }}
    QLabel#kpiCaption {{ color: {c['muted']}; font-size: 10px; }}
    QLabel#hint {{ color: {c['muted']}; }}
    QLabel#ok {{ color: {c['ok']}; }}
    QLabel#danger {{ color: {c['danger']}; }}
    QLabel#accent {{ color: {c['accent']}; }}

    QListWidget#nav {{ background: transparent; border: none; outline: none; }}
    QListWidget#nav::item {{
        height: 34px; margin: 2px 8px; padding-left: 10px;
        border-radius: 6px; color: {c['text']};
    }}
    QListWidget#nav::item:hover {{ background-color: #0E1622; }}
    QListWidget#nav::item:selected {{
        background-color: #101E2C; color: {c['accent']};
        border-left: 2px solid {c['accent']};
    }}

    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
        background-color: {c['panel_alt']}; border: 1px solid {c['border']};
        border-radius: 6px; padding: 5px 8px; color: {c['text']};
        selection-background-color: {c['accent_dim']};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {{ border-color: {c['accent_dim']}; }}
    QLineEdit#search {{ background-color: #0B1320; border-radius: 14px; padding: 6px 14px; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background-color: {c['panel']}; border: 1px solid {c['border_strong']};
        selection-background-color: #16243A; color: {c['text']};
    }}

    QPushButton {{
        background-color: #101A28; border: 1px solid {c['border_strong']};
        border-radius: 6px; padding: 6px 14px; color: {c['text']};
    }}
    QPushButton:hover {{ background-color: #16243A; border-color: {c['accent_dim']}; }}
    QPushButton:pressed {{ background-color: #1B2B44; }}
    QPushButton:disabled {{ color: {c['muted']}; border-color: {c['border']}; }}
    QPushButton#primary {{ background-color: #0F3040; border-color: {c['accent_dim']}; color: {c['accent']}; }}
    QPushButton#danger {{ background-color: #2A1218; border-color: #5A2230; color: {c['danger']}; }}

    QTableWidget, QTableWidget QTableCornerButton::section {{
        background-color: {c['panel_alt']}; alternate-background-color: #0A111B;
        gridline-color: {c['border']}; border: 1px solid {c['border']};
        selection-background-color: #16243A; selection-color: #EAF3FF;
    }}
    QHeaderView::section {{
        background-color: #0D1522; color: {c['muted']}; border: none;
        border-bottom: 1px solid {c['border']}; padding: 6px;
    }}

    QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 8px; top: -1px; }}
    QTabBar::tab {{
        background: transparent; color: {c['muted']}; padding: 6px 14px; margin-right: 4px;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{ color: {c['accent']}; border-bottom: 2px solid {c['accent']}; }}

    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #1D2A3D; border-radius: 4px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: #2A3D57; }}
    QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: #1D2A3D; border-radius: 4px; min-width: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}

    QStatusBar {{ background-color: {c['sidebar']}; color: {c['muted']}; border-top: 1px solid {c['border']}; }}
    QToolTip {{ background-color: #0B1220; color: {c['text']}; border: 1px solid {c['border_strong']}; }}
    QSplitter::handle {{ background-color: {c['border']}; }}
    """
