"""The drawer at the bottom: the log, and the move log (every rename or move of a file, each with Undo)."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QPlainTextEdit,
                               QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.db import get_db
from ..jobs import file_ops
from .hub import hub
from .theme import C
from .widgets import Segmented, button, label


class Drawer(QWidget):
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drawer")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setFixedHeight(220)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 6, 8, 6)
        lay.setSpacing(6)
        head = QHBoxLayout()
        self.tabs = Segmented([("log", "Log"), ("moves", "File moves")], "log")
        self.tabs.changed.connect(self.show_tab)
        head.addWidget(self.tabs)
        head.addStretch(1)
        self.copy_btn = button("Copy", "copy", "ghost")
        self.copy_btn.clicked.connect(self._copy)
        head.addWidget(self.copy_btn)
        self.clear_btn = button("Clear", "trash", "ghost")
        self.clear_btn.setToolTip("Clear what's shown here (the log file on disk stays; Settings > Reset can empty it)")
        self.clear_btn.clicked.connect(self._clear)
        head.addWidget(self.clear_btn)
        close = button("Close", "x", "ghost")
        close.setToolTip("Close (Esc or L)")
        close.clicked.connect(self.closed.emit)
        head.addWidget(close)
        lay.addLayout(head)
        self.stack = QStackedWidget()
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.stack.addWidget(self.log)
        mw = QWidget()
        ml = QVBoxLayout(mw)
        ml.setContentsMargins(0, 0, 0, 0)
        self.moves = QTableWidget(0, 5)
        self.moves.setHorizontalHeaderLabels(["When", "What", "From", "To", ""])
        self.moves.verticalHeader().hide()
        self.moves.setShowGrid(False)
        self.moves.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.moves.setSelectionMode(QAbstractItemView.NoSelection)
        hh = self.moves.horizontalHeader()
        for i, w in ((0, 120), (1, 190), (4, 80)):
            self.moves.setColumnWidth(i, w)
            hh.setSectionResizeMode(i, QHeaderView.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.moves.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ml.addWidget(self.moves)
        self.moves_empty = label("Nothing has been renamed or moved yet. Renames from the library show up here, "
                                 "each with Undo (Ctrl+Z undoes the last one).", "faint", wrap=True)
        ml.addWidget(self.moves_empty)
        self.stack.addWidget(mw)
        lay.addWidget(self.stack, 1)
        hub.movesChanged.connect(self._fill_moves)

    def show_tab(self, key: str) -> None:
        self.tabs.set_value(key)
        self.stack.setCurrentIndex(0 if key == "log" else 1)
        self.clear_btn.setVisible(key == "log")
        if key == "moves":
            self._fill_moves()

    def append(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    def _copy(self) -> None:
        if self.stack.currentIndex() == 0:
            QApplication.clipboard().setText(self.log.toPlainText())
        else:
            rows = []
            for r in get_db().moves():
                rows.append(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['at']))}\t{r['action']}\t"
                            f"{r['src']}\t{r['dst']}\t{'undone' if r['undone'] else ''}")
            QApplication.clipboard().setText("\n".join(rows))
        hub.toast.emit("Copied.", "ok")

    def _clear(self) -> None:
        self.log.clear()

    def _fill_moves(self) -> None:
        if self.stack.currentIndex() != 1:
            return
        rows = get_db().moves(300)
        self.moves.setRowCount(len(rows))
        self.moves_empty.setVisible(not rows)
        self.moves.setVisible(bool(rows))
        for i, r in enumerate(rows):
            when = time.strftime("%d %b %H:%M", time.localtime(r["at"]))
            for j, txt in enumerate((when, r["action"], Path(r["src"]).name, Path(r["dst"]).name)):
                it = QTableWidgetItem(txt)
                if j >= 2:
                    it.setToolTip(r["src"] if j == 2 else r["dst"])
                if r["undone"]:
                    it.setForeground(Qt.gray)
                self.moves.setItem(i, j, it)
            if r["undone"]:
                self.moves.setItem(i, 4, QTableWidgetItem("Undone"))
                self.moves.setCellWidget(i, 4, None)
            else:
                b = QPushButton("Undo")
                b.setObjectName("ghost")
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, mid=r["id"]: undo(mid))
                self.moves.setCellWidget(i, 4, b)


def undo(mid: int | None = None) -> None:
    """Undo a move (the last one when mid is None) and say what happened."""
    db = get_db()
    if mid is None:
        m = db.last_move()
        if m is None:
            hub.toast.emit("There's nothing to undo.", "info")
            return
        mid = m["id"]
    m = db.move(mid)
    res = file_ops.undo_move(mid)
    ok = res.startswith("Put ")
    hub.toast.emit(res, "ok" if ok else "warn")
    if ok and m is not None and m["song_id"] is not None:
        hub.songChanged.emit(int(m["song_id"]))
    hub.movesChanged.emit()
