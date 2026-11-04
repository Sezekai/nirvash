"""Choose any song from the library for a video, sorted by how well it fits."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLineEdit, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..analysis.labels import top_moods
from ..core.db import get_db
from ..jobs import automusic as am
from ..media.ff import fmt_time
from .player import Player
from .theme import C, icon
from .widgets import PlayerBar, button, label


class NumItem(QTableWidgetItem):
    def __lt__(self, other):
        a, b = self.data(Qt.UserRole), other.data(Qt.UserRole)
        try:
            return float(a) < float(b)
        except Exception:
            return super().__lt__(other)


class SongPicker(QDialog):
    def __init__(self, item: am.AutoItem, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose a song")
        self.resize(900, 620)
        self.item = item
        self.chosen: int | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(label(f"Song for {Path(item.video).name}", "h2"))
        head.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(260)
        self.search.textChanged.connect(self._filter)
        head.addWidget(self.search)
        lay.addLayout(head)
        lay.addWidget(label(f"Video length {fmt_time(item.duration)}. Sorted by fit; click a column to sort another way.",
                            "muted"))

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Fit", "Title", "Artist", "Length", "BPM", "Mood", "★"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        for i, w in enumerate((56, 260, 170, 70, 56, 140, 30)):
            self.table.setColumnWidth(i, w)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.doubleClicked.connect(lambda _i: self._use())
        self.table.currentCellChanged.connect(lambda r, *_: self._preview(r))
        lay.addWidget(self.table, 1)

        self.player = Player(self, video=False)
        self.bar = PlayerBar(self.player)
        lay.addWidget(self.bar)
        bb = QHBoxLayout()
        bb.addStretch(1)
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        bb.addWidget(cancel)
        use = button("Use this song", "check", "primary")
        use.clicked.connect(self._use)
        bb.addWidget(use)
        lay.addLayout(bb)
        self._fill()

    def _fill(self) -> None:
        songs = am.load_songs()
        mode = am.eff(self.item, "mode")
        rule = am.rule_category(self.item.video)
        rows = []
        db = get_db()
        for sg in songs:
            sc = am.score_song(self.item, sg, mode if mode != "long_only" else "trim_fade", 0, rule)
            r = db.song(sg.id)
            rows.append((sc[0] if sc else 0.0, sg, r))
        rows.sort(key=lambda x: -x[0])
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, (sc, sg, r) in enumerate(rows):
            fit = NumItem(f"{max(0, sc) * 100:.0f}")
            fit.setData(Qt.UserRole, sc)
            fit.setData(Qt.UserRole + 1, sg.id)
            fit.setForeground(QColor(C["teal"] if sc >= 0.7 else C["muted"]))
            self.table.setItem(i, 0, fit)
            t = QTableWidgetItem(r["title"] or Path(r["file_path"]).stem)
            if sg.id == self.item.song_id:
                t.setIcon(icon("check", C["teal"], 14))
            self.table.setItem(i, 1, t)
            self.table.setItem(i, 2, QTableWidgetItem(r["artist"] or ""))
            ln = NumItem(fmt_time(sg.duration))
            ln.setData(Qt.UserRole, sg.duration)
            if sg.duration < self.item.duration:
                ln.setForeground(QColor(C["amber"]))
                ln.setToolTip("Shorter than the video")
            self.table.setItem(i, 3, ln)
            bpm = NumItem(f"{r['bpm']:.0f}" if r["bpm"] else "")
            bpm.setData(Qt.UserRole, r["bpm"] or 0)
            self.table.setItem(i, 4, bpm)
            self.table.setItem(i, 5, QTableWidgetItem(", ".join(top_moods(sg.mood))))
            self.table.setItem(i, 6, QTableWidgetItem("★" if sg.favourite else ""))
        self.table.setSortingEnabled(True)

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        for i in range(self.table.rowCount()):
            hay = " ".join((self.table.item(i, c).text() if self.table.item(i, c) else "") for c in (1, 2, 5)).lower()
            self.table.setRowHidden(i, not all(w in hay for w in words))

    def _sid(self, row: int) -> int | None:
        it = self.table.item(row, 0)
        return it.data(Qt.UserRole + 1) if it else None

    def _preview(self, row: int) -> None:
        sid = self._sid(row)
        r = get_db().song(sid) if sid is not None else None
        if r is None:
            return
        self.bar.load(r["file_path"], f"song{sid}-{int(r['duration'] * 1000)}", r["duration"],
                      r["title"] or Path(r["file_path"]).stem, play=True)

    def _use(self) -> None:
        sid = self._sid(self.table.currentRow())
        if sid is not None:
            self.chosen = sid
            self.accept()

    def done(self, r):
        self.player.shutdown()
        super().done(r)
