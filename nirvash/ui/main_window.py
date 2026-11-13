"""Main window: our own title bar, the rail, the pages, the log drawer, the status bar, toasts and the keys."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QEvent, QObject, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QColor, QKeySequence, QLinearGradient, QPainter, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QStackedWidget, QTextEdit,
                               QToolButton, QVBoxLayout, QWidget)

from .. import APP_NAME, VERSION
from ..analysis import models
from ..core import log as _log
from ..core import safemode
from ..core.settings import get_settings
from ..jobs import storage
from ..jobs.identify import IdentifyQueue
from . import dialogs, frameless, theme
from .automusic_page import AutoMusicPage
from .drawer import Drawer, undo
from .hub import hub
from .jobqueue import get_queue
from .keys_page import KeysPopup
from .library_page import LibraryPage
from .look_page import LookPage
from .scan_page import ScanPage
from .settings_page import SettingsPage
from .statusbar import StatusBar
from .theme import C, icon
from .widgets import button, label, page_header, panel


class EditorPlaceholder(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        l = QVBoxLayout(self)
        l.setContentsMargins(12, 10, 12, 12)
        l.setSpacing(8)
        head, _ = page_header("Editor", "Trims, joins, moving crop, zoom, rotate, blur, overlays, colour and audio")
        l.addLayout(head)
        p = panel()
        pl = QVBoxLayout(p)
        pl.setAlignment(Qt.AlignCenter)
        ic = QLabel()
        ic.setPixmap(theme.pixmap("cut", 28, C["faint"]))
        ic.setAlignment(Qt.AlignCenter)
        pl.addWidget(ic)
        t = label("The editor arrives in Stage 2.", "muted")
        t.setAlignment(Qt.AlignCenter)
        pl.addWidget(t)
        l.addWidget(p, 1)


class Backdrop(QWidget):
    """The window background: the ground colour and, with the starfield scene, the stars behind the panels."""

    def __init__(self):
        super().__init__()
        self.setObjectName("backdrop")
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setClipRect(e.rect())
        theme.paint_backdrop(p, self.rect(), self.devicePixelRatioF())


class Toast(QFrame):
    """Bottom right, 4-7 s: an icon in the state colour and a short message."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("dialog")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(9)
        self.ic = QLabel()
        lay.addWidget(self.ic, 0, Qt.AlignTop)
        self.txt = QLabel()
        self.txt.setWordWrap(True)
        self.txt.setMaximumWidth(420)
        lay.addWidget(self.txt)
        sh = QGraphicsDropShadowEffect(self)
        sh.setBlurRadius(24)
        sh.setOffset(0, 6)
        sh.setColor(QColor(0, 0, 0, 140))
        self.setGraphicsEffect(sh)
        self.hide()
        self.timer = QTimer(self, singleShot=True, timeout=self.hide)

    def show_msg(self, text: str, kind: str = "info") -> None:
        col = {"ok": C["ok"], "warn": C["warn"], "error": C["danger"]}.get(kind, C["acc"])
        name = {"ok": "check", "warn": "warn", "error": "warn"}.get(kind, "info")
        self.ic.setPixmap(theme.pixmap(name, 16, col))
        self.txt.setText(text)
        self.adjustSize()
        self.place()
        self.raise_()
        self.show()
        self.timer.start(4500 if kind in ("info", "ok") else 7000)

    def place(self) -> None:
        p = self.parentWidget()
        self.move(p.width() - self.width() - 16, p.height() - self.height() - 48)

    def mousePressEvent(self, e):
        self.hide()


class TitleBar(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.setObjectName("titlebar")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setFixedHeight(32)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.setSpacing(8)
        name = QLabel(APP_NAME)
        name.setStyleSheet("font-size: 12px; font-weight: 600;")
        lay.addWidget(name)
        self.screen = label("", "faint")
        self.screen.setStyleSheet("font-size: 12px;")
        lay.addWidget(self.screen)
        lay.addStretch(1)
        self.pills = QHBoxLayout()
        self.pills.setSpacing(6)
        lay.addLayout(self.pills)
        lay.addSpacing(8)
        self.btns = []
        for name_, tip, fn in (("win_min", "Minimise", win.showMinimized), ("win_max", "Maximise", self._max),
                               ("win_close", "Close", win.close)):
            b = QToolButton()
            b.setObjectName("winClose" if name_ == "win_close" else "winBtn")
            b.setIcon(icon(name_, C["sec"], 14))
            b.setIconSize(QSize(14, 14))
            b.setFixedSize(46, 32)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            lay.addWidget(b)
            self.btns.append(b)
        self.native = False

    def _max(self) -> None:
        self.win.showNormal() if self.win.isMaximized() else self.win.showMaximized()

    def sync(self) -> None:
        self.btns[1].setIcon(icon("win_restore" if self.win.isMaximized() else "win_max", C["sec"], 14))
        self.btns[1].setToolTip("Restore" if self.win.isMaximized() else "Maximise")
        for b, n in zip(self.btns, ("win_min", None, "win_close")):
            if n:
                b.setIcon(icon(n, C["sec"], 14))

    # Dragging where Windows' own handling isn't in use (other systems)
    def mousePressEvent(self, e):
        if not self.native and e.button() == Qt.LeftButton and self.win.windowHandle():
            self.win.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, e):
        if not self.native:
            self._max()


class Rail(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("rail")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setFixedWidth(64)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 8)
        lay.setSpacing(2)
        self.logo = QLabel()
        self.logo.setFixedSize(36, 36)
        self.logo.setToolTip(f"{APP_NAME} {VERSION}")
        lay.addWidget(self.logo, 0, Qt.AlignHCenter)
        lay.addSpacing(8)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.btns: dict[str, QToolButton] = {}
        self.icons: dict[str, str] = {}
        for key, text, ic in win.PAGES:
            lay.addWidget(self._btn(key, text, ic), 0, Qt.AlignHCenter)
        lay.addStretch(1)
        for key, text, ic in win.BOTTOM:
            lay.addWidget(self._btn(key, text, ic, page=key not in ("log", "keys")), 0, Qt.AlignHCenter)
        self.restyle()

    def _btn(self, key: str, text: str, ic: str, page: bool = True) -> QToolButton:
        b = QToolButton()
        b.setObjectName("railBtn")
        b.setCheckable(True)
        b.setText(text)
        b.setIconSize(QSize(19, 19))
        b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        b.setFixedSize(60, 48)
        b.setCursor(Qt.PointingHandCursor)
        if page:
            self.group.addButton(b)
            b.clicked.connect(lambda _=False, k=key: self.win.go(k))
        elif key == "keys":
            b.clicked.connect(lambda _=False: self.win.toggle_keys())
        else:
            b.clicked.connect(lambda on: self.win.toggle_log(on))
        self.btns[key] = b
        self.icons[key] = ic
        return b

    def restyle(self) -> None:
        for k, b in self.btns.items():
            b.setIcon(icon(self.icons[k], C["sec"], 19, checked_color=C["acc"]))
        pm = theme.pixmap("sparkle", 18, C["on_acc"])
        from PySide6.QtGui import QPixmap
        dpr = 2.0
        tile = QPixmap(int(36 * dpr), int(36 * dpr))
        tile.setDevicePixelRatio(dpr)
        tile.fill(Qt.transparent)
        p = QPainter(tile)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, 36, 36)
        st = theme.T["stops"]
        for i, s in enumerate(st):
            g.setColorAt(i / max(1, len(st) - 1), QColor(s))
        p.setBrush(g)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, 36, 36), 9, 9)
        p.drawPixmap(9, 9, pm)
        p.end()
        self.logo.setPixmap(tile)


class _GlowFilter(QObject):
    """Adds the soft accent glow to main buttons when the Glow button style is on."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Show and isinstance(obj, QPushButton) and obj.objectName() == "primary":
            apply_glow(obj)
        return False


def apply_glow(b: QPushButton) -> None:
    if theme.T.get("glow"):
        eff = b.graphicsEffect()
        if not isinstance(eff, QGraphicsDropShadowEffect):
            eff = QGraphicsDropShadowEffect(b)
            b.setGraphicsEffect(eff)
        c = QColor(C["acc"])
        c.setAlpha(150)
        eff.setColor(c)
        eff.setBlurRadius(18)
        eff.setOffset(0, 3)
    elif isinstance(b.graphicsEffect(), QGraphicsDropShadowEffect):
        b.setGraphicsEffect(None)


class MainWindow(QMainWindow):
    PAGES = [("library", "Library", "library"), ("scan", "Find music", "scan"), ("auto", "Auto-music", "wand"),
             ("editor", "Editor", "cut")]
    BOTTOM = [("look", "Look", "look"), ("settings", "Settings", "gear"), ("keys", "Keys", "keys"), ("log", "Log", "log")]
    TITLES = {"library": "Song library", "scan": "Find music", "auto": "Auto-music", "editor": "Editor",
              "look": "Appearance", "settings": "Settings", "keys": "Keys"}

    def __init__(self):
        super().__init__()
        self.s = get_settings()
        self.setWindowTitle(APP_NAME)
        self.resize(1480, 900)
        # 1100 × 680 is the smallest the screens are designed for, but never bigger than the screen itself
        scr = QGuiApplication.primaryScreen()
        av = scr.availableGeometry() if scr else None
        self.setMinimumSize(min(1100, av.width()) if av else 1100, min(680, av.height()) if av else 680)
        self.native_frame = frameless.enable(self)
        geo = self.s.get("ui.geometry")
        if geo:
            try:
                self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
            except Exception:
                pass
        self.queue = get_queue()

        central = Backdrop()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar(self)
        self.title_bar.native = self.native_frame
        outer.addWidget(self.title_bar)
        mid = QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)
        self.rail = Rail(self)
        mid.addWidget(self.rail)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(12, 6, 8, 6)
        bl.setSpacing(10)
        self.banner_ic = QLabel()
        bl.addWidget(self.banner_ic)
        self.banner_txt = label("", wrap=True)
        bl.addWidget(self.banner_txt, 1)
        self.banner_btns = QHBoxLayout()
        bl.addLayout(self.banner_btns)
        self.banner.hide()
        bw = QWidget()
        bw.setObjectName("transparent")
        bwl = QVBoxLayout(bw)
        bwl.setContentsMargins(12, 10, 12, 0)
        bwl.addWidget(self.banner)
        self.banner_wrap = bw
        bw.hide()
        right.addWidget(bw)
        self.stack = QStackedWidget()
        self.library = LibraryPage()
        self.scan_page = ScanPage()
        self.auto_page = AutoMusicPage()
        self.editor_page = EditorPlaceholder()
        self.look_page = LookPage()
        self.settings_page = SettingsPage()
        self.pages = {"library": self.library, "scan": self.scan_page, "auto": self.auto_page,
                      "editor": self.editor_page, "look": self.look_page, "settings": self.settings_page}
        for p in self.pages.values():
            self.stack.addWidget(p)
        right.addWidget(self.stack, 1)
        self.drawer = Drawer()
        self.drawer.closed.connect(lambda: self.toggle_log(False))
        self.drawer.setVisible(False)
        right.addWidget(self.drawer)
        mid.addLayout(right, 1)
        outer.addLayout(mid, 1)
        self.status = StatusBar(self.queue)
        self.status.openLog.connect(lambda: self.toggle_log(True))
        sb_row = QHBoxLayout()
        sb_row.setContentsMargins(0, 0, 0, 0)
        sb_row.setSpacing(0)
        sb_row.addWidget(self.status)
        outer.addLayout(sb_row)
        self.title_bar.pills.addWidget(self.status.job_pill)
        self.title_bar.pills.addWidget(self.status.id_pill)
        for pl in (self.status.job_pill, self.status.id_pill):
            pl.setProperty("interactive", True)

        self.toast = Toast(central)
        hub.toast.connect(self.toast.show_msg)
        hub.logLine.connect(self._log_line)
        _log.add_listener(lambda lvl, msg: hub.logLine.emit(lvl, msg))
        hub.identifyStatus.connect(self.status.set_identify)
        hub.themeChanged.connect(self._theme_changed)
        # Glow on main buttons needs to see every button as it appears, which means watching every event in the
        # app; that only runs while the Glow style is chosen (it made everything slower when always on).
        self._glow = _GlowFilter(self)
        self._glow_on = False
        self._sync_glow_filter()

        self.identify = IdentifyQueue(on_change=lambda sid: hub.songChanged.emit(sid),
                                      on_status=lambda st: hub.identifyStatus.emit(st))
        self.status.identify = self.identify
        self._hidden_panels = False
        self.rail.setVisible(not self.s.get("ui.rail_hidden", False))
        self.keys_popup = KeysPopup(self.centralWidget())
        self.keys_popup.closed.connect(lambda: self.rail.btns["keys"].setChecked(False))
        self._keys()
        self.go("library")
        if self.s.get("ui.log_open", False):
            self.toggle_log(True)
        QTimer.singleShot(400, self._check_banners)
        QTimer.singleShot(900, self._check_models)
        QTimer.singleShot(2500, self._ask_level)
        self.queue.finishedJob.connect(self._job_finished)

    # ---- window frame -------------------------------------------------------------
    def nativeEvent(self, event_type, message):
        r = frameless.native_event(self, event_type, message, self.title_bar)
        if r is not None:
            return r
        return super().nativeEvent(event_type, message)

    def showEvent(self, e):
        super().showEvent(e)
        if self.native_frame and not getattr(self, "_styled", False):
            self._styled = True
            frameless.after_show(self)

    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange:
            self.title_bar.sync()
            self.title_bar.setVisible(not self.isFullScreen())
        super().changeEvent(e)

    # ---- navigation -------------------------------------------------------------------
    def go(self, key: str) -> None:
        if key not in self.pages:
            return
        cur = self.stack.currentWidget()
        if cur is not self.pages[key] and hasattr(cur, "stop"):
            cur.stop()
        self.stack.setCurrentWidget(self.pages[key])
        if key in self.rail.btns:
            self.rail.btns[key].setChecked(True)
        self.title_bar.screen.setText(f"· {self.TITLES.get(key, key)}")

    def current_page(self):
        return self.stack.currentWidget()

    def toggle_log(self, on: bool | None = None, tab: str | None = None) -> None:
        on = (not self.drawer.isVisible()) if on is None else on
        self.drawer.setVisible(on)
        if tab:
            self.drawer.show_tab(tab)
        self.rail.btns["log"].setChecked(on)
        self.s.set("ui.log_open", on)

    def _log_line(self, level: str, msg: str) -> None:
        self.drawer.append(msg)
        if level in ("ERROR", "CRITICAL") and not self.drawer.isVisible():
            self.toggle_log(True, "log")

    # ---- keys --------------------------------------------------------------------------
    def _keys(self) -> None:
        def sc(seq, fn, always: bool = False):
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.WindowShortcut)
            # while the shortcuts popup is open, other keys do nothing (DESIGN.md)
            s.activated.connect(lambda: fn() if always or not self.keys_popup.isVisible() else None)
            return s

        sc("K", self.toggle_keys, always=True)
        sc("/", self._search)
        sc("Ctrl+F", self._search)
        sc("F", self.toggle_full)
        sc("F11", self.toggle_full)
        sc("H", self.toggle_panels)
        sc("L", lambda: self.toggle_log())
        sc("M", self.toggle_rail)
        sc("S", lambda: self.go("settings"))
        sc("Ctrl+Z", lambda: undo(None))
        sc("Esc", self._escape, always=True)
        for seq, name in (("Space", "play_pause"), ("Left", "prev"), ("Right", "next"), ("PgUp", "prev"),
                          ("PgDown", "next"), ("Home", "first"), ("End", "last"),
                          ("-", "volume_down"), ("=", "volume_up"), ("+", "volume_up"), ("0", "mute")):
            sc(seq, lambda n=name: self._page_key(n))

    def _page_key(self, name: str) -> None:
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QPlainTextEdit, QTextEdit)) and name != "play_pause":
            return
        p = self.current_page()
        fn = getattr(p, "key_" + name, None)
        if fn:
            fn()

    def _search(self) -> None:
        p = self.current_page()
        if hasattr(p, "focus_search"):
            p.focus_search()
        else:
            self.go("library")
            self.library.focus_search()

    def toggle_full(self) -> None:
        if self.isFullScreen():
            self.showMaximized() if getattr(self, "_was_max", False) else self.showNormal()
        else:
            self._was_max = self.isMaximized()
            self.showFullScreen()

    def toggle_panels(self) -> None:
        self._hidden_panels = not self._hidden_panels
        hide = self._hidden_panels
        self.rail.setVisible(not hide and not self.s.get("ui.rail_hidden", False))
        for p in self.pages.values():
            if hasattr(p, "set_panels_hidden"):
                p.set_panels_hidden(hide)
        if hide and self.drawer.isVisible():
            self.drawer.setVisible(False)

    def toggle_rail(self) -> None:
        on = not self.rail.isVisible()
        self.rail.setVisible(on)
        self.s.set("ui.rail_hidden", not on)

    def toggle_keys(self) -> None:
        self.keys_popup.toggle()
        self.rail.btns["keys"].setChecked(self.keys_popup.isVisible())

    def _escape(self) -> None:
        if self.keys_popup.isVisible():
            self.toggle_keys()
        elif self._hidden_panels:
            self.toggle_panels()
        elif self.isFullScreen():
            self.toggle_full()
        elif self.drawer.isVisible():
            self.toggle_log(False)

    # ---- theme ---------------------------------------------------------------------------
    def _theme_changed(self) -> None:
        self.rail.restyle()
        self.title_bar.sync()
        for p in self.pages.values():
            if hasattr(p, "theme_changed"):
                p.theme_changed()
        for b in self.findChildren(QPushButton, "primary"):
            apply_glow(b)
        self._sync_glow_filter()
        self.status.refresh()
        self.centralWidget().update()

    def _sync_glow_filter(self) -> None:
        on = bool(theme.T.get("glow"))
        if on != self._glow_on:
            app = QApplication.instance()
            (app.installEventFilter if on else app.removeEventFilter)(self._glow)
            self._glow_on = on

    # ---- start-up checks --------------------------------------------------------------------
    def show_banner(self, icon_name: str, text: str, actions: list[tuple[str, object]]) -> None:
        from .widgets import clear_layout
        clear_layout(self.banner_btns)
        self.banner_ic.setPixmap(theme.pixmap(icon_name, 16, C["acc"]))
        self.banner_txt.setText(text)
        for i, (txt, fn) in enumerate(actions):
            b = button(txt, None, "primary" if i == len(actions) - 1 else None)
            b.clicked.connect(fn)
            self.banner_btns.addWidget(b)
        self.banner.show()
        self.banner_wrap.show()

    def hide_banner(self) -> None:
        self.banner.hide()
        self.banner_wrap.hide()

    def _check_banners(self) -> None:
        if safemode.active():
            self.show_banner("warn", "Nirvash closed unexpectedly twice in a row, so it started in safe mode: playback "
                             "and the graphics card are off. The crash log is in the logs folder.",
                             [("Open logs", lambda: __import__("nirvash.ui.widgets", fromlist=["x"]).open_in_explorer(
                                 str(_log.log_dir()))), ("Turn them back on", self._leave_safe_mode)])
            return
        miss = storage.missing_places()
        if miss:
            names = ", ".join(f"{lab.split(':')[0]} ({p})" for _k, lab, p in miss)
            drives = sorted({str(p.anchor) for _k, _l, p in miss})
            self.show_banner("drive", f"{' and '.join(drives)} isn't connected, so these can't be reached: {names}. "
                             "Nirvash keeps working with what it can reach.",
                             [("Choose a new place", lambda: (self.go("settings"), self.settings_page.focus_storage())),
                              ("Retry", self._retry_drives)])
        else:
            self.hide_banner()

    def _retry_drives(self) -> None:
        self._check_banners()
        if not storage.missing_places():
            hub.toast.emit("Everything can be reached again.", "ok")
            hub.songsReset.emit()

    def _leave_safe_mode(self) -> None:
        if dialogs.ask(self, "Leave safe mode", "Turn playback and the graphics card back on? Nirvash restarts to "
                       "do that.", "Restart Nirvash"):
            safemode.turn_off()
            self.restart()

    def restart(self) -> None:
        import sys
        from PySide6.QtCore import QProcess
        args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
        QProcess.startDetached(sys.executable, args)
        self.close()

    # ---- evening out song volume ------------------------------------------------------------
    def _job_finished(self, qj) -> None:
        if qj.state == "done" and qj.name.startswith("Finding music"):
            self.level_new_songs()

    def level_new_songs(self) -> None:
        """After a scan: even out the songs it added (Settings > Song volume)."""
        from ..jobs import audio_edit as ae
        if not self.s.get("level.auto", True):
            return
        ids = ae.songs_to_level()
        if not ids or any(j.name.startswith("Evening out") for j in ([self.queue.current] + self.queue.waiting) if j):
            return
        n = len(ids)
        self.queue.add(f"Evening out {n} song{'s' if n != 1 else ''}", ae.level_songs, ids, pausable=True,
                       on_event=lambda k, p: hub.songChanged.emit(int(p)) if k == "song_changed" else None,
                       done_text=lambda res: f"{res['changed']} of {res['measured']} songs made louder or quieter")

    def _ask_level(self) -> None:
        """Once, after updating to a version with automatic levelling: offer it for the songs already there."""
        from ..core.db import get_db
        from ..jobs import audio_edit as ae
        db = get_db()
        if db.kv_get("level_asked") or not self.s.get("level.auto", True):
            return
        ids = ae.songs_to_level()
        db.kv_set("level_asked", "1")
        if not ids:
            return
        n = len(ids)
        if dialogs.ask(self, f"Even out the volume of your {n} songs?",
                       "Nirvash can now make songs that are much louder or quieter than the rest sound alike. It "
                       "measures every song and changes only the ones well away from the rest (the file itself, so "
                       "it plays the same in any player). Each changed song's original is kept in "
                       "Library\\Originals, so Back to the original undoes it. It runs in the background and can be "
                       "paused. New songs are evened out after each scan; turn that off in Settings > Song volume.",
                       f"Even out {n} songs", cancel="Leave them as they are"):
            self.level_new_songs()
        else:
            db.x("UPDATE songs SET level_done=1 WHERE level_done=0")

    def _check_models(self) -> None:
        missing = [sp for sp in (models.AST, models.CLIP) if not models.status(sp)["present"]]
        if not missing:
            return
        lines = []
        for sp in missing:
            why = ("Collection Sorter's copy is gone, so auto-music can't judge mood without it."
                   if sp.key == "clip" else "Needed to find music in videos.")
            lines.append(f"• {sp.label}, about {sp.approx_mb} MB. {why}")
        total = sum(sp.approx_mb for sp in missing)
        if dialogs.ask(self, "AI models aren't downloaded yet", "\n".join(lines) +
                       f"\n\nThey download to {self.s.folder('models')}.",
                       f"Download {'both' if len(missing) > 1 else 'it'} ({total} MB)", "Not now"):
            for sp in missing:
                self.settings_page.download(sp)

    # ---- closing --------------------------------------------------------------------------
    def closeEvent(self, e):
        if self.queue.busy():
            if not dialogs.ask(self, "Stop and close?", f"{self.queue.current.name} is still running"
                               + (f", with {len(self.queue.waiting)} more queued" if self.queue.waiting else "")
                               + ". Finished work is kept.", "Stop and close", "Keep working"):
                e.ignore()
                return
            self.queue.stop_all()
        self.s.set("ui.geometry", bytes(self.saveGeometry().toBase64()).decode())
        self.identify.stop()
        for p in (self.library, self.auto_page):
            try:
                p.player.shutdown()
            except Exception:
                pass
        safemode.clean_exit()
        super().closeEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.toast.isVisible():
            self.toast.place()
