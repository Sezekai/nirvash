"""Video/audio playback: mpv (bundled libmpv) with a Qt Multimedia fallback."""
from __future__ import annotations

import os
import time
import sys

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtWidgets import QStackedLayout, QWidget

from ..core import log as _log
from ..core.paths import app_dir, bundle_dir

log = _log.get("player")

_mpv_mod = None
_mpv_error = ""


def _load_mpv():
    global _mpv_mod, _mpv_error
    if _mpv_mod is not None or _mpv_error:
        return _mpv_mod
    for d in (bundle_dir() / "bin", app_dir() / "bin"):
        if d.exists():
            from ..media.ff import set_env
            set_env("PATH", str(d) + os.pathsep + os.environ.get("PATH", ""))
            if sys.platform == "win32":
                try:
                    os.add_dll_directory(str(d))
                except Exception:
                    pass
    try:
        import mpv  # type: ignore
        _mpv_mod = mpv
    except Exception as e:
        _mpv_error = str(e)
        log.info("mpv not available, using Qt playback: %s", e)
    return _mpv_mod


class _Bridge(QObject):
    pos = Signal(float)
    dur = Signal(float)
    pause = Signal(bool)
    eof = Signal()
    loaded = Signal()


class Player(QWidget):
    """Plays a video (optionally with a separate audio file) or an audio file on its own."""

    positionChanged = Signal(float)
    durationChanged = Signal(float)
    playingChanged = Signal(bool)
    ended = Signal()

    def __init__(self, parent=None, video: bool = True):
        super().__init__(parent)
        self.video_enabled = video
        if video:
            self.setObjectName("stage")
            self.setAttribute(Qt.WA_StyledBackground)
        self._b = _Bridge()
        self._b.pos.connect(self._on_pos)
        self._b.dur.connect(self._on_dur)
        self._b.pause.connect(self._on_pause)
        self._paused = True
        self._b.eof.connect(self.ended.emit)
        self._b.loaded.connect(self._on_loaded)
        self._pos = 0.0
        self._last_pos_emit = 0.0
        self._last_pos_val = 0.0
        self._dur = 0.0
        self._volume = 80
        self._want_play = False
        self._mpv = None
        self._qt = None
        self._wid = None
        from PySide6.QtGui import QGuiApplication
        headless = bool(os.environ.get("NIRVASH_NO_PLAYBACK")) or QGuiApplication.platformName() == "offscreen"
        self._use_mpv = not headless and _load_mpv() is not None
        if self.video_enabled and self._use_mpv:
            # mpv draws into this widget's own window; it's created on first use, once the widget sits in its
            # final place (moving a native widget to a new parent replaces its window).
            self.setAttribute(Qt.WA_DontCreateNativeAncestors)
            self.setAttribute(Qt.WA_NativeWindow)
        if not self._use_mpv:
            self._init_qt()
        if not video:
            self.setFixedSize(0, 0)

    def _m(self):
        """The mpv instance, created (or re-created if our window changed) on demand."""
        if not self._use_mpv:
            return None
        wid = int(self.winId()) if self.video_enabled else None
        if self._mpv is not None and wid == self._wid:
            return self._mpv
        if self._mpv is not None:
            try:
                self._mpv.terminate()
            except Exception:
                pass
            self._mpv = None
        try:
            self._init_mpv(_load_mpv(), wid)
            self._wid = wid
        except Exception as e:
            log.warning("mpv failed to start (%s); using Qt playback", e)
            self._use_mpv = False
            self._mpv = None
            self._init_qt()
        return self._mpv

    # ---- mpv -------------------------------------------------------------
    def _init_mpv(self, mpv, wid: int | None) -> None:
        opts = dict(keep_open="yes", idle="yes", input_default_bindings=False, input_vo_keyboard=False, osc=False,
                    loglevel="warn", volume=self._volume, audio_display="no", hr_seek="yes")
        if self.video_enabled:
            opts.update(wid=str(wid), vo="gpu", hwdec="auto-safe", keepaspect="yes",
                        background="color", background_color="#05060a")
        else:
            opts.update(vid="no", force_window="no", vo="null")
        # No Lua scripts: Nirvash draws its own controls, and mpv's built-in scripts (on-screen controller, stats,
        # console, ...) run LuaJIT, which raises internal exceptions constantly. Older libmpv builds may not know
        # every switch, so unknown ones are dropped.
        extra = dict(audio_fallback_to_null=True, load_scripts=False, ytdl=False, config=False, terminal=False,
                     input_terminal=False,
                     load_stats_overlay=False, load_osd_console=False, load_auto_profiles=False,
                     load_select=False, load_commands=False, load_positioning=False, load_context_menu=False)
        if os.environ.get("NIRVASH_MPV_SCRIPTS"):   # test switch: the old behaviour
            extra = {}
        m = None
        for _ in range(len(extra) + 1):
            try:
                m = mpv.MPV(**opts, **extra)
                break
            except Exception as e:
                msg = str(e).replace("-", "_")
                bad = [k for k in extra if k in msg]
                if not bad:
                    log.warning("mpv rejected the script switches (%s); starting without them", e)
                    extra = {}
                    continue
                for k in bad:
                    extra.pop(k)
        if m is None:
            m = mpv.MPV(**opts)
        m.observe_property("time-pos", lambda _n, v: self._b.pos.emit(float(v or 0.0)))
        m.observe_property("duration", lambda _n, v: self._b.dur.emit(float(v or 0.0)))
        m.observe_property("pause", lambda _n, v: self._b.pause.emit(bool(v)))
        m.observe_property("eof-reached", lambda _n, v: self._b.eof.emit() if v else None)

        @m.event_callback("file-loaded")
        def _loaded(_ev):
            self._b.loaded.emit()

        self._mpv = m

    # ---- Qt fallback -------------------------------------------------------
    def _init_qt(self) -> None:
        from PySide6.QtGui import QGuiApplication
        if os.environ.get("NIRVASH_NO_PLAYBACK") or QGuiApplication.platformName() == "offscreen":
            self._qt = None  # no screen (tests): no media at all
            return
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except ImportError as e:
            log.error("no playback available: mpv failed and Qt Multimedia isn't included (%s)", e)
            self._qt = None
            return

        lay = QStackedLayout(self)
        self._qt = QMediaPlayer(self)
        self._qt_out = QAudioOutput(self)
        self._qt.setAudioOutput(self._qt_out)
        self._qt_ext = QMediaPlayer(self)
        self._qt_ext_out = QAudioOutput(self)
        self._qt_ext.setAudioOutput(self._qt_ext_out)
        self._has_ext = False
        if self.video_enabled:
            from PySide6.QtMultimediaWidgets import QVideoWidget
            vw = QVideoWidget(self)
            lay.addWidget(vw)
            self._qt.setVideoOutput(vw)
        self._qt.positionChanged.connect(lambda ms: self._b.pos.emit(ms / 1000.0))
        self._qt.durationChanged.connect(lambda ms: self._b.dur.emit(ms / 1000.0))
        self._qt.playbackStateChanged.connect(
            lambda st: self._b.pause.emit(st != QMediaPlayer.PlaybackState.PlayingState))
        self._qt.mediaStatusChanged.connect(
            lambda st: self._b.eof.emit() if st == QMediaPlayer.MediaStatus.EndOfMedia else None)
        self.set_volume(self._volume)

    # ---- API ---------------------------------------------------------------
    @property
    def backend(self) -> str:
        return "mpv" if self._use_mpv else "qt"

    def load(self, path: str, audio_file: str | None = None, play: bool = False, start: float = 0.0) -> None:
        if not self._use_mpv and self._qt is None:
            return
        self._want_play = play
        self._start = start
        self._pos = 0.0
        m = self._m()
        if m is not None:
            m["audio-files"] = [audio_file] if audio_file else []
            m["pause"] = not play
            m["start"] = f"{start:.3f}" if start else "none"
            m.play(path)
        else:
            self._qt.setSource(QUrl.fromLocalFile(path))
            self._has_ext = bool(audio_file)
            self._qt_out.setMuted(bool(audio_file))
            self._qt_ext.setSource(QUrl.fromLocalFile(audio_file) if audio_file else QUrl())
            if start:
                self.seek(start)
            if play:
                self.play()

    def _on_loaded(self) -> None:
        m = self._mpv
        if m is None:
            return
        try:
            tracks = m["track-list"] or []
            ext = [t for t in tracks if t.get("type") == "audio" and t.get("external")]
            if ext:
                m["aid"] = ext[-1]["id"]
        except Exception:
            pass

    def unload(self) -> None:
        if self._mpv is None and self._qt is None:
            return
        if self._mpv is not None:
            self._mpv.command("stop")
        elif self._qt is not None:
            self._qt.stop()
            self._qt.setSource(QUrl())
            self._qt_ext.stop()
            self._qt_ext.setSource(QUrl())

    def play(self) -> None:
        if self._mpv is None and self._qt is None:
            return
        if self._mpv is not None:
            self._mpv["pause"] = False
        else:
            self._qt.play()
            if self._has_ext:
                self._qt_ext.setPosition(self._qt.position())
                self._qt_ext.play()

    def pause(self) -> None:
        if self._mpv is None and self._qt is None:
            return
        if self._mpv is not None:
            self._mpv["pause"] = True
        else:
            self._qt.pause()
            self._qt_ext.pause()

    def toggle(self) -> None:
        if self.is_playing():
            self.pause()
        else:
            if self._dur and self._pos >= self._dur - 0.05:
                self.seek(0)
            self.play()

    def is_playing(self) -> bool:
        if self._mpv is not None:
            return not self._paused
        if self._qt is None:
            return False
        from PySide6.QtMultimedia import QMediaPlayer
        return self._qt.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def seek(self, seconds: float) -> None:
        if self._mpv is None and self._qt is None:
            return
        seconds = max(0.0, seconds)
        if self._mpv is not None:
            try:
                self._mpv.command("seek", f"{seconds:.3f}", "absolute+exact")
            except Exception:
                pass
        else:
            self._qt.setPosition(int(seconds * 1000))
            if self._has_ext:
                self._qt_ext.setPosition(int(seconds * 1000))

    def set_volume(self, v: int) -> None:
        self._volume = int(v)
        if self._mpv is not None:
            self._mpv["volume"] = self._volume
        elif self._qt is not None:
            self._qt_out.setVolume(self._volume / 100)
            self._qt_ext_out.setVolume(self._volume / 100)

    def set_gain_db(self, db: float) -> None:
        """Preview a volume change (the audio editor): mpv applies it as a filter; Qt playback scales the volume."""
        self._gain_db = float(db)
        if self._mpv is not None:
            try:
                self._mpv["af"] = f"volume={db:.2f}dB" if abs(db) >= 0.05 else ""
            except Exception:
                pass
        elif self._qt is not None:
            v = min(1.0, self._volume / 100 * (10 ** (db / 20)))
            self._qt_out.setVolume(v)

    def position(self) -> float:
        return self._pos

    def duration(self) -> float:
        return self._dur

    def _on_pos(self, v: float) -> None:
        # mpv reports the position many times a second for audio; passing on about 25 a second is plenty for the
        # playhead and keeps the rest of the window responsive while a song plays.
        self._pos = v
        now = time.monotonic()
        if now - self._last_pos_emit >= 0.04 or abs(v - self._last_pos_val) > 1.0:
            self._last_pos_emit, self._last_pos_val = now, v
            self.positionChanged.emit(v)

    def _on_pause(self, paused: bool) -> None:
        self._paused = paused
        self.playingChanged.emit(not paused)

    def _on_dur(self, v: float) -> None:
        self._dur = v
        self.durationChanged.emit(v)

    def shutdown(self) -> None:
        if self._mpv is not None:
            try:
                self._mpv.terminate()
            except Exception:
                pass
            self._mpv = None

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)
