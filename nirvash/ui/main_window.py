"""Main window: icon rail, pages, status strip, log drawer and toasts."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QPoint, QSize, Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
                               QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from .. import APP_NAME, VERSION
from ..analysis import models
from ..core import log as _log
from ..core.settings import get_settings
from ..jobs.identify import IdentifyQueue
from . import theme
from .automusic_page import AutoMusicPage
from .hub import hub
from .library_page import LibraryPage
from .scan_page import ScanPage
from .settings_page import SettingsPage
from .theme import C, icon
from .widgets import label, tool


class EditorPlaceholder(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        l = QVBoxLayout(self)
        l.setContentsMargins(22, 18, 22, 18)
        l.addWidget(label("Editor", "h1"))
        l.addWidget(label("Trims, joins, moving crop with tracking, zoom, rotate, blur and pixelate, overlays and colour "
                          "arrive in Stage 2.", "muted", wrap=True))
        l.addStretch(1)


class Toast(QLabel):
    COLORS = {"info": C["violet"], "ok": C["teal"], "warn": C["amber"], "error": C["red"]}

    def __init__(self, parent):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setMaximumWidth(460)
        self.hide()
        self.timer = QTimer(self, singleShot=True, timeout=self.hide)

    def show_msg(self, text: str, kind: str = "info") -> None:
        col = self.COLORS.get(kind, C["violet"])
        self.setStyleSheet(f"background:{C['surface3']}; color:{C['text']}; border:1px solid {col};"
                           f"border-left: 4px solid {col}; border-radius:10px; padding:10px 14px;")
        self.setText(text)
        self.adjustSize()
        p = self.parentWidget()
        self.move(p.width() - self.width() - 24, p.height() - self.height() - 56)
        self.raise_()
        self.show()
        self.timer.start(4500 if kind in ("info", "ok") else 8000)

    def mousePressEvent(self, e):
        self.hide()


class MainWindow(QMainWindow):
    PAGES = [("library", "Library", "library"), ("scan", "Find music", "scan"), ("auto", "Auto-music", "wand"),
             ("editor", "Editor", "cut")]

    def __init__(self):
        super().__init__()
        self.s = get_settings()
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.resize(1480, 900)
        self.setMinimumSize(1100, 700)
        geo = self.s.get("ui.geometry")
        if geo:
            try:
                self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
            except Exception:
                pass

        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        rail = QWidget()
        rail.setObjectName("rail")
        rail.setFixedWidth(84)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(8, 14, 8, 12)
        rl.setSpacing(6)
        logo = QLabel()
        logo.setPixmap(theme.pixmap("sparkle", 30, C["teal"]))
        logo.setAlignment(Qt.AlignCenter)
        logo.setToolTip(f"{APP_NAME} {VERSION}")
        rl.addWidget(logo)
        name = label(APP_NAME)
        name.setAlignment(Qt.AlignCenter)
        name.setStyleSheet(f"font-weight:700; letter-spacing:1px; color:{C['text']}; font-size:9pt;")
        rl.addWidget(name)
        rl.addSpacing(14)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.rail_btns: dict[str, QToolButton] = {}
        for key, text, ic in self.PAGES + [("settings", "Settings", "gear")]:
            b = QToolButton()
            b.setObjectName("railBtn")
            b.setCheckable(True)
            b.setText(text)
            b.setIcon(icon(ic, C["muted"], 22, checked_color=C["teal"]))
            b.setIconSize(QSize(22, 22))
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setFixedSize(68, 62)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.go(k))
            self.group.addButton(b)
            self.rail_btns[key] = b
            if key == "settings":
                rl.addStretch(1)
            rl.addWidget(b, 0, Qt.AlignHCenter)
        outer.addWidget(rail)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.stack = QStackedWidget()
        self.library = LibraryPage()
        self.scan_page = ScanPage()
        self.auto_page = AutoMusicPage()
        self.editor_page = EditorPlaceholder()
        self.settings_page = SettingsPage()
        self.pages = {"library": self.library, "scan": self.scan_page, "auto": self.auto_page,
                      "editor": self.editor_page, "settings": self.settings_page}
        for p in self.pages.values():
            self.stack.addWidget(p)
        right.addWidget(self.stack, 1)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setFixedHeight(180)
        self.log_view.setStyleSheet(f"font-family: Consolas, 'Cascadia Mono', monospace; font-size: 9pt;"
                                    f"background:{C['rail']}; border:none; border-top:1px solid {C['border']}; border-radius:0;")
        self.log_view.setVisible(bool(self.s.get("ui.log_open", False)))
        right.addWidget(self.log_view)

        strip = QWidget()
        strip.setStyleSheet(f"background:{C['rail']}; border-top:1px solid {C['border']};")
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(14, 4, 10, 4)
        self.id_lbl = label("", "muted")
        sl.addWidget(self.id_lbl, 1)
        self.log_btn = tool("log", "Show / hide the log", C["muted"], 16, checkable=True)
        self.log_btn.setChecked(self.log_view.isVisible())
        self.log_btn.toggled.connect(self._toggle_log)
        sl.addWidget(self.log_btn)
        right.addWidget(strip)
        outer.addLayout(right, 1)

        self.toast = Toast(central)
        hub.toast.connect(self.toast.show_msg)
        hub.logLine.connect(self._log_line)
        _log.add_listener(lambda lvl, msg: hub.logLine.emit(lvl, msg))
        hub.identifyStatus.connect(self._id_status)

        self.identify = IdentifyQueue(on_change=lambda sid: hub.songChanged.emit(sid),
                                      on_status=lambda n, cur: hub.identifyStatus.emit(n, cur))
        self.go("library")
        QTimer.singleShot(600, self._check_models)

    # ---- navigation ------------------------------------------------------------
    def go(self, key: str) -> None:
        if key not in self.pages:
            return
        cur = self.stack.currentWidget()
        if cur is not self.pages[key] and hasattr(cur, "stop"):
            cur.stop()
        self.stack.setCurrentWidget(self.pages[key])
        self.rail_btns[key].setChecked(True)

    def _toggle_log(self, on: bool) -> None:
        self.log_view.setVisible(on)
        self.s.set("ui.log_open", on)

    def _log_line(self, level: str, msg: str) -> None:
        self.log_view.appendPlainText(msg)

    def _id_status(self, pending: int, current: str) -> None:
        self.id_lbl.setText(f"Identifying songs: {pending} waiting · {current}" if pending and current else
                            (f"Identifying songs: {pending} waiting" if pending else ""))

    def _check_models(self) -> None:
        missing = [sp for sp in (models.AST, models.CLIP) if not models.status(sp)["present"]]
        if not missing:
            return
        lines = []
        for sp in missing:
            why = ("Collection Sorter's copy is gone, so auto-music can't judge mood without it."
                   if sp.key == "clip" else "Needed to find music in videos.")
            lines.append(f"• {sp.label}, about {sp.approx_mb} MB — {why}")
        box = QMessageBox(self)
        box.setWindowTitle("AI models")
        box.setText("These models aren't on this PC yet:\n\n" + "\n".join(lines) +
                    f"\n\nThey download to {self.s.folder('models')}.")
        dl = box.addButton("Download", QMessageBox.AcceptRole)
        box.addButton("Not now", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is dl:
            for sp in missing:
                self.settings_page.download(sp)
            self.go("settings")

    def closeEvent(self, e):
        self.s.set("ui.geometry", bytes(self.saveGeometry().toBase64()).decode())
        self.identify.stop()
        for p in (self.library, self.auto_page):
            try:
                p.player.shutdown()
            except Exception:
                pass
        super().closeEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.toast.isVisible():
            p = self.centralWidget()
            self.toast.move(p.width() - self.toast.width() - 24, p.height() - self.toast.height() - 56)
