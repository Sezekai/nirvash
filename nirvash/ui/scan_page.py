"""Find music: scan folders of videos and extract the music into the library."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QListWidget,
                               QListWidgetItem, QProgressBar, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..analysis import models
from ..core.settings import get_settings
from ..jobs import scan as scanjob
from ..media.ff import fmt_time
from . import workers
from .hub import hub
from .theme import C, icon
from .widgets import Toggle, button, card, label, open_in_explorer, tool

RESULT_COLOURS = {"music": C["teal"], "partial": C["pink"], "speech": C["muted"], "silent": C["faint"],
                  "no_audio": C["faint"], "skipped": C["faint"], "error": C["red"]}


class Stat(QWidget):
    def __init__(self, title: str, color: str):
        super().__init__()
        l = QVBoxLayout(self)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(0)
        self.v = label("0")
        self.v.setStyleSheet(f"font-size: 20pt; font-weight: 600; color: {color};")
        l.addWidget(self.v)
        l.addWidget(label(title, "muted"))

    def set(self, n: int) -> None:
        self.v.setText(str(n))


class ScanPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.s = get_settings()
        self.job: workers.Job | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(label("Find music", "h1"))
        head.addStretch(1)
        root.addLayout(head)
        root.addWidget(label("Add folders of videos. Nirvash listens to each one, finds the music, and copies it into "
                             "your library without re-encoding. Talking and other sounds are left out.", "muted", wrap=True))

        self.banner = card("bannerWarn")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(14, 10, 14, 10)
        self.banner_txt = label("", wrap=True)
        bl.addWidget(self.banner_txt, 1)
        self.banner_btn = button("Download CLAP (~620 MB)", "download")
        self.banner_btn.clicked.connect(self._download_clap)
        bl.addWidget(self.banner_btn)
        root.addWidget(self.banner)

        top = QHBoxLayout()
        top.setSpacing(12)
        fc = card()
        fl = QVBoxLayout(fc)
        fl.setContentsMargins(14, 12, 14, 12)
        fh = QHBoxLayout()
        fh.addWidget(label("Folders", "h2"))
        fh.addStretch(1)
        add = button("Add folder", "plus")
        add.clicked.connect(self._add_folder)
        fh.addWidget(add)
        rm = tool("minus", "Remove selected folder", C["muted"])
        rm.clicked.connect(self._remove_folder)
        fh.addWidget(rm)
        fl.addLayout(fh)
        self.folders = QListWidget()
        self.folders.setMinimumHeight(110)
        self.folders.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for f in self.s.get("ui.last_scan_folders", []) or []:
            self._folder_item(f)
        fl.addWidget(self.folders)
        opts = QHBoxLayout()
        opts.setSpacing(8)
        self.t_rec = Toggle(bool(self.s.get("scan.recursive", True)))
        self.t_rec.toggledOn.connect(lambda v: self.s.set("scan.recursive", v))
        opts.addWidget(self.t_rec)
        opts.addWidget(label("Include subfolders"))
        opts.addSpacing(16)
        self.t_re = Toggle(bool(self.s.get("scan.rescan", False)))
        self.t_re.toggledOn.connect(lambda v: self.s.set("scan.rescan", v))
        opts.addWidget(self.t_re)
        opts.addWidget(label("Re-check videos already scanned"))
        opts.addStretch(1)
        fl.addLayout(opts)
        top.addWidget(fc, 3)

        rc = card()
        rl = QVBoxLayout(rc)
        rl.setContentsMargins(16, 12, 16, 12)
        stats = QHBoxLayout()
        self.st_videos = Stat("Videos checked", C["text"])
        self.st_added = Stat("Songs added", C["teal"])
        self.st_merged = Stat("Duplicates merged", C["violet"])
        self.st_partial = Stat("Partial — check", C["pink"])
        for w in (self.st_videos, self.st_added, self.st_merged, self.st_partial):
            stats.addWidget(w)
        rl.addLayout(stats)
        rl.addStretch(1)
        self.prog = QProgressBar()
        self.prog.setRange(0, 1000)
        rl.addWidget(self.prog)
        self.prog_lbl = label("Ready", "muted")
        rl.addWidget(self.prog_lbl)
        btns = QHBoxLayout()
        self.go = button("Find music", "sparkle", "primary")
        self.go.setMinimumHeight(36)
        self.go.clicked.connect(self.start)
        btns.addWidget(self.go, 1)
        self.cancel_btn = button("Stop", "x")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        btns.addWidget(self.cancel_btn)
        rl.addLayout(btns)
        self.id_lbl = label("", "muted", wrap=True)
        rl.addWidget(self.id_lbl)
        top.addWidget(rc, 2)
        root.addLayout(top)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Video", "Result", "Music found", "Library"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i, w in ((1, 200), (2, 240), (3, 170)):
            hh.setSectionResizeMode(i, QHeaderView.Interactive)
            self.table.setColumnWidth(i, w)
        self.table.doubleClicked.connect(self._open_row)
        root.addWidget(self.table, 1)

        hub.identifyStatus.connect(self._id_status)
        hub.modelsChanged.connect(self.refresh_banner)
        self.refresh_banner()

    # ---- folders --------------------------------------------------------------
    def _folder_item(self, f: str) -> None:
        it = QListWidgetItem(icon("folder", C["violet"], 16), f)
        it.setSizeHint(QSize(0, 28))
        self.folders.addItem(it)

    def _folders(self) -> list[str]:
        return [self.folders.item(i).text() for i in range(self.folders.count())]

    def _add_folder(self) -> None:
        start = self._folders()[-1] if self._folders() else str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Add a folder of videos", start)
        if d and d not in self._folders():
            self._folder_item(str(Path(d)))
            self.s.set("ui.last_scan_folders", self._folders())

    def _remove_folder(self) -> None:
        for it in self.folders.selectedItems():
            self.folders.takeItem(self.folders.row(it))
        self.s.set("ui.last_scan_folders", self._folders())

    def refresh_banner(self) -> None:
        st = models.status(models.CLAP)
        self.banner.setVisible(not st["present"])
        self.banner_txt.setText("Finding music needs the CLAP audio model. It's a one-time download to "
                                f"{models.own_dir(models.CLAP)}.")

    def _download_clap(self) -> None:
        w = self.window()
        if hasattr(w, "settings_page"):
            w.settings_page.download(models.CLAP)
            w.go("settings")

    # ---- run --------------------------------------------------------------------
    def start(self) -> None:
        folders = [f for f in self._folders() if Path(f).exists()]
        if not folders:
            hub.toast.emit("Add a folder of videos first.", "warn")
            return
        if not models.status(models.CLAP)["present"]:
            hub.toast.emit("Download the CLAP model first (Settings > AI models).", "warn")
            return
        self.table.setRowCount(0)
        self.counts = {"videos": 0, "added": 0, "merged": 0, "partial": 0}
        self._update_stats()
        self.job = workers.Job(scanjob.scan_folders, folders)
        self.job.sig.progress.connect(self._progress)
        self.job.sig.event.connect(self._event)
        self.job.sig.done.connect(lambda _r: self._finish("Finished"))
        self.job.sig.failed.connect(lambda e: self._finish(f"Stopped: {e}", error=True))
        self.job.sig.cancelled.connect(lambda: self._finish("Stopped"))
        self.go.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.prog.setValue(0)
        workers.start(self.job)
        w = self.window()
        if hasattr(w, "identify"):
            w.identify.poke()

    def cancel(self) -> None:
        if self.job:
            self.job.cancel()
            self.prog_lbl.setText("Stopping after the current video…")

    def _finish(self, msg: str, error: bool = False) -> None:
        self.go.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        c = self.counts
        self.prog_lbl.setText(f"{msg} — {c['videos']} videos checked, {c['added']} songs added, "
                              f"{c['merged']} duplicates merged.")
        hub.toast.emit(msg if error else f"Found {c['added']} new song{'s' if c['added'] != 1 else ''}.",
                       "error" if error else "ok")
        self.job = None

    def _progress(self, f: float, msg: str) -> None:
        self.prog.setValue(int(f * 1000))
        if msg and msg != "Done":
            self.prog_lbl.setText(f"{self.counts['videos']} / {getattr(self, 'total', '?')} · {msg}")

    def _event(self, kind: str, payload) -> None:
        if kind == "total":
            self.total = payload
        elif kind in ("song_added", "song_changed"):
            hub.songChanged.emit(int(payload))
            w = self.window()
            if hasattr(w, "identify"):
                w.identify.poke()
        elif kind == "video_done":
            self._add_result(payload)

    def _add_result(self, r: scanjob.VideoResult) -> None:
        c = self.counts
        c["videos"] += 1
        c["added"] += len(r.added)
        c["merged"] += len(r.merged)
        if r.status == "partial":
            c["partial"] += len(r.added)
        self._update_stats()
        if r.status == "skipped" and not self.s.get("scan.rescan"):
            pass
        row = self.table.rowCount()
        self.table.insertRow(row)
        name = QTableWidgetItem(icon("film", C["muted"], 16), Path(r.path).name)
        name.setToolTip(r.path)
        name.setData(256, r.path)
        self.table.setItem(row, 0, name)
        res = QTableWidgetItem(scanjob.STATUS_TEXT.get(r.status, r.status))
        res.setForeground(QColor(RESULT_COLOURS.get(r.status, C["muted"])))
        if r.status == "error":
            res.setToolTip(r.detail)
        self.table.setItem(row, 1, res)
        found = ", ".join(f"{fmt_time(s.start)}–{fmt_time(s.end)}" for s in r.sections) if r.sections else \
            (r.detail if r.status == "skipped" else "")
        self.table.setItem(row, 2, QTableWidgetItem(found))
        lib = []
        if r.added:
            lib.append(f"{len(r.added)} added")
        if r.merged:
            lib.append(f"{len(r.merged)} duplicate{'s' if len(r.merged) > 1 else ''}")
        li = QTableWidgetItem(", ".join(lib))
        li.setForeground(QColor(C["teal"] if r.added else C["muted"]))
        self.table.setItem(row, 3, li)
        self.table.scrollToBottom()

    def _update_stats(self) -> None:
        c = self.counts
        self.st_videos.set(c["videos"])
        self.st_added.set(c["added"])
        self.st_merged.set(c["merged"])
        self.st_partial.set(c["partial"])

    def _open_row(self, idx) -> None:
        it = self.table.item(idx.row(), 0)
        if it:
            open_in_explorer(it.data(256))

    def _id_status(self, pending: int, current: str) -> None:
        if pending:
            self.id_lbl.setText(f"Identifying songs in the background: {pending} waiting. "
                                "Shazam allows about 20 lookups a minute.")
        else:
            self.id_lbl.setText("")

    counts = {"videos": 0, "added": 0, "merged": 0, "partial": 0}
