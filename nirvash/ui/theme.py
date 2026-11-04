"""Colours, stylesheet and line icons."""
from __future__ import annotations

import sys

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Dark base with more colour: deep indigo surfaces, teal + violet + pink accents.
C = {
    "bg": "#0e0f17",
    "rail": "#0a0b12",
    "surface": "#161826",
    "surface2": "#1d2032",
    "surface3": "#252942",
    "border": "#2b3050",
    "text": "#e8eaf6",
    "muted": "#8d93b5",
    "faint": "#5d6388",
    "teal": "#2fe0c0",
    "violet": "#9b86ff",
    "pink": "#ff6fb1",
    "amber": "#ffbf5a",
    "red": "#ff5f73",
    "blue": "#5aa8ff",
    "green": "#5be38a",
}

CATEGORY_COLOURS = ["#9b86ff", "#2fe0c0", "#ff6fb1", "#ffbf5a", "#5aa8ff", "#5be38a", "#ff8a5c", "#d07bff",
                    "#7fd4ff", "#f2e05a"]


def font_family() -> str:
    return "Segoe UI Variable Text, Segoe UI" if sys.platform == "win32" else "Inter, Noto Sans, DejaVu Sans"


def stylesheet() -> str:
    c = C
    return f"""
* {{ font-family: {font_family()}; font-size: 10pt; color: {c['text']}; outline: 0; }}
QMainWindow, QWidget#page, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {c['bg']}; }}
QWidget#rail {{ background: {c['rail']}; border-right: 1px solid {c['border']}; }}
QToolTip {{ background: {c['surface3']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 5px 8px; border-radius: 6px; }}
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: 17pt; font-weight: 600; }}
QLabel#h2 {{ font-size: 12pt; font-weight: 600; }}
QLabel#muted, QLabel[muted="true"] {{ color: {c['muted']}; }}
QLabel#faint {{ color: {c['faint']}; }}
QFrame#card {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 12px; }}
QFrame#card2 {{ background: {c['surface2']}; border: 1px solid {c['border']}; border-radius: 10px; }}
QFrame#banner {{ background: rgba(155,134,255,0.12); border: 1px solid rgba(155,134,255,0.45); border-radius: 10px; }}
QFrame#bannerWarn {{ background: rgba(255,191,90,0.10); border: 1px solid rgba(255,191,90,0.45); border-radius: 10px; }}
QFrame#hline {{ background: {c['border']}; max-height: 1px; min-height: 1px; border: none; }}

QPushButton {{ background: {c['surface2']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 6px 14px; }}
QPushButton:hover {{ background: {c['surface3']}; border-color: #3a4170; }}
QPushButton:pressed {{ background: #2e3352; }}
QPushButton:disabled {{ color: {c['faint']}; background: {c['surface']}; border-color: {c['surface2']}; }}
QPushButton:checked {{ background: rgba(155,134,255,0.22); border-color: {c['violet']}; }}
QPushButton#primary {{ border: none; color: #0b0c14; font-weight: 600;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {c['teal']}, stop:1 {c['violet']}); }}
QPushButton#primary:hover {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #5cf0d4, stop:1 #b3a3ff); }}
QPushButton#primary:disabled {{ background: {c['surface2']}; color: {c['faint']}; }}
QPushButton#danger {{ color: {c['red']}; }}
QPushButton#danger:hover {{ background: rgba(255,95,115,0.15); border-color: {c['red']}; }}
QPushButton#flat, QToolButton#flat {{ background: transparent; border: none; padding: 4px 6px; }}
QPushButton#flat:hover, QToolButton#flat:hover {{ background: {c['surface2']}; }}
QPushButton#chip {{ background: {c['surface2']}; border: 1px solid {c['border']}; border-radius: 13px; padding: 3px 11px; font-size: 9pt; }}
QPushButton#chip:hover {{ border-color: {c['violet']}; }}
QPushButton#chip:checked {{ background: rgba(47,224,192,0.18); border-color: {c['teal']}; }}
QToolButton {{ background: transparent; border: none; border-radius: 8px; padding: 4px; }}
QToolButton:hover {{ background: {c['surface2']}; }}
QToolButton::menu-indicator {{ image: none; }}

QToolButton#railBtn {{ border-radius: 10px; padding: 8px 0; color: {c['muted']}; font-size: 8pt; }}
QToolButton#railBtn:hover {{ background: {c['surface']}; color: {c['text']}; }}
QToolButton#railBtn:checked {{ background: {c['surface2']}; color: {c['text']}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextBrowser {{
    background: {c['surface2']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 5px 8px;
    selection-background-color: {c['violet']}; selection-color: #0b0c14; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {c['violet']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {c['surface2']}; border: 1px solid {c['border']}; selection-background-color: {c['surface3']}; padding: 4px; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {c['faint']}; background: {c['surface2']}; }}
QCheckBox::indicator:checked {{ background: {c['teal']}; border-color: {c['teal']}; image: url(:/check.svg); }}

QTableView, QListWidget, QTreeWidget, QListView {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px;
    alternate-background-color: #191b2b; gridline-color: transparent; selection-background-color: rgba(155,134,255,0.18);
    selection-color: {c['text']}; }}
QTableView::item, QListWidget::item, QTreeWidget::item {{ padding: 4px 6px; border: none; }}
QTableView::item:hover, QListWidget::item:hover, QTreeWidget::item:hover {{ background: rgba(255,255,255,0.03); }}
QTableView::item:selected, QListWidget::item:selected, QTreeWidget::item:selected {{ background: rgba(155,134,255,0.18); }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {c['surface']}; color: {c['muted']}; border: none; border-bottom: 1px solid {c['border']};
    padding: 6px 8px; font-size: 9pt; font-weight: 600; }}
QTableCornerButton::section {{ background: {c['surface']}; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #343a5e; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #454c78; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #343a5e; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}

QProgressBar {{ background: {c['surface2']}; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 4px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {c['teal']}, stop:1 {c['violet']}); }}
QSlider::groove:horizontal {{ height: 4px; background: {c['surface3']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ height: 4px; border-radius: 2px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {c['teal']}, stop:1 {c['violet']}); }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; background: {c['text']}; }}
QSplitter::handle {{ background: transparent; }}
QMenu {{ background: {c['surface2']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c['surface3']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 6px; }}
QMenu::indicator {{ width: 14px; height: 14px; margin-left: 6px; }}
QDialog {{ background: {c['bg']}; }}
QTabBar::tab {{ background: transparent; color: {c['muted']}; padding: 6px 12px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {c['teal']}; }}
"""


# --- icons -------------------------------------------------------------------
_P = {
    "library": '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    "scan": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="M8 11h1l1-2 2 4 1-2h1"/>',
    "wand": '<path d="m15 4 1 2 2 1-2 1-1 2-1-2-2-1 2-1z"/><path d="M3 21 14 10"/><path d="M19 13l.7 1.3L21 15l-1.3.7L19 17l-.7-1.3L17 15l1.3-.7z"/>',
    "cut": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/>',
    "gear": '<path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "play": '<path d="M7 4v16l13-8z" fill="currentColor"/>',
    "pause": '<rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"/><rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"/>',
    "star": '<path d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8-6.2-3.2-6.2 3.2L7 14.2 2 9.3l6.9-1z"/>',
    "star_on": '<path d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8-6.2-3.2-6.2 3.2L7 14.2 2 9.3l6.9-1z" fill="currentColor"/>',
    "trash": '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "log": '<path d="M4 6h16M4 12h16M4 18h10"/>',
    "tag": '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "export": '<path d="M12 3v12"/><path d="m7 8 5-5 5 5"/><path d="M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/>',
    "shuffle": '<path d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/>',
    "volume": '<path d="M11 5 6 9H2v6h4l5 4z" fill="currentColor"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/>',
    "open": '<path d="M15 3h6v6M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "film": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 7.5h4M3 12h18M3 16.5h4M17 7.5h4M17 16.5h4"/>',
    "sparkle": '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" fill="currentColor"/>',
    "download": '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 16v-5M12 8h.01"/>',
    "lyrics": '<path d="M4 5h16M4 10h10M4 15h12M4 20h7"/>',
    "skip": '<path d="M5 4v16l10-8z" fill="currentColor"/><path d="M19 5v14"/>',
}


def svg(name: str, color: str = C["text"], stroke: float = 1.8) -> str:
    body = _P.get(name, _P["info"]).replace("currentColor", color)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')


_cache: dict[tuple, QIcon] = {}


def pixmap(name: str, size: int = 18, color: str = C["text"], dpr: float = 2.0) -> QPixmap:
    r = QSvgRenderer(QByteArray(svg(name, color).encode()))
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p, QRectF(0, 0, size * dpr, size * dpr))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, color: str = C["text"], size: int = 18, checked_color: str | None = None) -> QIcon:
    key = (name, color, size, checked_color)
    if key in _cache:
        return _cache[key]
    ic = QIcon()
    ic.addPixmap(pixmap(name, size, color), QIcon.Normal, QIcon.Off)
    ic.addPixmap(pixmap(name, size, C["faint"]), QIcon.Disabled, QIcon.Off)
    if checked_color:
        ic.addPixmap(pixmap(name, size, checked_color), QIcon.Normal, QIcon.On)
    _cache[key] = ic
    return ic


def check_svg_resource() -> None:
    """Register the tick used inside checked checkboxes (QSS url(:/check.svg))."""
    import tempfile
    from pathlib import Path

    from PySide6.QtCore import QDir

    d = Path(tempfile.gettempdir()) / "nirvash-ui"
    d.mkdir(exist_ok=True)
    (d / "check.svg").write_text(svg("check", "#0b0c14", 3.0), "utf-8")
    QDir.addSearchPath("nv", str(d))


def fixed_stylesheet() -> str:
    return stylesheet().replace("url(:/check.svg)", "url(nv:check.svg)")


def apply_font(app) -> None:
    f = QFont()
    f.setFamilies([x.strip() for x in font_family().split(",")])
    f.setPointSizeF(10)
    f.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(f)


def qcolor(name: str, alpha: int = 255) -> QColor:
    c = QColor(C.get(name, name))
    c.setAlpha(alpha)
    return c


ICON_SIZE = QSize(18, 18)
