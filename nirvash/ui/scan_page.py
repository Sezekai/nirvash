"""Find music: scan folders of videos and extract the music into the library."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from ..analysis import models
from ..core.settings import get_settings
from ..jobs import scan as scanjob
from ..media.ff import fmt_time
from . import theme
from .hub import hub
from .jobqueue import get_queue
from .theme import C, icon
from .widgets import Segmented, button, flow_row, label, open_in_explorer, page_header, panel

# result -> (filter group, colour token)
GROUP = {"music": ("music", "ok"), "partial": ("check", "warn"), "speech": ("none", "sec"), "silent": ("none", "faint"),
         "no_audio": ("none", "faint"), "skipped": ("skipped", "faint"), "ignored": ("skipped", "faint"),
         "in_library": ("skipped", "faint"), "error": ("failed", "danger")}


class FolderChip(QFrame):
    def __init__(self, path: str, page: "ScanPage"):
        super().__init__()
        self.path = path
        self.setObjectName("card2")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(7, 0, 2, 0)
        lay.setSpacing(5)
        ic = QLabel()
        ic.setPixmap(theme.pixmap("folder", 14, C["amber"]))
        lay.addWidget(ic)
        t = QLabel(path)
        t.setToolTip(path + ("" if Path(path).exists() else "\nThis folder can't be reached right now."))
        if not Path(path).exists():
            t.setStyleSheet(f"color: {C['warn']};")
        lay.addWidget(t)
        x = QToolButton()
        x.setIcon(icon("x", C["sec"], 12))
        x.setToolTip("Take this folder off the list (nothing on disk changes)")
        x.setCursor(Qt.PointingHandCursor)
        x.clicked.connect(lambda: page.remove_folder(path))
        lay.addWidget(x)
        self.setFixedHeight(theme.T["ch"] - 2)


class ScanPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.s = get_settings()
        self.queue = get_queue()
        self.counts = {"videos": 0, "added": 0, "merged": 0, "partial": 0, "total": 0}
        self.groups = {"all": 0, "music": 0, "check": 0, "none": 0, "skipped": 0, "failed": 0}
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)
        head, _ = page_header("Find music", "Listens to each video, finds the music, and copies it into the library "
                                            "without re-encoding. Talking and other sounds are left out")
        root.addLayout(head)

        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(10, 6, 8, 6)
        bl.setSpacing(10)
        bi = QLabel()
        bi.setPixmap(theme.pixmap("download", 16, C["acc"]))
        bl.addWidget(bi)
        self.banner_txt = label("", wrap=True)
        bl.addWidget(self.banner_txt, 1)
        self.banner_btn = button(f"Download ({models.AST.approx_mb} MB)", "download")
        self.banner_btn.clicked.connect(self._download_model)
        bl.addWidget(self.banner_btn)
        root.addWidget(self.banner)

        # Toolbar: folders and the main button, then the options
        self.toolbar = panel()
        tl = QVBoxLayout(self.toolbar)
        tl.setContentsMargins(10, 8, 10, 8)
        tl.setSpacing(8)
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        lb = label("Folders", "cap")
        lb.setFixedWidth(88)
        r1.addWidget(lb, 0, Qt.AlignTop | Qt.AlignLeft)
        self.chips_w, self.chips = flow_row(6)
        r1.addWidget(self.chips_w, 1)
        self.no_folders = label("No folders yet. Add one or more folders of videos.", "faint")
        r1.addWidget(self.no_folders, 1)
        add = button("Add folder", "plus")
        add.clicked.connect(self._add_folder)
        r1.addWidget(add, 0, Qt.AlignTop)
        self.go = button("Find music", "sparkle", "primary")
        self.go.clicked.connect(self.start)
        r1.addWidget(self.go, 0, Qt.AlignTop)
        tl.addLayout(r1)
        r2 = QHBoxLayout()
        r2.setSpacing(14)
        lb = label("Options", "cap")
        lb.setFixedWidth(88)
        r2.addWidget(lb)
        self.t_rec = QCheckBox("Include subfolders")
        self.t_rec.setChecked(bool(self.s.get("scan.recursive", True)))
        self.t_rec.toggled.connect(lambda v: self.s.set("scan.recursive", v))
        r2.addWidget(self.t_rec)
        self.t_re = QCheckBox("Re-check videos that gave no songs")
        self.t_re.setChecked(bool(self.s.get("scan.rescan", False)))
        self.t_re.setToolTip("Videos whose songs are in your library, and videos you deleted songs from, are always "
                             "skipped - even if the folder was moved.")
        self.t_re.toggled.connect(lambda v: self.s.set("scan.rescan", v))
        r2.addWidget(self.t_re)
        r2.addStretch(1)
        self.id_lbl = label("", "faint")
        r2.addWidget(self.id_lbl)
        tl.addLayout(r2)
        root.addWidget(self.toolbar)

        # Work area: summary line with filters, then the results
        work = panel()
        wl = QVBoxLayout(work)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        sm = QHBoxLayout()
        sm.setContentsMargins(10, 6, 8, 6)
        sm.setSpacing(10)
        self.summary = label("Add folders of videos, then press Find music.", "h2")
        sm.addWidget(self.summary)
        self.summary2 = label("", "muted")
        sm.addWidget(self.summary2, 1)
        self.filter = Segmented([("all", "All"), ("music", "Music"), ("check", "To check"), ("none", "No music"),
                                 ("skipped", "Skipped"), ("failed", "Failed")], "all")
        self.filter.changed.connect(self._apply_filter)
        sm.addWidget(self.filter)
        wl.addLayout(sm)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Video", "Result", "Music found", "Library"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(theme.T["rh"])
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setWordWrap(False)
        self.table.setStyleSheet("QTableWidget { border: none; border-top: 1px solid " + C["line"] + "; border-radius: 0; }")
        hh = self.table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i, w in ((1, 230), (2, 220), (3, 180)):
            hh.setSectionResizeMode(i, QHeaderView.Interactive)
            self.table.setColumnWidth(i, w)
        self.table.doubleClicked.connect(self._open_row)
        wl.addWidget(self.table, 1)
        self.empty = label("Results show here as each video is checked. Double-click a video to see it in its folder.",
                           "faint")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setContentsMargins(0, 24, 0, 24)
        wl.addWidget(self.empty)
        root.addWidget(work, 1)

        for f in self.s.get("ui.last_scan_folders", []) or []:
            self._folder_chip(f)
        self._update_go()
        hub.identifyStatus.connect(self._id_status)
        hub.modelsChanged.connect(self.refresh_banner)
        self.queue.changed.connect(self._update_go)
        self.refresh_banner()

    # ---- shared keys ----------------------------------------------------------------
    def set_panels_hidden(self, hide: bool) -> None:
        self.toolbar.setVisible(not hide)

    def theme_changed(self) -> None:
        self.table.verticalHeader().setDefaultSectionSize(theme.T["rh"])
        self.table.setStyleSheet("QTableWidget { border: none; border-top: 1px solid " + C["line"] + "; border-radius: 0; }")

    # ---- folders --------------------------------------------------------------------
    def _folders(self) -> list[str]:
        return [self.chips.itemAt(i).widget().path for i in range(self.chips.count())]

    def _folder_chip(self, f: str) -> None:
        self.chips.addWidget(FolderChip(f, self))

    def _add_folder(self) -> None:
        start = self._folders()[-1] if self._folders() else str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Add a folder of videos", start)
        if d and str(Path(d)) not in self._folders():
            self._folder_chip(str(Path(d)))
            self._save_folders()

    def remove_folder(self, path: str) -> None:
        for i in range(self.chips.count()):
            w = self.chips.itemAt(i).widget()
            if w.path == path:
                self.chips.takeAt(i)
                w.hide()
                w.deleteLater()
                break
        self._save_folders()

    def _save_folders(self) -> None:
        self.s.set("ui.last_scan_folders", self._folders())
        self.chips_w.updateGeometry()
        self._update_go()

    def _update_go(self) -> None:
        n = len(self._folders())
        self.no_folders.setVisible(n == 0)
        self.chips_w.setVisible(n > 0)
        what = f"{n} folder{'s' if n != 1 else ''}" if n else "folders"
        busy = self.queue.busy()
        self.go.setText(f"Add {what} to the queue" if busy else f"Find music in {what}")
        self.go.setEnabled(n > 0)

    def refresh_banner(self) -> None:
        st = models.status(models.AST)
        self.banner.setVisible(not st["present"])
        self.banner_txt.setText(f"<b>The audio tagger isn't downloaded.</b> Finding music needs it: {models.AST.approx_mb}"
                                f" MB to {models.own_dir(models.AST)}.")

    def _download_model(self) -> None:
        w = self.window()
        if hasattr(w, "settings_page"):
            w.settings_page.download(models.AST)

    # ---- run --------------------------------------------------------------------------
    def start(self) -> None:
        folders = [f for f in self._folders() if Path(f).exists()]
        if not folders:
            hub.toast.emit("Add a folder of videos first (folders shown in amber can't be reached).", "warn")
            return
        if not models.status(models.AST)["present"]:
            hub.toast.emit("Download the audio tagger first (the button above).", "warn")
            return
        n = len(folders)
        qj = self.queue.add(f"Finding music in {n} folder{'s' if n != 1 else ''}", scanjob.scan_folders, folders,
                            pausable=True, ai=True, detail="Listing the videos…",
                            on_start=self._reset_results, on_event=self._event, on_progress=self._progress,
                            done_text=lambda _r: self._done_text(),
                            on_done=lambda _r: hub.toast.emit(f"Found {self.counts['added']} new song"
                                                              f"{'s' if self.counts['added'] != 1 else ''}.", "ok"),
                            on_failed=lambda e: hub.toast.emit(f"Finding music stopped: {e}", "error"))
        if self.queue.current is not qj:
            hub.toast.emit("Added to the queue; it starts when the current job finishes.", "info")
        w = self.window()
        if hasattr(w, "identify"):
            w.identify.poke()

    def _reset_results(self) -> None:
        self.table.setRowCount(0)
        self.counts = {"videos": 0, "added": 0, "merged": 0, "partial": 0, "total": 0}
        self.groups = {k: 0 for k in self.groups}
        self._update_summary()

    def _done_text(self) -> str:
        c = self.counts
        return (f"{c['videos']:,} videos checked, {c['added']} songs added, {c['merged']} duplicates"
                + (f", {c['partial']} to check" if c["partial"] else ""))

    def _progress(self, f: float, msg: str) -> None:
        q = self.queue.current
        if q is not None and msg and msg != "Done" and self.counts["total"]:
            q.detail = f"Video {min(self.counts['videos'] + 1, self.counts['total']):,} of {self.counts['total']:,} · {msg}"

    def _event(self, kind: str, payload) -> None:
        if kind == "total":
            self.counts["total"] = int(payload)
            self._update_summary()
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
        c["partial"] += min(len(r.added), sum(1 for sec in r.sections if sec.check))
        grp, col = GROUP.get(r.status, ("none", "sec"))
        self.groups["all"] += 1
        self.groups[grp] += 1
        row = self.table.rowCount()
        self.table.insertRow(row)
        name = QTableWidgetItem(Path(r.path).name)
        name.setToolTip(r.path)
        name.setData(256, r.path)
        name.setData(257, grp)
        self.table.setItem(row, 0, name)
        res = QTableWidgetItem(scanjob.STATUS_TEXT.get(r.status, r.status))
        res.setForeground(QColor(C[col]))
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
        if r.dropped:
            lib.append(f"{r.dropped} you deleted before")
        li = QTableWidgetItem(", ".join(lib))
        li.setForeground(QColor(C["ok"] if r.added else C["sec"]))
        self.table.setItem(row, 3, li)
        self.table.setRowHidden(row, self.filter.value not in ("all", grp))
        if self.filter.value in ("all", grp):
            self.table.scrollToBottom()
        self._update_summary()

    def _update_summary(self) -> None:
        c = self.counts
        has = self.table.rowCount() > 0
        self.empty.setVisible(not has)
        if c["total"]:
            self.summary.setText(f"Checked {c['videos']:,} of {c['total']:,}")
            self.summary2.setText(f"· {c['added']} songs added · {c['merged']} duplicates"
                                  + (f" · {c['partial']} to check" if c["partial"] else ""))
        for k, n in self.groups.items():
            self.filter.set_count(k, n if has else None)

    def _apply_filter(self, key) -> None:
        for row in range(self.table.rowCount()):
            g = self.table.item(row, 0).data(257)
            self.table.setRowHidden(row, key not in ("all", g))

    def _open_row(self, idx) -> None:
        it = self.table.item(idx.row(), 0)
        if it:
            open_in_explorer(it.data(256))

    def _id_status(self, st: dict) -> None:
        on = bool(st.get("pending"))
        self.id_lbl.setText("Songs are identified in the background - see the bar at the bottom." if on else "")
