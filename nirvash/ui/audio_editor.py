"""Edit a song's audio: trim the start and end, change its volume, or re-cut it from the video it came from.

Opened from a song's right-click menu, the waveform's right-click menu, or E in the library. The changes are
made by jobs/audio_edit.py from the kept original, so they can always be undone (Back to the original)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QSlider, QWidget

from ..core.db import get_db
from ..jobs import audio_edit as ae
from . import theme, workers
from .dialogs import Dialog
from .player import Player
from .theme import C
from .waveform import compute_peaks
from .widgets import Segmented, button, fmt_time, label


def fmt_precise(s: float | None) -> str:
    s = max(0.0, s or 0.0)
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:05.2f}"


def parse_time(t: str) -> float | None:
    t = t.strip().replace(",", ".")
    try:
        if ":" in t:
            m, s = t.rsplit(":", 1)
            return int(m or 0) * 60 + float(s)
        return float(t)
    except ValueError:
        return None


class RangeWave(QWidget):
    """A big waveform with a start and an end handle, the part outside them dimmed, and a playhead.

    Click to move the playhead, drag a handle to move the start or end, scroll to zoom in around the mouse
    (Shift+scroll moves along)."""

    rangeChanged = Signal(float, float)
    seekRequested = Signal(float)

    HANDLE = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(132)
        self.setMouseTracking(True)
        self.peaks: np.ndarray | None = None
        self.duration = 0.0
        self.a = 0.0
        self.b = 0.0
        self.pos = 0.0
        self.old: tuple[float, float] | None = None    # where the song was cut before (re-cut view)
        self.view = (0.0, 0.0)                          # the part of the timeline on screen (zoom)
        self._drag: str | None = None
        self.setCursor(Qt.PointingHandCursor)

    def set_audio(self, peaks: np.ndarray | None, duration: float) -> None:
        self.peaks = peaks
        self.duration = max(0.01, duration)
        self.view = (0.0, self.duration)
        self.update()

    def set_range(self, a: float, b: float, emit: bool = False) -> None:
        a = max(0.0, min(a, self.duration))
        b = max(0.0, min(b, self.duration))
        if b - a < 1.0:
            if self._drag == "a":
                a = max(0.0, b - 1.0)
            else:
                b = min(self.duration, a + 1.0)
        self.a, self.b = a, b
        self.update()
        if emit:
            self.rangeChanged.emit(a, b)

    def set_pos(self, s: float) -> None:
        self.pos = s
        self.update()

    # ---- coordinates -------------------------------------------------------------------
    def _x(self, t: float) -> float:
        v0, v1 = self.view
        return (t - v0) / max(1e-6, v1 - v0) * self.width()

    def _t(self, x: float) -> float:
        v0, v1 = self.view
        return max(0.0, min(self.duration, v0 + x / max(1, self.width()) * (v1 - v0)))

    def _hit(self, x: float) -> str | None:
        for name, t in (("a", self.a), ("b", self.b)):
            if abs(self._x(t) - x) <= self.HANDLE + 3:
                return name
        return None

    # ---- mouse ---------------------------------------------------------------------------
    def mousePressEvent(self, e):  # noqa: N802
        if e.button() != Qt.LeftButton or self.duration <= 0:
            return
        x = e.position().x()
        self._drag = self._hit(x)
        if self._drag is None:
            self._drag = "pos"
            self.pos = self._t(x)
            self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        x = e.position().x()
        if self._drag == "a":
            self.set_range(min(self._t(x), self.b - 1.0), self.b, emit=True)
        elif self._drag == "b":
            self.set_range(self.a, max(self._t(x), self.a + 1.0), emit=True)
        elif self._drag == "pos":
            self.pos = self._t(x)
            self.update()
        else:
            self.setCursor(Qt.SizeHorCursor if self._hit(x) else Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._drag == "pos":
            self.seekRequested.emit(self._t(e.position().x()))
        self._drag = None

    def wheelEvent(self, e):  # noqa: N802
        if self.duration <= 0:
            return
        v0, v1 = self.view
        span = v1 - v0
        steps = e.angleDelta().y() / 120 or e.angleDelta().x() / 120
        if e.modifiers() & Qt.ShiftModifier or not e.angleDelta().y():
            shift = -steps * span * 0.15
            v0, v1 = v0 + shift, v1 + shift
        else:
            at = self._t(e.position().x())
            k = 0.8 ** steps
            new = max(2.0, min(self.duration, span * k))
            f = (at - v0) / max(1e-6, span)
            v0 = at - f * new
            v1 = v0 + new
        if v0 < 0:
            v0, v1 = 0.0, v1 - v0
        if v1 > self.duration:
            v0, v1 = max(0.0, v0 - (v1 - self.duration)), self.duration
        self.view = (v0, v1)
        self.update()

    # ---- drawing ---------------------------------------------------------------------------
    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        ruler = 16
        top, bot = 4, h - ruler
        mid = (top + bot) / 2
        bg = QColor(C["field"])
        p.fillRect(self.rect(), bg)
        v0, v1 = self.view
        if self.old is not None:
            r = QRectF(self._x(self.old[0]), top, self._x(self.old[1]) - self._x(self.old[0]), bot - top)
            c = QColor(C["sec"])
            c.setAlpha(28)
            p.fillRect(r, c)
        if self.peaks is not None and len(self.peaks):
            n = len(self.peaks)
            bar, gap = 2.0, 1.0
            count = max(1, int(w / (bar + gap)))
            ts = v0 + (np.arange(count) + 0.5) * (bar + gap) / w * (v1 - v0)
            idx = np.clip((ts / self.duration * n).astype(int), 0, n - 1)
            inside, outside = QPainterPath(), QPainterPath()
            for i, (t, v) in enumerate(zip(ts, self.peaks[idx])):
                bh = max(2.0, float(v) * (bot - top - 6))
                rect = QRectF(i * (bar + gap), mid - bh / 2, bar, bh)
                (inside if self.a <= t <= self.b else outside).addRoundedRect(rect, 1, 1)
            g = QLinearGradient(0, 0, w, 0)
            for k, s in enumerate(theme.T["stops"]):
                g.setColorAt(k / max(1, len(theme.T["stops"]) - 1), QColor(s))
            p.fillPath(outside, QColor(C["line2"]))
            p.fillPath(inside, g)
        # dim what will be cut off
        dim = QColor(C["ground"])
        dim.setAlpha(120)
        xa, xb = self._x(self.a), self._x(self.b)
        if xa > 0:
            p.fillRect(QRectF(0, top, xa, bot - top), dim)
        if xb < w:
            p.fillRect(QRectF(xb, top, w - xb, bot - top), dim)
        # handles
        for x in (xa, xb):
            if -10 <= x <= w + 10:
                p.setPen(QPen(QColor(C["acc"]), 2))
                p.drawLine(QPointF(x, top), QPointF(x, bot))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(C["acc"]))
                p.drawRoundedRect(QRectF(x - self.HANDLE / 2, top, self.HANDLE, 18), 2, 2)
                p.drawRoundedRect(QRectF(x - self.HANDLE / 2, bot - 18, self.HANDLE, 18), 2, 2)
        # playhead
        xp = self._x(self.pos)
        if 0 <= xp <= w:
            p.setPen(QPen(QColor(C["text"]), 1.5))
            p.drawLine(QPointF(xp, top), QPointF(xp, bot))
        # time ruler
        p.setPen(QColor(C["faint"]))
        f = p.font()
        f.setPixelSize(10)
        p.setFont(f)
        span = max(0.01, v1 - v0)
        step = next((s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300) if span / s <= w / 70), 600)
        t = (int(v0 / step) + 1) * step if v0 % step else v0
        while t <= v1:
            x = self._x(t)
            p.drawLine(QPointF(x, bot + 1), QPointF(x, bot + 4))
            lx = max(0.0, min(w - 60.0, x - 30))           # labels at the edges stay inside
            p.drawText(QRectF(lx, bot + 3, 60, ruler - 3), Qt.AlignCenter, fmt_time(t))
            t += step
        p.end()


@dataclass
class EditRequest:
    mode: str                 # "song" | "video" | "reset"
    trim_start: float = 0.0
    trim_end: float | None = None
    gain_db: float = 0.0
    video: str | None = None
    start: float = 0.0        # video mode: the part of the video
    end: float = 0.0


class AudioEditDialog(Dialog):
    """Trim / volume / re-cut. exec() returns 1 with self.request set when something should be saved."""

    def __init__(self, parent, sid: int, mode: str = "song"):
        db = get_db()
        r = db.song(sid)
        name = r["title"] or Path(r["file_path"]).stem
        super().__init__(parent, f"Edit audio · {name}", "", width=900)
        self.setMaximumWidth(1100)
        self.sid = sid
        self.r = r
        self.request: EditRequest | None = None
        self._closed = False      # background measurements finishing after it closes are ignored
        self.src_path = ae.source_for_editing(sid)
        self.src_total = 0.0
        self.sources = [s for s in db.sources(sid) if Path(s["video_path"]).exists()]
        self.all_sources = db.sources(sid)

        top = QHBoxLayout()
        self.mode = Segmented([("song", "This song"), ("video", "From its video")])
        self.mode.changed.connect(self._mode_changed)
        top.addWidget(self.mode)
        self.video_box = QComboBox()
        for s in self.sources:
            self.video_box.addItem(Path(s["video_path"]).name, s["video_path"])
        self.video_box.currentIndexChanged.connect(lambda _i: self._load_video())
        top.addWidget(self.video_box, 1)
        top.addStretch(0)
        self.body.addLayout(top)
        self.hint = label("", "muted", wrap=True)
        self.body.addWidget(self.hint)

        self.wave = RangeWave()
        self.wave.rangeChanged.connect(self._range_changed)
        self.wave.seekRequested.connect(self._seek)
        self.body.addWidget(self.wave)

        # start / play / end
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        self.start_edit = QLineEdit()
        self.end_edit = QLineEdit()
        for e in (self.start_edit, self.end_edit):
            e.setFixedWidth(84)
            e.setAlignment(Qt.AlignRight)
            e.editingFinished.connect(self._typed)
        g.addWidget(label("Start", "muted"), 0, 0)
        g.addWidget(self.start_edit, 0, 1)
        sb = button("At playhead", "tri_down", "ghost")
        sb.setToolTip("Start where the playhead is (I)")
        sb.clicked.connect(lambda: self._set_at_playhead("a"))
        g.addWidget(sb, 0, 2)
        self.play_btn = button("Play", "play", "primary")
        self.play_btn.clicked.connect(self._toggle)
        g.addWidget(self.play_btn, 0, 4)
        ps = button("Hear the start", "play", "ghost")
        ps.clicked.connect(lambda: self._play_from(self.wave.a))
        g.addWidget(ps, 0, 5)
        pe = button("Hear the end", "play", "ghost")
        pe.clicked.connect(lambda: self._play_from(max(self.wave.a, self.wave.b - 4)))
        g.addWidget(pe, 0, 6)
        g.setColumnStretch(3, 1)
        g.setColumnStretch(7, 1)
        g.addWidget(label("End", "muted"), 0, 8)
        g.addWidget(self.end_edit, 0, 9)
        eb = button("At playhead", "tri_down", "ghost")
        eb.setToolTip("End where the playhead is (O)")
        eb.clicked.connect(lambda: self._set_at_playhead("b"))
        g.addWidget(eb, 0, 10)
        self.length_lbl = label("", "muted")
        g.addWidget(self.length_lbl, 1, 0, 1, 11, Qt.AlignHCenter)
        self.body.addLayout(g)

        # volume
        vol = QHBoxLayout()
        vol.setSpacing(8)
        vol.addWidget(label("Volume", "h2"))
        self.gain = QSlider(Qt.Horizontal)
        self.gain.setRange(-24, 24)          # half-dB steps: -12 .. +12 dB
        self.gain.setValue(int(round((r["gain_db"] or 0.0) * 2)))
        self.gain.setFixedWidth(260)
        self.gain.valueChanged.connect(self._gain_changed)
        vol.addWidget(self.gain)
        self.gain_lbl = label("", "mono")
        self.gain_lbl.setFixedWidth(64)
        vol.addWidget(self.gain_lbl)
        self.auto_btn = button("Even out", "wand", "ghost")
        self.auto_btn.setToolTip("Set the volume that brings this song to the same loudness as the rest")
        self.auto_btn.clicked.connect(self._auto_gain)
        self.auto_btn.setEnabled(False)
        vol.addWidget(self.auto_btn)
        self.loud_lbl = label("Measuring loudness…", "muted")
        vol.addWidget(self.loud_lbl, 1)
        self.body.addLayout(vol)
        self.note = label("", "cap", wrap=True)
        self.body.addWidget(self.note)

        self.reset_btn = self.add_button("Back to the original", "ghost", "undo")
        self.reset_btn.setToolTip("Undo every trim, volume change and re-cut made in Nirvash")
        self.reset_btn.setEnabled(ae.is_edited(r))
        self.reset_btn.clicked.connect(self._reset)
        # the reset button sits on the left, Cancel and Save on the right
        self.btns.removeWidget(self.reset_btn)
        self.btns.insertWidget(0, self.reset_btn)
        self.add_button("Cancel", "cancel")
        self.save_btn = self.add_button("Save", "primary", "check")
        self.save_btn.clicked.disconnect()
        self.save_btn.clicked.connect(self._save)

        self.player = Player(self, video=False)
        self.player.positionChanged.connect(self._player_pos)
        self.player.playingChanged.connect(self._playing)
        self._loaded: str | None = None
        self.loud: ae.Loudness | None = None
        self._loud_token = 0
        self._loud_timer = QTimer(self, singleShot=True, interval=500, timeout=self._measure)

        self.mode.set_value("video" if mode == "video" and self.sources else "song")
        if not self.sources:
            self.mode.setEnabled(False)
            self.mode.setToolTip("The video this song came from isn't there any more" if self.all_sources
                                 else "This song isn't linked to a video")
        self._mode_changed(self.mode.value)
        self._gain_changed(self.gain.value())

    # ---- loading -------------------------------------------------------------------------------
    def _mode_changed(self, mode: str) -> None:
        self.player.pause()
        self.video_box.setVisible(mode == "video" and len(self.sources) > 1)
        if mode == "video" and self.sources:
            self._load_video()
        else:
            self._load_song()
        self._update_note()

    def _load_song(self) -> None:
        r = self.r
        self.hint.setText("Drag the handles to cut off the start or the end. The original is kept, so you can "
                          "make it longer again later or go back to it.")
        path = str(self.src_path)
        total = ae.ff.probe(path).duration or (r["duration"] or 0)
        self.src_total = total
        self.wave.old = None
        self.wave.set_audio(None, total)
        a = r["trim_start"] or 0.0
        b = r["trim_end"] if r["trim_end"] is not None else total
        self.wave.set_range(a, b)
        self._load_peaks(path, f"edit-{self.sid}-{Path(path).name}-{int(total * 1000)}", total)
        self._load_player(path)
        self._show_range()

    def _load_video(self) -> None:
        if not self.sources:
            return
        vp = self.video_box.currentData() or self.sources[0]["video_path"]
        src = next(s for s in self.sources if s["video_path"] == vp)
        self.hint.setText("The whole sound of the video the song came from. Drag the handles to where the song "
                          "should start and end; the light band is where it's cut now. Saving takes that part of "
                          "the video as the song (the audio is copied as it is, like a scan does).")
        total = ae.ff.probe(vp).duration or 0
        self.src_total = total
        # where the song sits in the video now, including any trim
        a = (src["start"] or 0.0) + (self.r["trim_start"] or 0.0)
        b = (src["start"] or 0.0) + (self.r["trim_end"] if self.r["trim_end"] is not None
                                     else (src["end"] or total) - (src["start"] or 0.0))
        self.wave.set_audio(None, total)
        self.wave.old = (src["start"] or 0.0, src["end"] or total)
        self.wave.set_range(a, min(b, total))
        self._load_peaks(vp, f"editv-{abs(hash(vp))}-{int(total * 1000)}", total)
        self._load_player(vp)
        self._show_range()

    def _load_peaks(self, path: str, key: str, total: float) -> None:
        n = int(min(20000, max(1500, total * 25)))      # about 25 bars a second, so zooming in still shows detail
        tok = (path, key)
        self._peaks_for = tok
        workers.run_bg(compute_peaks, path, key, n,
                       done=lambda peaks, t=tok: self.wave.set_audio(peaks, self.src_total)
                       if not self._closed and self._peaks_for == t else None)

    def _load_player(self, path: str) -> None:
        if self._loaded != path:
            self.player.load(path, play=False)
            self._loaded = path
        self.player.set_gain_db(self.gain.value() / 2)

    # ---- range -------------------------------------------------------------------------------
    def _range_changed(self, _a: float, _b: float) -> None:
        self._show_range()
        self._loud_timer.start()

    def _show_range(self) -> None:
        a, b = self.wave.a, self.wave.b
        self.start_edit.setText(fmt_precise(a))
        self.end_edit.setText(fmt_precise(b))
        cut = a + max(0.0, self.src_total - b)
        self.length_lbl.setText(f"Length {fmt_precise(b - a)}" + (f"  ·  {fmt_precise(cut)} cut off" if cut > 0.05 else ""))
        self._loud_timer.start()

    def _typed(self) -> None:
        a = parse_time(self.start_edit.text())
        b = parse_time(self.end_edit.text())
        self.wave.set_range(self.wave.a if a is None else a, self.wave.b if b is None else b, emit=True)
        self._show_range()

    def _set_at_playhead(self, which: str) -> None:
        t = self.wave.pos
        if which == "a":
            self.wave.set_range(min(t, self.wave.b - 1.0), self.wave.b, emit=True)
        else:
            self.wave.set_range(self.wave.a, max(t, self.wave.a + 1.0), emit=True)

    # ---- playback -----------------------------------------------------------------------------
    def _seek(self, t: float) -> None:
        self.player.seek(t)

    def _play_from(self, t: float) -> None:
        self.wave.set_pos(t)
        self.player.seek(t)
        self.player.play()

    def _toggle(self) -> None:
        if self.player.is_playing():
            self.player.pause()
        else:
            if not (self.wave.a <= self.wave.pos < self.wave.b - 0.1):
                self._play_from(self.wave.a)
            else:
                self.player.seek(self.wave.pos)
                self.player.play()

    def _player_pos(self, t: float) -> None:
        self.wave.set_pos(t)
        if t >= self.wave.b and self.player.is_playing():      # stop at the end handle: hear exactly the cut
            self.player.pause()

    def _playing(self, on: bool) -> None:
        self.play_btn.setText("Pause" if on else "Play")
        self.play_btn.setIcon(theme.icon("pause" if on else "play", C["on_acc"], 15))

    # ---- volume ---------------------------------------------------------------------------------
    def _gain_changed(self, v: int) -> None:
        db = v / 2
        self.gain_lbl.setText(f"{db:+.1f} dB" if db else "0 dB")
        self.player.set_gain_db(db)
        self._update_note()
        self._show_loud()

    def _measure(self) -> None:
        self._loud_token += 1
        tok = self._loud_token
        path = self._loaded
        a, b = self.wave.a, self.wave.b
        if path is None:
            return
        self.loud_lbl.setText("Measuring loudness…")
        workers.run_bg(ae.measure, path, a, b, done=lambda l, t=tok: self._measured(t, l))

    def _measured(self, tok: int, l) -> None:
        if self._closed or tok != self._loud_token:
            return
        self.loud = l
        self.auto_btn.setEnabled(l is not None)
        self._show_loud()

    def _show_loud(self) -> None:
        l = self.loud
        if l is None:
            if self._loud_token:
                self.loud_lbl.setText("Couldn't measure the loudness (silent?)")
            return
        from ..core.settings import get_settings
        target = float(get_settings().get("level.target", -14.0))
        g = self.gain.value() / 2
        self.loud_lbl.setText(f"{l.lufs + g:.1f} LUFS with this volume · the library aims for {target:.0f} · "
                              f"peak {l.peak + g:+.1f} dB" + ("  (will distort)" if l.peak + g > 0 else ""))
        self.loud_lbl.setObjectName("warnText" if l.peak + g > 0 else "muted")
        self.loud_lbl.style().unpolish(self.loud_lbl)
        self.loud_lbl.style().polish(self.loud_lbl)

    def _auto_gain(self) -> None:
        self.gain.setValue(int(round(ae.level_gain(self.loud) * 2)))

    def _update_note(self) -> None:
        g = self.gain.value() / 2
        video = self.mode.value == "video"
        if abs(g) < 0.05:
            how = "The audio is copied exactly as it is, so nothing is lost."
        else:
            how = ("Changing the volume re-encodes the song once, from the kept original, at high quality in the "
                   "same format, so it plays at this volume in any player.")
        self.note.setText(("Saving takes the chosen part of the video as the song. " if video else "") + how +
                          " Back to the original undoes it any time.")

    # ---- results ------------------------------------------------------------------------------
    def _save(self) -> None:
        g = self.gain.value() / 2
        if self.mode.value == "video" and self.sources:
            vp = self.video_box.currentData() or self.sources[0]["video_path"]
            self.request = EditRequest("video", gain_db=g, video=vp, start=self.wave.a, end=self.wave.b)
        else:
            end = None if self.wave.b >= self.src_total - 0.02 else self.wave.b
            self.request = EditRequest("song", trim_start=self.wave.a if self.wave.a > 0.02 else 0.0, trim_end=end,
                                       gain_db=g)
        self.accept()

    def _reset(self) -> None:
        self.request = EditRequest("reset")
        self.accept()

    def keyPressEvent(self, e):  # noqa: N802
        k = e.key()
        if isinstance(self.focusWidget(), QLineEdit) and k not in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            return super().keyPressEvent(e)
        if k == Qt.Key_Space:
            self._toggle()
        elif k == Qt.Key_I:
            self._set_at_playhead("a")
        elif k == Qt.Key_O:
            self._set_at_playhead("b")
        else:
            super().keyPressEvent(e)

    def done(self, r: int) -> None:  # noqa: D401 - stop playback whichever way it closes
        self._closed = True
        try:
            self.player.pause()
            self.player.unload()
            self.player.shutdown()
        except Exception:
            pass
        super().done(r)

    def exec(self) -> int:
        import os
        if os.environ.get("NIRVASH_AUTO_DIALOGS") == "accept":   # tests: save what's set
            self._save()
            return 1
        return super().exec()


_ = QLabel
