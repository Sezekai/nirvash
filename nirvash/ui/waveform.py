"""Waveform strip with playhead; click or drag to seek."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ..core.settings import get_settings
from ..media import ff
from . import workers
from .theme import C


def compute_peaks(path: str, key: str, n: int = 900) -> np.ndarray:
    cache = get_settings().cache("waveforms") / f"{key}.npy"
    if cache.exists():
        try:
            return np.load(cache)
        except Exception:
            pass
    y = np.abs(ff.decode_audio(path, 4000))
    if len(y) == 0:
        return np.zeros(n, np.float32)
    k = max(1, len(y) // n)
    y = y[: k * (len(y) // k)].reshape(-1, k)
    peaks = np.sqrt((y ** 2).mean(axis=1)) * 0.6 + y.max(axis=1) * 0.4
    peaks = peaks / max(float(peaks.max()), 1e-6)
    peaks = peaks.astype(np.float32)
    try:
        np.save(cache, peaks)
    except Exception:
        pass
    return peaks


class Waveform(QWidget):
    seekRequested = Signal(float)
    menuRequested = Signal(object)      # global QPoint: right-click (the library offers Trim / volume there)

    def __init__(self, parent=None, height: int = 46):
        super().__init__(parent)
        self.setMinimumHeight(height)
        self.setCursor(Qt.PointingHandCursor)
        self.peaks: np.ndarray | None = None
        self.duration = 0.0
        self.pos = 0.0
        self.mark: tuple[float, float] | None = None   # highlighted range (e.g. the part used)
        self._token = 0
        self._dragging = False

    def load(self, path: str | None, key: str, duration: float) -> None:
        self._token += 1
        tok = self._token
        self.peaks = None
        self.duration = duration
        self.pos = 0.0
        self.update()
        if not path or not Path(path).exists():
            return
        workers.run_bg(compute_peaks, path, key, done=lambda p, t=tok: self._set(t, p))

    def _set(self, tok: int, peaks) -> None:
        if tok == self._token:
            self.peaks = peaks
            self.update()

    def set_position(self, s: float) -> None:
        if not self._dragging:
            self.pos = s
            self.update()

    def set_mark(self, a: float | None, b: float | None) -> None:
        self.mark = None if a is None or b is None else (a, b)
        self.update()

    def _x_to_t(self, x: float) -> float:
        return max(0.0, min(1.0, x / max(1, self.width()))) * self.duration

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.duration > 0:
            self._dragging = True
            self.pos = self._x_to_t(e.position().x())
            self.update()

    def mouseMoveEvent(self, e):
        if self._dragging:
            self.pos = self._x_to_t(e.position().x())
            self.update()

    def contextMenuEvent(self, e):  # noqa: N802
        self.menuRequested.emit(e.globalPos())

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self._dragging = False
            self.seekRequested.emit(self._x_to_t(e.position().x()))

    def _bars(self) -> tuple:
        """The bars drawn twice, once in the played colours and once in the rest colour, kept until the size,
        the song or the theme changes. Painting is then two pixmaps and a line, which keeps playback smooth."""
        w, h = self.width(), self.height()
        dpr = self.devicePixelRatioF()
        key = (w, h, dpr, id(self.peaks), C["teal"], C["violet"], C["surface3"])
        if getattr(self, "_bars_key", None) == key:
            return self._bars_pm
        mid = h / 2
        n = len(self.peaks)
        bar, gap = 3.0, 1.0
        count = max(1, int(w / (bar + gap)))
        idx = np.linspace(0, n - 1, count).astype(int)
        path = QPainterPath()
        for i, v in enumerate(self.peaks[idx]):
            bh = max(2.0, float(v) * (h - 6))
            path.addRoundedRect(QRectF(i * (bar + gap), mid - bh / 2, bar, bh), 1.2, 1.2)
        played = QLinearGradient(0, 0, w, 0)
        played.setColorAt(0, QColor(C["teal"]))
        played.setColorAt(1, QColor(C["violet"]))
        out = []
        for brush in (QColor(C["surface3"]).lighter(135), played):
            pm = QPixmap(max(1, int(w * dpr)), max(1, int(h * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            q = QPainter(pm)
            q.setRenderHint(QPainter.Antialiasing)
            q.fillPath(path, brush)
            q.end()
            out.append(pm)
        self._bars_key, self._bars_pm = key, tuple(out)
        return self._bars_pm

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        mid = h / 2
        frac = (self.pos / self.duration) if self.duration > 0 else 0.0
        px = frac * w
        if self.mark and self.duration > 0:
            a, b = self.mark
            r = QRectF(a / self.duration * w, 0, max(2.0, (b - a) / self.duration * w), h)
            p.fillRect(r, QColor(47, 224, 192, 26))
        if self.peaks is None or len(self.peaks) == 0:
            p.setPen(QPen(QColor(C["border"]), 1))
            p.drawLine(0, int(mid), w, int(mid))
        else:
            rest, played = self._bars()
            p.drawPixmap(0, 0, rest)
            if px > 0:
                p.save()
                p.setClipRect(QRectF(0, 0, px, h))
                p.drawPixmap(0, 0, played)
                p.restore()
        if self.duration > 0:
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(C["text"]), 1.6))
            p.drawLine(QPointF(px, 2), QPointF(px, h - 2))
        p.end()
