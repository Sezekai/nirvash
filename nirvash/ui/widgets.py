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
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(p if p.exists() else p.parent))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])


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
