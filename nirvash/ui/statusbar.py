"""The status bar on every screen, and the matching pills in the title bar.

Shows the running job (or song identification when nothing else runs): name, what it's on, a bar, %, time elapsed
and time left, a warning when it stalls, how many more are queued, the hardware chip for AI work, Pause / Stop and
Log. When idle it shows the last result, which stays until the next job (STANDARDS.md, "Progress you can trust")."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QWidget

from ..core import gpu
from . import theme
from .jobqueue import JobQueue
from .theme import C, icon
from .widgets import ElidedLabel, button, fmt_dur, label

STALL_S = 20


def _style(w, css: str) -> None:
    """setStyleSheet only when it changes: restyling re-polishes the widget, which is slow when done every tick."""
    if w.property("_nv_css") != css:
        w.setProperty("_nv_css", css)
        w.setStyleSheet(css)


def _icon(btn, name: str, colour: str, size: int) -> None:
    key = f"{name}|{colour}|{size}"
    if btn.property("_nv_icon") != key:
        btn.setProperty("_nv_icon", key)
        btn.setIcon(icon(name, colour, size))


def _text(w, txt: str) -> None:
    if w.text() != txt:
        w.setText(txt)


def times_text(el: float, frac: float) -> str:
    if frac >= 0.01 and el >= 3:
        return f"{fmt_dur(el)} elapsed · about {fmt_dur(el * (1 - frac) / frac)} left"
    return f"{fmt_dur(el)} elapsed · working out time left…"


class MiniBar(QWidget):
    """A 46 px progress bar for the title bar pills (slides when the size isn't known)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(46, 4)
        self.frac = 0.0
        self.sliding = False
        self.t = 0.0

    def set(self, frac: float, sliding: bool = False) -> None:
        self.frac, self.sliding = frac, sliding
        self.t = time.time()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(C["raised2"]))
        p.drawRoundedRect(QRectF(0, 0, 46, 4), 2, 2)
        p.setBrush(QColor(C["acc"]))
        if self.sliding:
            x = (time.time() * 40) % 62 - 16
            p.drawRoundedRect(QRectF(max(0, x), 0, min(16, 46 - max(0, x)), 4), 2, 2)
        else:
            p.drawRoundedRect(QRectF(0, 0, 46 * max(0.0, min(1.0, self.frac)), 4), 2, 2)


class Pill(QFrame):
    """'● Finding music 12/40 · 34%' in the title bar."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("pill")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(22)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 9, 0)
        lay.setSpacing(6)
        self.led = QLabel()
        self.led.setFixedSize(6, 6)
        lay.addWidget(self.led)
        self.txt = QLabel()
        self.txt.setObjectName("muted")
        self.txt.setStyleSheet("font-size: 11px;")
        lay.addWidget(self.txt)
        self.bar = MiniBar()
        lay.addWidget(self.bar)
        self.pct = QLabel()
        self.pct.setObjectName("muted")
        self.pct.setStyleSheet("font-size: 11px;")
        lay.addWidget(self.pct)
        self.set_state("run")

    def set_state(self, state: str) -> None:
        col = {"run": C["acc"], "pause": C["warn"], "stall": C["warn"]}.get(state, C["acc"])
        _style(self.led, f"background:{col}; border-radius:3px;")

    def mousePressEvent(self, e):
        self.clicked.emit()


class HwChip(QFrame):
    """What the AI work runs on: 'RTX 3060 Laptop GPU · 87% · 3.1 / 6.0 GB', or 'On the CPU (slow)' in amber."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hwchip")
        self.setFixedHeight(22)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 9, 0)
        lay.setSpacing(6)
        self.ring = QLabel()
        self.ring.setFixedSize(14, 14)
        lay.addWidget(self.ring)
        self.txt = QLabel()
        _style(self.txt, "font-size: 11px;")
        lay.addWidget(self.txt)
        self.stats: dict | None = None
        self._busy = False
        self.timer = QTimer(self, interval=2000, timeout=self._poll)

    def start(self) -> None:
        self._show()
        self.timer.start()
        self._poll()

    def stop(self) -> None:
        self.timer.stop()

    def _poll(self) -> None:
        name, on_gpu = gpu.describe_device()
        if not on_gpu or self._busy:
            self._show()
            return
        self._busy = True

        def work():
            st = gpu.nvidia_stats() if "nvidia" in (gpu.in_use.get("device") or "").lower() or "rtx" in name.lower() \
                or "gtx" in name.lower() else None
            self.stats = st
            self._busy = False

        threading.Thread(target=work, daemon=True).start()
        self._show()

    def _show(self) -> None:
        name, on_gpu = gpu.describe_device()
        if not name:
            self.txt.setText("Loading the AI model…")
            _style(self.txt, f"font-size: 11px; color: {C['sec']};")
            _style(self.ring, f"border: 2px solid {C['line2']}; border-radius: 7px;")
            return
        if on_gpu:
            st = self.stats
            txt = name + (f" · {st['util']}% · {st['used']:.1f} / {st['total']:.1f} GB" if st else " · DirectML")
            self.txt.setText(txt)
            _style(self.txt, f"font-size: 11px; color: {C['sec']};")
            _style(self.ring, f"border: 2px solid {C['acc']}; border-radius: 7px;")
        else:
            self.txt.setText(name)
            _style(self.txt, f"font-size: 11px; color: {C['warn']};")
            _style(self.ring, f"border: 2px solid {C['warn']}; border-radius: 7px;")


class StatusBar(QWidget):
    openLog = Signal()

    def __init__(self, queue: JobQueue, parent=None):
        super().__init__(parent)
        self.setObjectName("statusbar")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setFixedHeight(36)
        self.q = queue
        self.identify = None                   # set by the main window
        self.id_state: dict = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 8, 0)
        lay.setSpacing(10)
        self.name = label("Ready", "h2")
        lay.addWidget(self.name)
        self.detail = ElidedLabel("Nothing running")
        self.detail.setObjectName("muted")
        lay.addWidget(self.detail, 2)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedWidth(220)
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        self.pct = label("", None)
        _style(self.pct, "font-weight: 600;")
        self.pct.setMinimumWidth(34)
        lay.addWidget(self.pct)
        self.times = ElidedLabel("")
        self.times.setObjectName("muted")
        self.times.setMinimumWidth(80)
        lay.addWidget(self.times, 3)
        self.queued = label("", "muted")
        lay.addWidget(self.queued)
        self.hw = HwChip()
        lay.addWidget(self.hw)
        self.pause_btn = button("Pause", "pause")
        self.pause_btn.setToolTip("Pause / resume")
        self.pause_btn.clicked.connect(self._pause)
        lay.addWidget(self.pause_btn)
        self.stop_btn = button("Stop", "x")
        self.stop_btn.setToolTip("Stop this and empty the queue (finished work is kept)")
        self.stop_btn.clicked.connect(self._stop)
        lay.addWidget(self.stop_btn)
        self.log_btn = button("Log", "log", "ghost")
        self.log_btn.setToolTip("Show the log (L)")
        self.log_btn.clicked.connect(self.openLog.emit)
        lay.addWidget(self.log_btn)
        # title bar pills
        self.job_pill = Pill()
        self.id_pill = Pill()
        self.job_pill.clicked.connect(self.openLog.emit)
        self.id_pill.clicked.connect(self.openLog.emit)
        self.job_pill.hide()
        self.id_pill.hide()
        self.slide = 0.0
        self.timer = QTimer(self, interval=1000, timeout=self.refresh)
        self.timer.start()
        self.anim = QTimer(self, interval=60, timeout=self._animate)
        # many updates a second are folded into one repaint
        self._soon = QTimer(self, singleShot=True, interval=80, timeout=self.refresh)
        queue.changed.connect(self.refresh)
        queue.progressed.connect(self._soon.start)
        queue.finishedJob.connect(lambda _qj: self.refresh())
        self.refresh()

    # ---- identification state (from IdentifyQueue via the hub) -------------------
    def set_identify(self, st: dict) -> None:
        self.id_state = dict(st)
        if not self._soon.isActive():
            self._soon.start()

    def _id_active(self) -> bool:
        st = self.id_state
        return bool(st.get("pending") or st.get("busy"))

    # ---- buttons ---------------------------------------------------------------------
    def _pause(self) -> None:
        qj = self.q.current
        if qj is not None and qj.pausable:
            self.q.pause(not qj.paused)
        elif qj is None and self._id_active() and self.identify is not None:
            self.identify.pause(not self.identify.paused)
            self.id_state["paused"] = self.identify.paused
        self.refresh()

    def _stop(self) -> None:
        if self.q.current is not None:
            self.q.stop_all()

    def _animate(self) -> None:
        self.job_pill.bar.update()
        self.id_pill.bar.update()

    # ---- drawing -----------------------------------------------------------------------
    def refresh(self) -> None:
        now = time.time()
        qj = self.q.current
        id_on = self._id_active()
        id_paused = bool(self.identify.paused) if self.identify is not None else False
        # identification pill (always while it runs)
        if id_on:
            st = self.id_state
            done, pend = int(st.get("done") or 0), int(st.get("pending") or 0)
            total = done + pend
            frac = done / total if total else 0.0
            self.id_pill.txt.setText(f"Identifying {done}/{total}")
            self.id_pill.pct.setText(f"{frac * 100:.0f}%")
            self.id_pill.bar.set(frac)
            self.id_pill.set_state("pause" if id_paused else "run")
            self.id_pill.setToolTip("Songs are identified in the background (Shazam allows about 18 a minute)")
        self.id_pill.setVisible(id_on)
        if qj is not None:
            el = qj.elapsed(now)
            stalled = (now - qj.last_change) > STALL_S and not qj.paused
            self.name.setText(qj.name)
            self.detail.setText(qj.detail or "Starting…")
            _style(self.detail, f"color: {C['sec']};")
            self.bar.setVisible(True)
            self.pct.setVisible(True)
            sliding = not qj.sized and qj.frac <= 0
            if sliding:
                self.bar.setRange(0, 0)
                self.pct.setText("")
            else:
                self.bar.setRange(0, 1000)
                self.bar.setValue(int(qj.frac * 1000))
                self.pct.setText(f"{qj.frac * 100:.0f}%")
            if qj.paused:
                tt, col = f"Paused · {fmt_dur(el)} elapsed", C["warn"]
            elif stalled:
                tt = (f"No progress for {fmt_dur(now - qj.last_change)} · {fmt_dur(el)} elapsed · "
                      "the log shows what's running")
                col = C["warn"]
            else:
                tt, col = (times_text(el, qj.frac) if not sliding else f"{fmt_dur(el)} elapsed · size not known yet"), C["sec"]
            self.times.setText(tt)
            _style(self.times, f"color: {col};")
            if self.bar.property("stalled") != ("true" if stalled else "false"):
                self.bar.setProperty("stalled", "true" if stalled else "false")
                self.bar.style().unpolish(self.bar)
                self.bar.style().polish(self.bar)
            n = len(self.q.waiting)
            self.queued.setText(f"{n} more queued" if n else "")
            self.queued.setVisible(bool(n))
            self.pause_btn.setVisible(qj.pausable)
            self.pause_btn.setText("Resume" if qj.paused else "Pause")
            _icon(self.pause_btn, "play" if qj.paused else "pause", C["text"], 15)
            self.stop_btn.setVisible(True)
            if qj.ai and not self.hw.isVisible():
                self.hw.setVisible(True)
                self.hw.start()
            elif not qj.ai and self.hw.isVisible():
                self.hw.setVisible(False)
                self.hw.stop()
            # pill
            self.job_pill.txt.setText(qj.name + (f" · {n} queued" if n else ""))
            self.job_pill.pct.setText("" if sliding else f"{qj.frac * 100:.0f}%")
            self.job_pill.bar.set(qj.frac, sliding)
            self.job_pill.set_state("pause" if qj.paused else "stall" if stalled else "run")
            self.job_pill.setVisible(True)
            if sliding and not self.anim.isActive():
                self.anim.start()
            elif not sliding:
                self.anim.stop()
            return
        self.job_pill.setVisible(False)
        self.anim.stop()
        self.hw.setVisible(False)
        self.hw.stop()
        self.queued.setVisible(False)
        self.stop_btn.setVisible(False)
        if id_on:
            st = self.id_state
            done, pend = int(st.get("done") or 0), int(st.get("pending") or 0)
            total = done + pend
            frac = done / total if total else 0.0
            self.name.setText("Identifying songs")
            self.detail.setText(f"{done} of {total}" + (f" · {st.get('current')}" if st.get("current") else ""))
            _style(self.detail, f"color: {C['sec']};")
            self.bar.setVisible(True)
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))
            self.pct.setVisible(True)
            self.pct.setText(f"{frac * 100:.0f}%")
            el = now - float(st.get("started") or now)
            self.times.setText(f"Paused · {done} done" if id_paused else times_text(el, frac))
            _style(self.times, f"color: {C['warn'] if id_paused else C['sec']};")
            self.pause_btn.setVisible(True)
            self.pause_btn.setText("Resume" if id_paused else "Pause")
            _icon(self.pause_btn, "play" if id_paused else "pause", C["text"], 15)
            return
        self.pause_btn.setVisible(False)
        self.bar.setVisible(False)
        self.pct.setVisible(False)
        self.name.setText("Ready")
        last = self.q.last
        if last is not None:
            self.detail.setText(last.result_text)
            col = C["danger"] if last.state == "failed" else C["sec"]
            _style(self.detail, f"color: {col};")
        else:
            self.detail.setText("Nothing running")
            _style(self.detail, f"color: {C['sec']};")
        self.times.setText("")
