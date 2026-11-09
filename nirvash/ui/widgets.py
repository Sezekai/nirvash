"""Small shared widgets."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QLayout, QPushButton,
                               QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget)

from ..media.ff import fmt_time
from . import theme
from .theme import C, icon


def card(name: str = "card") -> QFrame:
    f = QFrame()
    f.setObjectName(name)
    return f


def panel() -> QFrame:
    return card("panel")


def page_header(title: str, desc: str = "") -> tuple[QHBoxLayout, QLabel]:
    """One line: the screen's title, then what it does (cut short with … when narrow)."""
    row = QHBoxLayout()
    row.setSpacing(10)
    t = QLabel(title)
    t.setObjectName("h1")
    row.addWidget(t)
    d = ElidedLabel(desc)
    d.setObjectName("faint")
    row.addWidget(d, 1)
    return row, d


class ElidedLabel(QLabel):
    """A one-line label that cuts long text with … instead of widening the window."""

    def __init__(self, text: str = "", parent=None, mode=Qt.ElideRight):
        super().__init__(parent)
        self._full = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(20)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        if (text or "") == getattr(self, "_full", None):
            return
        self._full = text or ""
        self.setToolTip(self._full if len(self._full) > 40 else "")
        self._elide()

    def fullText(self) -> str:  # noqa: N802
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def changeEvent(self, e):  # noqa: N802 - a new font (Look screen) changes where the text needs cutting
        super().changeEvent(e)
        if e.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._elide()

    def _elide(self) -> None:
        w = max(10, self.width())
        t = self.fontMetrics().elidedText(self._full, self._mode, w)
        if t != super().text():
            super().setText(t)


class Segmented(QFrame):
    """A segmented control: one choice of a few, counts in faint text after the labels."""

    changed = Signal(object)

    def __init__(self, options: list[tuple[object, str]], value=None, parent=None):
        super().__init__(parent)
        self.setObjectName("segtrack")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict = {}
        self.labels: dict = {}
        for key, text in options:
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._pick(k))
            self.group.addButton(b)
            lay.addWidget(b)
            self.buttons[key] = b
            self.labels[key] = text
        self.value = None
        self.set_value(value if value is not None else options[0][0])
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

    def _pick(self, key) -> None:
        if key != self.value:
            self.value = key
            self.changed.emit(key)

    def set_value(self, key) -> None:
        self.value = key
        if key in self.buttons:
            self.buttons[key].setChecked(True)

    def set_count(self, key, n: int | None) -> None:
        if key in self.buttons:
            self.buttons[key].setText(self.labels[key] + (f"  {n:,}" if n is not None else ""))


def hline() -> QFrame:
    f = QFrame()
    f.setObjectName("hline")
    return f


def label(text: str = "", obj: str | None = None, wrap: bool = False) -> QLabel:
    l = QLabel(text)
    if obj:
        l.setObjectName(obj)
    l.setWordWrap(wrap)
    return l


def tool(name: str, tip: str, color: str | None = None, size: int = 16, checkable: bool = False,
         checked_color: str | None = None) -> QToolButton:
    color = color or C["text"]
    b = QToolButton()
    b.setIcon(icon(name, color, size, checked_color))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setCursor(Qt.PointingHandCursor)
    return b


def button(text: str, icon_name: str | None = None, obj: str | None = None, color: str | None = None) -> QPushButton:
    b = QPushButton(text)
    if icon_name:
        col = C["on_acc"] if obj == "primary" else (C["danger"] if obj in ("dangerGhost", "danger") else
                                                    (C["sec"] if obj == "ghost" else (color or C["text"])))
        b.setIcon(icon(icon_name, col, 15))
        b.setIconSize(QSize(15, 15))
    if obj:
        b.setObjectName(obj)
    b.setCursor(Qt.PointingHandCursor)
    return b


def chip(text: str, color: str | None = None, filled: bool = False) -> QLabel:
    """A 20 px round tag: state (ok / warn / danger), or a colour you gave a group."""
    color = color or C["violet"]
    l = QLabel(text)
    qc = QColor(color)
    bg = f"rgba({qc.red()},{qc.green()},{qc.blue()},{0.9 if filled else 0.14})"
    bd = f"rgba({qc.red()},{qc.green()},{qc.blue()},{0.9 if filled else 0.55})"
    fg = "#0b0c0f" if filled else color
    l.setStyleSheet(f"background:{bg}; color:{fg}; border:1px solid {bd}; border-radius:9px; padding:0 7px;"
                    f"min-height:16px; max-height:16px; font-size:11px; font-weight:600;")
    l.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return l


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps onto new lines (for rows of chips and buttons)."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        from PySide6.QtCore import Qt as _Qt
        return _Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._layout(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        sz = QSize()
        for it in self._items:
            sz = sz.expandedTo(it.minimumSize())
        return sz

    def _layout(self, rect, test: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint().expandedTo(it.minimumSize()).boundedTo(it.maximumSize())
            if x + hint.width() > rect.right() + 1 and line > 0:
                x = rect.x()
                y += line + self._sp
                line = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._sp
            line = max(line, hint.height())
        return y + line - rect.y()


def flow_row(spacing: int = 6) -> tuple[QWidget, "FlowLayout"]:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    return w, FlowLayout(w, spacing)


def clear_layout(lay: QLayout) -> None:
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif it.layout() is not None:
            clear_layout(it.layout())


def open_in_explorer(path: str) -> None:
    p = Path(path)
    if sys.platform == "win32":
        if p.is_file():
            from ..media.ff import popen
            popen(["explorer", "/select,", str(p)], creationflags=0)
        else:
            os.startfile(str(p if p.exists() else p.parent))  # type: ignore[attr-defined]
    else:
        from ..media.ff import popen
        popen(["xdg-open", str(p if p.is_dir() else p.parent)])


def open_with_default(path: str) -> bool:
    """Open a file in whatever program Windows uses for it (like double-clicking it in Explorer)."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    if not Path(path).exists():
        return False
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def cover_pixmap(path: str | None, size: int, seed: str = "", radius: int = 12) -> QPixmap:
    """Cover art, or a colourful placeholder with a note."""
    dpr = 2.0
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(0, 0, size, size, radius, radius)
    p.setClipPath(clip)
    src = QPixmap(path) if path and Path(path).exists() else QPixmap()
    if not src.isNull():
        s = src.scaled(int(size * dpr), int(size * dpr), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        s.setDevicePixelRatio(dpr)
        p.drawPixmap(0, 0, s)
    else:
        h = abs(hash(seed)) % 360
        g = QLinearGradient(0, 0, size, size)
        g.setColorAt(0, QColor.fromHsl(h, 150, 70))
        g.setColorAt(1, QColor.fromHsl((h + 60) % 360, 170, 40))
        p.fillRect(0, 0, size, size, g)
        note = theme.pixmap("library", int(size * 0.42), "#ffffff")
        p.setOpacity(0.55)
        p.drawPixmap(int(size * 0.29), int(size * 0.29), note)
    p.end()
    return pm


class PlayerBar(QFrame):
    """Play button, title, waveform, time and volume - drives a Player. One compact line."""

    def __init__(self, player, parent=None, show_title: bool = True):
        super().__init__(parent)
        from ..core.settings import get_settings
        from .waveform import Waveform

        self.setObjectName("panel")
        self.player = player
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 5, 10, 5)
        lay.setSpacing(10)
        self.play_btn = QToolButton()
        self.play_btn.setObjectName("playRound")
        self.play_btn.setIconSize(QSize(13, 13))
        self.play_btn.setFixedSize(28, 28)
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setToolTip("Play / pause (Space)")
        self.play_btn.clicked.connect(self.player.toggle)
        self._restyle()
        lay.addWidget(self.play_btn)
        self.title = ElidedLabel("Nothing playing")
        self.title.setObjectName("h2")
        self.title.setFixedWidth(240)
        self.title.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)   # Ignored would let the time overlap it
        lay.addWidget(self.title)
        if not show_title:      # only after it has a parent: showing a parentless label makes it a window
            self.title.hide()
        self.t_pos = label("0:00", "muted")
        self.wave = Waveform(height=26)
        self.t_dur = label("0:00", "muted")
        lay.addWidget(self.t_pos)
        lay.addWidget(self.wave, 1)
        lay.addWidget(self.t_dur)
        self.vol_icon = QLabel()
        self.vol_icon.setPixmap(theme.pixmap("volume", 15, C["sec"]))
        lay.addWidget(self.vol_icon)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setFixedWidth(84)
        self.vol.setValue(int(get_settings().get("ui.volume", 80)))
        self.vol.valueChanged.connect(self._vol)
        self.player.set_volume(self.vol.value())
        lay.addWidget(self.vol)
        self.wave.seekRequested.connect(self.player.seek)
        self.player.positionChanged.connect(self._pos)
        self.player.durationChanged.connect(self._dur)
        self.player.playingChanged.connect(self._playing)
        self._on = False

    def _restyle(self) -> None:
        self.play_btn.setStyleSheet(
            f"QToolButton#playRound {{ border-radius: 14px; background: {theme.grad()}; padding: 0; }}"
            f"QToolButton#playRound:hover {{ background: {C['text']}; }}")
        self.play_btn.setIcon(icon("pause" if getattr(self, "_on", False) else "play", C["on_acc"], 13))

    def nudge_volume(self, delta: int) -> None:
        self.vol.setValue(max(0, min(100, self.vol.value() + delta)))

    def _vol(self, v: int) -> None:
        from ..core.settings import get_settings
        self.player.set_volume(v)
        get_settings().set("ui.volume", v)

    def _pos(self, s: float) -> None:
        self.t_pos.setText(fmt_time(s))
        self.wave.set_position(s)

    def _dur(self, s: float) -> None:
        if s > 0:
            self.t_dur.setText(fmt_time(s))
            self.wave.duration = s

    def _playing(self, on: bool) -> None:
        self._on = on
        self.play_btn.setIcon(icon("pause" if on else "play", C["on_acc"], 13))

    def load(self, path: str, key: str, duration: float, title: str, play: bool = True) -> None:
        self.title.setText(title)
        self.t_dur.setText(fmt_time(duration))
        self.wave.load(path, key, duration)
        self.player.load(path, play=play)


class Toggle(QCheckBox):
    """An on/off option: a native checkbox in the accent (DESIGN.md). Kept under its old name."""

    toggledOn = Signal(bool)

    def __init__(self, checked: bool = False, parent=None, text: str = ""):
        super().__init__(text, parent)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.toggled.connect(self.toggledOn.emit)


def fmt_dur(s: float) -> str:
    s = int(max(0, round(s)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


class TaskProgress(QWidget):
    """Progress bar + percentage + time elapsed + time left + what it's doing right now.

    The elapsed time ticks every second, so it's always visible that the task is still running."""

    def __init__(self, parent=None, compact: bool = False):
        super().__init__(parent)
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QProgressBar

        self.compact = compact
        lay = QHBoxLayout(self) if compact else QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8 if compact else 4)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        if compact:
            self.bar.setFixedWidth(160)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.pct = label("0%")
        self.pct.setStyleSheet("font-weight:600;")
        self.times = label("", "muted")
        self.detail = label("", "muted")
        self.detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        if compact:
            for w in (self.bar, self.pct, self.times):
                lay.addWidget(w)
            lay.addWidget(self.detail, 1)
        else:
            top.addWidget(self.pct)
            top.addWidget(self.times, 1)
            lay.addWidget(self.bar)
            lay.addLayout(top)
            lay.addWidget(self.detail)
        self.t0 = 0.0
        self.frac = 0.0
        self.running = False
        self.timer = QTimer(self, interval=1000, timeout=self._tick)

    def start(self, detail: str = "", t0: float | None = None) -> None:
        import time
        self.t0 = t0 or time.time()
        self.frac = 0.0
        self.running = True
        self.bar.setValue(0)
        self.detail.setText(detail)
        self.setVisible(True)
        self.timer.start()
        self._tick()

    def update(self, frac: float, detail: str | None = None) -> None:
        if not self.running:
            self.start()
        self.frac = max(0.0, min(0.99, float(frac)))      # 100% only when finish() says it's done
        self.bar.setValue(int(self.frac * 1000))
        if detail is not None:
            self.detail.setText(detail)
            self.detail.setToolTip(detail)
        self._tick()

    def _tick(self) -> None:
        import time
        if not self.running:
            return
        el = time.time() - self.t0
        self.pct.setText(f"{self.frac * 100:.0f}%")
        if self.frac >= 0.01 and el >= 3:
            left = el * (1 - self.frac) / self.frac
            eta = f"about {fmt_dur(left)} left"
        else:
            eta = "working out time left…"
        self.times.setText(f"{fmt_dur(el)} elapsed · {eta}")

    def finish(self, text: str = "") -> None:
        import time
        if self.running:
            el = time.time() - self.t0
            self.bar.setValue(int(self.frac * 1000))
            self.times.setText(f"took {fmt_dur(el)}")
        self.running = False
        self.timer.stop()
        if text:
            self.detail.setText(text)
            self.detail.setToolTip(text)
            if not text.lower().startswith(("stop", "fail")):
                self.bar.setValue(1000)
                self.pct.setText("100%")
