"""Small shared widgets."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy, QSlider, QToolButton,
                               QVBoxLayout, QWidget)

from ..media.ff import fmt_time
from . import theme
from .theme import C, icon


def card(name: str = "card") -> QFrame:
    f = QFrame()
    f.setObjectName(name)
    return f


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


def tool(name: str, tip: str, color: str = C["text"], size: int = 18, checkable: bool = False,
         checked_color: str | None = None) -> QToolButton:
    b = QToolButton()
    b.setIcon(icon(name, color, size, checked_color))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setCursor(Qt.PointingHandCursor)
    return b


def button(text: str, icon_name: str | None = None, obj: str | None = None, color: str = C["text"]) -> QPushButton:
    b = QPushButton(text)
    if icon_name:
        b.setIcon(icon(icon_name, "#0b0c14" if obj == "primary" else color, 16))
        b.setIconSize(QSize(16, 16))
    if obj:
        b.setObjectName(obj)
    b.setCursor(Qt.PointingHandCursor)
    return b


def chip(text: str, color: str = C["violet"], filled: bool = False) -> QLabel:
    l = QLabel(text)
    qc = QColor(color)
    bg = f"rgba({qc.red()},{qc.green()},{qc.blue()},{0.9 if filled else 0.16})"
    fg = "#0b0c14" if filled else color
    l.setStyleSheet(f"background:{bg}; color:{fg}; border-radius:9px; padding:2px 9px; font-size:8.5pt; font-weight:600;")
    l.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return l


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
    """Play button, title, waveform, time and volume - drives a Player."""

    def __init__(self, player, parent=None, show_title: bool = True):
        super().__init__(parent)
        from ..core.settings import get_settings
        from .waveform import Waveform

        self.setObjectName("card")
        self.player = player
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(12)
        self.play_btn = QToolButton()
        self.play_btn.setIcon(icon("play", "#0b0c14", 18))
        self.play_btn.setIconSize(QSize(18, 18))
        self.play_btn.setFixedSize(38, 38)
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setToolTip("Play / pause (Space)")
        self.play_btn.setStyleSheet(
            f"QToolButton {{ border-radius: 19px; background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {C['teal']}, stop:1 {C['violet']}); }}"
            "QToolButton:hover { background: #ffffff; }")
        self.play_btn.clicked.connect(self.player.toggle)
        lay.addWidget(self.play_btn)
        mid = QVBoxLayout()
        mid.setSpacing(2)
        self.title = label("Nothing playing", "muted")
        self.title.setVisible(show_title)
        mid.addWidget(self.title)
        row = QHBoxLayout()
        self.t_pos = label("0:00", "muted")
        self.wave = Waveform(height=38)
        self.t_dur = label("0:00", "muted")
        row.addWidget(self.t_pos)
        row.addWidget(self.wave, 1)
        row.addWidget(self.t_dur)
        mid.addLayout(row)
        lay.addLayout(mid, 1)
        vol_icon = QLabel()
        vol_icon.setPixmap(theme.pixmap("volume", 16, C["muted"]))
        lay.addWidget(vol_icon)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setFixedWidth(90)
        self.vol.setValue(int(get_settings().get("ui.volume", 80)))
        self.vol.valueChanged.connect(self._vol)
        self.player.set_volume(self.vol.value())
        lay.addWidget(self.vol)
        self.wave.seekRequested.connect(self.player.seek)
        self.player.positionChanged.connect(self._pos)
        self.player.durationChanged.connect(self._dur)
        self.player.playingChanged.connect(self._playing)

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
        self.play_btn.setIcon(icon("pause" if on else "play", "#0b0c14", 18))

    def load(self, path: str, key: str, duration: float, title: str, play: bool = True) -> None:
        self.title.setText(title)
        self.t_dur.setText(fmt_time(duration))
        self.wave.load(path, key, duration)
        self.player.load(path, play=play)


class Toggle(QPushButton):
    """A pill switch."""

    toggledOn = Signal(bool)

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(40, 22)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")
        self.toggled.connect(lambda v: (self.update(), self.toggledOn.emit(v)))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        bg = QColor(C["teal"]) if on else QColor(C["surface3"])
        p.setBrush(bg)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, 40, 22, 11, 11)
        p.setBrush(QColor("#0b0c14") if on else QColor(C["muted"]))
        p.drawEllipse(21 if on else 3, 3, 16, 16)
        p.end()


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
        self.pct.setStyleSheet(f"font-weight:700; color:{C['teal']};")
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
        self.frac = max(0.0, min(1.0, float(frac)))
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
