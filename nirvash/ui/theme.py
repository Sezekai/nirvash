"""The look of the app: colours and sizes from the current theme (core/appearance), the stylesheet built from them,
the starfield scene, fonts and line icons.

Colours are read from C at the moment something is drawn or styled, and C is updated in place when the theme
changes, so painted widgets (delegates, chips, the rail) follow live changes after a repaint."""
from __future__ import annotations

import math
import random

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap, QRadialGradient
from PySide6.QtSvg import QSvgRenderer

from ..core import appearance as ap
from ..core import paths

# Fixed colours for things you group or label (genre, BPM, mood, categories): Nirvash keeps its colourful chips.
PALETTE = {"teal": "#2fe0c0", "violet": "#9b86ff", "pink": "#ff6fb1", "amber": "#ffbf5a", "blue": "#5aa8ff",
           "green": "#5be38a"}
CATEGORY_COLOURS = ["#9b86ff", "#2fe0c0", "#ff6fb1", "#ffbf5a", "#5aa8ff", "#5be38a", "#ff8a5c", "#d07bff",
                    "#7fd4ff", "#f2e05a"]

C: dict[str, str] = {}      # colours, by token and by the older names the screens use
T: dict = {}                # sizes and other values from the theme
THEME: dict = {}            # the normalized theme in use


def rgba(hex_: str, a: float) -> str:
    q = QColor(hex_)
    return f"rgba({q.red()},{q.green()},{q.blue()},{max(0.0, min(1.0, a)):.3f})"


def mix(a: str, b: str, t: float) -> str:
    """a mixed with t of b."""
    x, y = QColor(a), QColor(b)
    return QColor(round(x.red() + (y.red() - x.red()) * t), round(x.green() + (y.green() - x.green()) * t),
                  round(x.blue() + (y.blue() - x.blue()) * t)).name()


def set_theme(t: dict) -> None:
    """Load a theme into C / T (no Qt calls; apply() does the rest)."""
    t = ap.normalize(t)
    THEME.clear()
    THEME.update(t)
    b, s = t["background"], t["states"]
    stops = t["accent"]["stops"]
    C.clear()
    C.update(b)
    C.update(s)
    C.update(PALETTE)
    C.update({
        # older names used around the app
        "bg": b["ground"], "surface": b["panel"], "surface2": b["raised"], "surface3": b["raised2"],
        "border": b["line"], "border2": b["line2"], "muted": b["sec"], "red": s["danger"],
        "acc": stops[0], "acc2": stops[-1], "on_acc": ap.on_accent(t),
    })
    sc = t["scene"]
    op = (sc["opacity"] / 100.0) if sc["image"] != "none" else 1.0
    T.clear()
    T.update({
        "fs": t["font"]["size"], "family": t["font"]["family"], "ch": t["density"]["control"],
        "rh": t["density"]["row"], "rc": t["corners"]["control"], "rp": t["corners"]["panel"],
        "glow": t["buttons"]["style"] == "glow", "scene": sc["image"], "opacity": op,
        "angle": t["accent"]["angle"], "stops": stops, "light": ap.is_light(t),
    })
    _cache.clear()


def grad(stops=None, angle=None) -> str:
    """The accent as a QSS gradient (or a plain colour for one stop)."""
    stops = stops or T["stops"]
    angle = T["angle"] if angle is None else angle
    if len(stops) == 1:
        return stops[0]
    a = math.radians(angle)
    # CSS angles: 90deg points right, 180deg points down.
    dx, dy = math.sin(a), -math.cos(a)
    x1, y1, x2, y2 = 0.5 - dx / 2, 0.5 - dy / 2, 0.5 + dx / 2, 0.5 + dy / 2
    st = ", ".join(f"stop:{i / (len(stops) - 1):.3f} {c}" for i, c in enumerate(stops))
    return f"qlineargradient(x1:{x1:.3f},y1:{y1:.3f},x2:{x2:.3f},y2:{y2:.3f}, {st})"


def surf(key: str, extra: float = 0.0) -> str:
    """A surface colour with the see-through amount of the scene."""
    op = T.get("opacity", 1.0)
    return C[key] if op >= 1.0 else rgba(C[key], min(1.0, op + extra))


def stylesheet() -> str:
    c, t = C, T
    fs, ch, rc, rp = t["fs"], t["ch"], t["rc"], t["rp"]
    fam = t["family"].replace('"', "")
    acc, acc2, on = c["acc"], c["acc2"], c["on_acc"]
    sel = rgba(acc, 0.16)
    inner = ch - 2                      # control height inside its 1 px border
    return f"""
* {{ font-family: "{fam}", "Inter", "Segoe UI"; font-size: {fs}px; color: {c['text']}; outline: 0; }}
QMainWindow, QWidget#backdrop {{ background: transparent; }}
QWidget#page, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget, QWidget#transparent {{ background: transparent; }}
QWidget#rail {{ background: {surf('rail', 0.2)}; border-right: 1px solid {c['line']}; }}
QWidget#titlebar {{ background: transparent; border-bottom: 1px solid {c['line']}; }}
QWidget#statusbar {{ background: {surf('panel', 0.2)}; border-top: 1px solid {c['line']}; }}
QWidget#drawer {{ background: {surf('panel', 0.25)}; border-top: 1px solid {c['line']}; }}
QToolTip {{ background: {c['raised2']}; color: {c['text']}; border: 1px solid {c['line2']}; padding: 4px 7px; border-radius: {rc}px; }}
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: {fs + 2}px; font-weight: 700; }}
QLabel#h2 {{ font-size: {fs}px; font-weight: 600; }}
QLabel#cap {{ font-size: {fs - 1}px; font-weight: 600; color: {c['sec']}; }}
QLabel#muted, QLabel[muted="true"] {{ color: {c['sec']}; }}
QLabel#faint {{ color: {c['faint']}; }}
QLabel#warnText {{ color: {c['warn']}; }}
QLabel#dangerText {{ color: {c['danger']}; }}
QLabel#okText {{ color: {c['ok']}; }}
QLabel#mono {{ font-family: "JetBrains Mono", Consolas; font-size: {fs - 1}px; color: {c['sec']}; }}
QFrame#card, QFrame#panel {{ background: {surf('panel')}; border: 1px solid {c['line']}; border-radius: {rp}px; }}
QFrame#card2 {{ background: {surf('raised', 0.15)}; border: 1px solid {c['line']}; border-radius: {rc + 2}px; }}
QFrame#card2[chosen="true"] {{ border-color: {acc}; }}
QFrame#banner, QFrame#bannerWarn {{ background: {rgba(acc, 0.07)}; border: 1px solid {rgba(acc, 0.30)}; border-radius: {rp}px; }}
QFrame#selbar {{ background: {rgba(acc, 0.09)}; border: 1px solid {rgba(acc, 0.30)}; border-radius: {rc}px; }}
QWidget#stage {{ background: {c['stage']}; border: 1px solid {c['line']}; border-radius: {rp}px; }}
QFrame#hline {{ background: {c['line']}; max-height: 1px; min-height: 1px; border: none; }}

QPushButton {{ background: {surf('raised', 0.2)}; border: 1px solid {c['line']}; border-radius: {rc}px; padding: 0 10px;
    min-height: {inner}px; max-height: {inner}px; font-weight: 600; }}
QPushButton:hover {{ background: {surf('raised2', 0.25)}; border-color: {c['line2']}; }}
QPushButton:pressed {{ background: {c['raised2']}; }}
QPushButton:disabled {{ color: {c['faint']}; border-color: {c['line']}; }}
QPushButton:checked {{ background: {rgba(acc, 0.18)}; border-color: {rgba(acc, 0.6)}; }}
QPushButton:focus {{ border-color: {acc}; }}
QPushButton#primary {{ border: none; color: {on}; background: {grad()}; }}
QPushButton#primary:hover {{ background: {grad([mix(s, '#ffffff', 0.18) for s in t['stops']])}; }}
QPushButton#primary:disabled {{ background: {c['raised2']}; color: {c['faint']}; }}
QPushButton#ghost, QPushButton#flat {{ background: transparent; border-color: transparent; color: {c['sec']}; }}
QPushButton#ghost:hover, QPushButton#flat:hover {{ background: {surf('raised', 0.2)}; color: {c['text']}; }}
QPushButton#dangerGhost, QPushButton#danger {{ background: transparent; color: {c['danger']}; border-color: {rgba(c['danger'], 0.45)}; }}
QPushButton#dangerGhost:hover, QPushButton#danger:hover {{ background: {rgba(c['danger'], 0.12)}; border-color: {c['danger']}; }}
QPushButton#dangerSolid {{ background: {c['danger']}; color: #ffffff; border: none; }}
QPushButton#dangerSolid:hover {{ background: {mix(c['danger'], '#ffffff', 0.15)}; }}
QPushButton#chip {{ background: {surf('raised', 0.2)}; border: 1px solid {c['line']}; border-radius: {min(12, inner // 2)}px; padding: 0 10px;
    min-height: {max(18, inner - 6)}px; max-height: {max(18, inner - 6)}px; font-weight: 400; font-size: {fs - 1}px; }}
QPushButton#chip:hover {{ border-color: {c['line2']}; }}
QPushButton#chip:checked {{ background: {rgba(acc, 0.20)}; border-color: {acc}; }}
QPushButton#seg {{ background: transparent; border: none; border-radius: {max(0, rc - 2)}px; color: {c['sec']};
    min-height: {inner - 6}px; max-height: {inner - 6}px; padding: 0 9px; font-weight: 600; }}
QPushButton#seg:hover {{ color: {c['text']}; }}
QPushButton#seg:checked {{ background: {c['raised2']}; color: {c['text']}; }}
QFrame#segtrack {{ background: {surf('field', 0.2)}; border: 1px solid {c['line']}; border-radius: {rc}px; }}
QToolButton {{ background: transparent; border: none; border-radius: {rc}px; padding: 3px; }}
QToolButton:hover {{ background: {surf('raised', 0.2)}; }}
QToolButton:checked {{ background: {surf('raised', 0.2)}; }}
QToolButton::menu-indicator {{ image: none; }}
QToolButton#menuBtn {{ background: {surf('raised', 0.2)}; border: 1px solid {c['line']}; padding: 0 10px; min-height: {inner}px; max-height: {inner}px; font-weight: 600; }}
QToolButton#menuBtn:hover {{ background: {surf('raised2', 0.25)}; border-color: {c['line2']}; }}

QToolButton#railBtn {{ border-radius: {rc}px; padding: 5px 0 3px 0; color: {c['sec']}; font-size: 10px; }}
QToolButton#railBtn:hover {{ background: {surf('raised', 0.2)}; color: {c['text']}; }}
QToolButton#railBtn:checked {{ background: {c['raised']}; color: {c['text']}; }}
QToolButton#winBtn {{ border-radius: 0; padding: 0; }}
QToolButton#winBtn:hover {{ background: {rgba(c['text'], 0.08)}; }}
QToolButton#winClose:hover {{ background: #e81123; }}
QToolButton#winClose {{ border-radius: 0; padding: 0; }}
QFrame#pill {{ background: {surf('raised', 0.2)}; border: 1px solid {c['line']}; border-radius: 11px; }}
QFrame#pill:hover {{ border-color: {c['line2']}; }}
QFrame#hwchip {{ background: {surf('raised', 0.2)}; border: 1px solid {c['line']}; border-radius: 11px; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {surf('field', 0.25)}; border: 1px solid {c['line']}; border-radius: {rc}px; padding: 0 8px;
    min-height: {inner}px; max-height: {inner}px; selection-background-color: {acc}; selection-color: {on}; }}
QPlainTextEdit, QTextBrowser, QTextEdit {{ background: {surf('field', 0.25)}; border: 1px solid {c['line']}; border-radius: {rc}px;
    padding: 4px 6px; selection-background-color: {acc}; selection-color: {on}; }}
QPlainTextEdit#log {{ font-family: "JetBrains Mono", Consolas; font-size: {fs - 1}px; border: none; border-radius: 0; background: transparent; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {acc}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {c['faint']}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{ image: url(nv:chevron.svg); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {c['panel']}; border: 1px solid {c['line2']}; selection-background-color: {c['raised2']}; padding: 3px; outline: 0; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QCheckBox, QRadioButton {{ spacing: 7px; background: transparent; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border-radius: {min(4, rc)}px; border: 1px solid {c['line2']}; background: {c['field']}; }}
QCheckBox::indicator:hover {{ border-color: {acc}; }}
QCheckBox::indicator:checked {{ background: {acc}; border-color: {acc}; image: url(nv:check.svg); }}
QRadioButton::indicator {{ width: 14px; height: 14px; border-radius: 7px; border: 1px solid {c['line2']}; background: {c['field']}; }}
QRadioButton::indicator:checked {{ background: {acc}; border: 3px solid {c['field']}; }}

QTableView, QListWidget, QTreeWidget, QListView, QTreeView {{ background: {surf('field', 0.2)}; border: 1px solid {c['line']}; border-radius: {rp}px;
    alternate-background-color: {rgba(c['text'], 0.022)}; gridline-color: transparent; selection-background-color: {sel};
    selection-color: {c['text']}; }}
QTableView::item, QListWidget::item, QTreeWidget::item {{ padding: 0 6px; border: none; }}
QTableView::item:hover, QListWidget::item:hover, QTreeWidget::item:hover {{ background: {rgba(c['text'], 0.035)}; }}
QTableView::item:selected, QListWidget::item:selected, QTreeWidget::item:selected {{ background: {sel}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {surf('panel', 0.2)}; color: {c['sec']}; border: none; border-bottom: 1px solid {c['line']};
    padding: 0 8px; height: {ch}px; font-size: {fs - 1}px; font-weight: 600; }}
QHeaderView::down-arrow, QHeaderView::up-arrow {{ width: 9px; height: 9px; subcontrol-position: center right; right: 4px; }}
QHeaderView::up-arrow {{ image: url(nv:sort_up.svg); }}
QHeaderView::down-arrow {{ image: url(nv:sort_down.svg); }}
QTableCornerButton::section {{ background: transparent; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['line2']}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {mix(c['line2'], c['text'], 0.2)}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['line2']}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}

QProgressBar {{ background: {c['raised2']}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 3px; background: {grad(angle=90)}; }}
QProgressBar[stalled="true"]::chunk {{ background: {c['warn']}; }}
QSlider::groove:horizontal {{ height: 4px; background: {c['raised2']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ height: 4px; border-radius: 2px; background: {grad(angle=90)}; }}
QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; background: {c['text']}; }}
QSplitter::handle {{ background: transparent; }}
QMenu {{ background: {c['panel']}; border: 1px solid {c['line2']}; border-radius: {rc}px; padding: 4px; }}
QMenu::item {{ padding: 5px 20px 5px 10px; border-radius: {max(0, rc - 2)}px; }}
QMenu::item:selected {{ background: {c['raised2']}; }}
QMenu::item:disabled {{ color: {c['faint']}; }}
QMenu::separator {{ height: 1px; background: {c['line']}; margin: 4px 6px; }}
QMenu::indicator {{ width: 13px; height: 13px; margin-left: 5px; }}
QDialog {{ background: {c['panel']}; }}
QFrame#dialog {{ background: {c['panel']}; border: 1px solid {c['line2']}; border-radius: {rp}px; }}
QTabBar::tab {{ background: transparent; color: {c['sec']}; padding: 4px 10px; border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {acc}; }}
"""


# ---------------------------------------------------------------------------
# Applying
_fonts_loaded: set[str] = set()


def load_font(family_id: str) -> None:
    """Register a bundled or added font family with Qt (once)."""
    if family_id in _fonts_loaded:
        return
    _fonts_loaded.add(family_id)
    for f in ap.bundled_fonts():
        if f["id"] == family_id:
            for x in f["files"]:
                QFontDatabase.addApplicationFont(str(paths.asset("fonts") / f["id"] / x["file"]))
            return
    for f in ap.user_fonts():
        if f["id"] == family_id:
            for x in f["files"]:
                QFontDatabase.addApplicationFont(str(ap.user_fonts_dir() / f["id"] / x["file"]))
            return


def font_id(family: str) -> str | None:
    for f in ap.bundled_fonts() + ap.user_fonts():
        if f["family"].casefold() == family.casefold():
            return f["id"]
    return None


def load_all_fonts() -> None:
    for f in ap.bundled_fonts() + ap.user_fonts():
        load_font(f["id"])


def font_available(family: str) -> bool:
    return family in QFontDatabase.families()


def _write_svg_resources() -> None:
    import tempfile
    from pathlib import Path

    from PySide6.QtCore import QDir

    d = Path(tempfile.gettempdir()) / "nirvash-ui"
    d.mkdir(exist_ok=True)
    (d / "check.svg").write_text(svg("check", C["on_acc"], 3.0), "utf-8")
    (d / "chevron.svg").write_text(svg("chev", C["sec"], 2.2), "utf-8")
    (d / "sort_up.svg").write_text(svg("tri_up", C["acc"], 0), "utf-8")
    (d / "sort_down.svg").write_text(svg("tri_down", C["acc"], 0), "utf-8")
    QDir.setSearchPaths("nv", [str(d)])


def apply(app, t: dict | None = None) -> None:
    """Use a theme (default: the saved current one) for the whole app."""
    set_theme(t if t is not None else ap.get_appearance().current)
    fid = font_id(T["family"])
    if fid:
        load_font(fid)
    load_font("inter")
    load_font("jetbrains-mono")
    if not font_available(T["family"]):
        T["family"] = "Inter"
    f = QFont(T["family"])
    f.setPixelSize(T["fs"])
    f.setHintingPreference(QFont.PreferNoHinting)
    f.setFeature(QFont.Tag("tnum"), 1) if hasattr(QFont, "Tag") else None
    app.setFont(f)
    _write_svg_resources()
    app.setStyleSheet(stylesheet())


# Kept for older callers ------------------------------------------------------
def check_svg_resource() -> None:
    if not C:
        set_theme(ap.get_appearance().current)
    _write_svg_resources()


def fixed_stylesheet() -> str:
    if not C:
        set_theme(ap.get_appearance().current)
    return stylesheet()


def apply_font(app) -> None:
    if not C:
        set_theme(ap.get_appearance().current)
    load_font("inter")
    f = QFont(T["family"])
    f.setPixelSize(T["fs"])
    app.setFont(f)


# ---------------------------------------------------------------------------
# The starfield scene behind see-through panels
_scene_cache: dict = {}


def scene_pixmap(w: int, h: int, dpr: float = 1.0) -> QPixmap | None:
    """Stars and a faint nebula in the accent colours, the same every time for the same size."""
    if T.get("scene") != "starfield" or w <= 0 or h <= 0:
        return None
    key = (w // 64 + 1, h // 64 + 1, round(dpr, 2), tuple(T["stops"]), C["ground"])
    pm = _scene_cache.get(key)
    if pm is not None:
        return pm
    W, H = (key[0] * 64, key[1] * 64)
    pm = QPixmap(int(W * dpr), int(H * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(QColor(C["ground"]))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    stops = T["stops"]

    def neb(x, y, r, col, a):
        g = QRadialGradient(QPointF(x, y), r)
        q = QColor(col)
        q.setAlphaF(a)
        g.setColorAt(0, q)
        q2 = QColor(col)
        q2.setAlphaF(0)
        g.setColorAt(1, q2)
        p.fillRect(QRectF(0, 0, W, H), g)

    neb(W * 0.82, H * 0.14, max(W, H) * 0.42, stops[-1], 0.22)
    neb(W * 0.18, H * 0.88, max(W, H) * 0.45, stops[0], 0.14)
    neb(W * 0.53, H * 0.49, max(W, H) * 0.30, PALETTE["pink"], 0.05)
    rnd = random.Random(7)
    n = int(W * H / 820)
    p.setPen(Qt.NoPen)
    for _ in range(n):
        x, y = rnd.random() * W, rnd.random() * H
        big = rnd.random() > 0.965
        r = (1.0 + rnd.random() * 0.7) if big else (0.3 + rnd.random() * 0.5)
        k = rnd.random()
        col = QColor("#beb4ff") if k > 0.85 else QColor("#a0ffeb") if k > 0.75 else QColor("#ffffff")
        col.setAlphaF(0.35 + rnd.random() * 0.6)
        p.setBrush(col)
        p.drawEllipse(QPointF(x, y), r, r)
        if big and rnd.random() > 0.5:
            glow = QColor(col)
            glow.setAlphaF(0.12)
            p.setBrush(glow)
            p.drawEllipse(QPointF(x, y), r * 3, r * 3)
    p.end()
    if len(_scene_cache) > 6:
        _scene_cache.clear()
    _scene_cache[key] = pm
    return pm


def paint_backdrop(p: QPainter, rect, dpr: float = 1.0) -> None:
    """Paint the window background: the ground colour, plus the scene when there is one."""
    pm = scene_pixmap(rect.width(), rect.height(), dpr)
    if pm is None:
        p.fillRect(rect, QColor(C["ground"]))
    else:
        p.drawPixmap(0, 0, pm)


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
    "unlink": '<path d="m18.84 12.25 1.72-1.71a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="m5.17 11.75-1.71 1.71a5 5 0 0 0 7.07 7.07l1.71-1.71"/><path d="M8 2v3M2 8h3M16 22v-3M22 16h-3"/>',
    "filter": '<path d="M22 3H2l8 9.46V19l4 2v-8.54z"/>',
    "badge": '<path d="M12 2l2.4 2.1 3.2-.4.9 3.1 2.8 1.6-1.2 3 1.2 3-2.8 1.6-.9 3.1-3.2-.4L12 22l-2.4-2.1-3.2.4-.9-3.1-2.8-1.6 1.2-3-1.2-3 2.8-1.6.9-3.1 3.2.4z"/><path d="m8.5 12 2.3 2.3 4.7-4.6"/>',
    "film": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 7.5h4M3 12h18M3 16.5h4M17 7.5h4M17 16.5h4"/>',
    "sparkle": '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" fill="currentColor"/>',
    "download": '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 16v-5M12 8h.01"/>',
    "lyrics": '<path d="M4 5h16M4 10h10M4 15h12M4 20h7"/>',
    "look": '<circle cx="13.5" cy="6.5" r="1.5"/><circle cx="17.5" cy="10.5" r="1.5"/><circle cx="8.5" cy="7.5" r="1.5"/><circle cx="6.5" cy="12.5" r="1.5"/><path d="M12 22a10 10 0 1 1 10-10c0 2.8-2.2 3-4 3h-2a2 2 0 0 0-1 3.7A2 2 0 0 1 12 22z"/>',
    "keys": '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
    "win_min": '<path d="M5 12h14"/>',
    "win_max": '<rect x="5" y="5" width="14" height="14" rx="1"/>',
    "win_restore": '<rect x="5" y="8" width="11" height="11" rx="1"/><path d="M8 8V5h11v11h-3"/>',
    "win_close": '<path d="M6 6l12 12M18 6 6 18"/>',
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "chip": '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    "drive": '<path d="M22 12H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/><path d="M6 16h.01M10 16h.01"/>',
    "move": '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "warn": '<path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3z"/><path d="M12 9v4M12 17h.01"/>',
    "chev": '<path d="m6 9 6 6 6-6"/>',
    "tri_up": '<path d="M12 6 20 18H4z" fill="currentColor"/>',
    "tri_down": '<path d="M12 18 4 6h16z" fill="currentColor"/>',
    "skip": '<path d="M5 4v16l10-8z" fill="currentColor"/><path d="M19 5v14"/>',
}




def svg(name: str, color: str | None = None, stroke: float = 1.6) -> str:
    color = color or C.get("text", "#ececee")
    body = _P.get(name, _P["info"]).replace("currentColor", color)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')


_cache: dict[tuple, QIcon] = {}


def pixmap(name: str, size: int = 18, color: str | None = None, dpr: float = 2.0) -> QPixmap:
    r = QSvgRenderer(QByteArray(svg(name, color).encode()))
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p, QRectF(0, 0, size * dpr, size * dpr))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, color: str | None = None, size: int = 16, checked_color: str | None = None) -> QIcon:
    color = color or C.get("text", "#ececee")
    key = (name, color, size, checked_color)
    if key in _cache:
        return _cache[key]
    ic = QIcon()
    ic.addPixmap(pixmap(name, size, color), QIcon.Normal, QIcon.Off)
    ic.addPixmap(pixmap(name, size, C.get("faint", "#7d7d84")), QIcon.Disabled, QIcon.Off)
    if checked_color:
        ic.addPixmap(pixmap(name, size, checked_color), QIcon.Normal, QIcon.On)
    _cache[key] = ic
    return ic


def qcolor(name: str, alpha: int = 255) -> QColor:
    c = QColor(C.get(name, name))
    c.setAlpha(alpha)
    return c


ICON_SIZE = QSize(16, 16)
set_theme(ap.DEFAULT_THEME)
