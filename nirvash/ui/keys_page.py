"""The keyboard shortcuts popup (K): opens over whatever screen is showing (DESIGN.md, "Keyboard shortcuts popup").

K again, Esc or a click outside closes it. It always fits inside the window: groups flow into more columns (about
340 px each) before anything scrolls, and a group's heading stays with its first key."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from .theme import C
from .widgets import flow_row, label

# (group, [(keys, what)]): each key is drawn as a keycap; alternatives are separate caps
KEYS = [
    ("Anywhere", [
        (["K"], "These shortcuts"),
        (["/", "Ctrl+F"], "Search (the library's search box)"),
        (["F", "F11"], "Full screen"),
        (["H"], "Hide the panels so only the list or video shows (H or Esc brings them back)"),
        (["L"], "Open or close the log"),
        (["M"], "Show or hide the rail on the left"),
        (["S"], "Settings"),
        (["Ctrl+Z"], "Undo the last rename or move"),
        (["Esc"], "Close what's open: this popup or a dialog, then hidden panels, full screen, then the log"),
    ]),
    ("Playing", [
        (["Space"], "Play / pause"),
        (["←", "→", "PgUp", "PgDn"], "Previous / next song (or video in Auto-music)"),
        (["Home", "End"], "First / last"),
        (["-", "="], "Volume down / up"),
        (["0"], "Mute / unmute"),
        (["Enter"], "Play the selected song"),
    ]),
    ("Library", [
        (["Click"], "Clicking anywhere on the playing song's row pauses it; again carries on"),
        (["V"], "Favourite / unfavourite the selected songs"),
        (["A"], "Approve / remove approval"),
        (["E"], "Edit the selected song's audio: trim, volume, re-cut from its video"),
        (["Delete"], "Delete the selected songs (asks first; they go to the Recycle Bin)"),
        (["↑", "↓"], "Move through the list"),
    ]),
    ("Auto-music", [
        (["R"], "Mark the video ready (or not)"),
    ]),
    ("Editor", [
        (["Space"], "Play / pause the edit"),
        (["←", "→"], "One frame back / on"),
        (["Home", "End"], "Start / end of the edit"),
        (["I", "O"], "Trim: the clip starts / ends at the playhead"),
        (["X"], "Split the clip at the playhead"),
        (["Delete"], "Remove the chosen blur box or overlay"),
        (["Ctrl+Z", "Ctrl+Y"], "Undo / redo"),
        (["H"], "Hide the side panels: just the picture and the timeline"),
    ]),
    ("Dialogs", [
        (["Enter"], "The main button"),
        (["Esc"], "Cancel"),
    ]),
    ("Audio editor", [
        (["Space"], "Play / pause from the playhead"),
        (["I", "O"], "Set the start / end at the playhead"),
        (["Click", "drag"], "Move the playhead; drag a handle to move the start or end"),
    ]),
]

COL_W = 340
MARGIN = 24


def _cap(text: str) -> QLabel:
    l = QLabel(text)
    l.setObjectName("keycap")
    l.setAlignment(Qt.AlignCenter)
    return l


class _Row(QWidget):
    """One shortcut: its keycaps, then what it does."""

    def __init__(self, keys: list[str], what: str):
        super().__init__()
        self.setObjectName("transparent")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)
        caps, cl = flow_row(3)          # several keys wrap onto a second line instead of being cut off
        caps.setObjectName("transparent")
        for k in keys:
            cl.addWidget(_cap(k))
        caps.setFixedWidth(124)
        lay.addWidget(caps, 0, Qt.AlignTop)
        w = label(what, wrap=True)
        lay.addWidget(w, 1)


class _Card(QFrame):
    """The popup itself; clicks inside it stay inside."""

    def mousePressEvent(self, e):  # noqa: N802
        e.accept()


class KeysPopup(QWidget):
    closed = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("keysScrim")
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.hide()
        self.card = _Card(self)
        self.card.setObjectName("dialog")
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(18, 14, 18, 16)
        cl.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label("Keyboard shortcuts", "h1"))
        top.addSpacing(10)
        top.addWidget(label("K or Esc closes", "cap"), 0, Qt.AlignBottom)
        top.addStretch(1)
        cl.addLayout(top)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.inner = QWidget()
        self.inner.setObjectName("transparent")
        self.cols = QHBoxLayout(self.inner)
        self.cols.setContentsMargins(0, 0, 0, 0)
        self.cols.setSpacing(22)
        self.scroll.setWidget(self.inner)
        cl.addWidget(self.scroll, 1)
        # items in order: ("h", widget) headings and ("k", widget) rows
        self.items: list[tuple[str, QWidget]] = []
        for group, rows in KEYS:
            h = label(group, "h2")
            h.setContentsMargins(0, 6, 0, 2)
            self.items.append(("h", h))
            for keys, what in rows:
                self.items.append(("k", _Row(keys, what)))
        self._ncols = 0
        parent.installEventFilter(self)

    # ---- open / close --------------------------------------------------------------
    def toggle(self) -> None:
        self.close_popup() if self.isVisible() else self.open_popup()

    def open_popup(self) -> None:
        self.setGeometry(self.parentWidget().rect())
        self._layout(force=True)
        self.show()
        self.raise_()
        self.setFocus()

    def close_popup(self) -> None:
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def mousePressEvent(self, e):  # noqa: N802 - a click outside the card closes it
        self.close_popup()

    def eventFilter(self, obj, ev):
        if obj is self.parentWidget() and ev.type() == QEvent.Resize and self.isVisible():
            self.setGeometry(self.parentWidget().rect())
            self._layout()
        return False

    def paintEvent(self, _e):  # the dim scrim behind the card
        p = QPainter(self)
        c = QColor(C["ground"])
        c.setAlpha(150)
        p.fillRect(self.rect(), c)

    # ---- flowing the columns -------------------------------------------------------------
    def _layout(self, force: bool = False) -> None:
        avail_w = max(COL_W + 40, self.width() - 2 * MARGIN)
        avail_h = max(200, self.height() - 2 * MARGIN)
        chrome_w, chrome_h = 36 + 22, 14 + 16 + 10 + 34
        max_cols = max(1, (avail_w - chrome_w + 22) // (COL_W + 22))
        heights = []
        for _kind, w in self.items:
            w.setFixedWidth(COL_W)
            heights.append(w.heightForWidth(COL_W) if w.hasHeightForWidth() else w.sizeHint().height())
        total = sum(heights)
        # fewest columns whose tallest column still fits; otherwise as many as fit, and scroll up and down
        ncols = max_cols
        for n in range(1, max_cols + 1):
            if self._split_height(heights, n) <= avail_h - chrome_h:
                ncols = n
                break
        if ncols == self._ncols and not force:
            self._place_card(ncols, heights)
            return
        self._ncols = ncols
        for _kind, w in self.items:        # keep the rows: move them out before the old columns go
            w.setParent(self.inner)
        while self.cols.count():
            it = self.cols.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for chunk in self._split(heights, ncols):
            col = QWidget()
            col.setObjectName("transparent")
            v = QVBoxLayout(col)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)
            for i in chunk:
                v.addWidget(self.items[i][1])
            v.addStretch(1)
            col.setFixedWidth(COL_W)
            self.cols.addWidget(col, 0, Qt.AlignTop)
        self._place_card(ncols, heights)
        _ = total

    def _split(self, heights: list[int], n: int) -> list[list[int]]:
        """Cut the items into n columns of similar height. Whole groups move to the next column where they can;
        only a group too tall for any column is split, and never right after its heading."""
        groups: list[list[int]] = []
        for i, (kind, _w) in enumerate(self.items):
            if kind == "h" or not groups:
                groups.append([])
            groups[-1].append(i)
        target = sum(heights) / n
        out: list[list[int]] = [[]]
        h = 0
        for g in groups:
            gh = sum(heights[i] for i in g)
            if out[-1] and h + gh > target * 1.1 and len(out) < n:
                out.append([])
                h = 0
            if gh > target * 1.1 and len(out) < n:      # too tall: split it item by item
                for i in g:
                    if out[-1] and h + heights[i] > target * 1.05 and len(out) < n and self.items[out[-1][-1]][0] != "h":
                        out.append([])
                        h = 0
                    out[-1].append(i)
                    h += heights[i]
            else:
                out[-1].extend(g)
                h += gh
        return [c for c in out if c]

    def _split_height(self, heights: list[int], n: int) -> int:
        return max(sum(heights[i] for i in chunk) for chunk in self._split(heights, n))

    def _place_card(self, ncols: int, heights: list[int]) -> None:
        content_h = self._split_height(heights, ncols)
        w = ncols * COL_W + (ncols - 1) * 22 + 36 + 4
        h = min(content_h + 14 + 16 + 10 + 34 + 6, self.height() - 2 * MARGIN)
        w = min(w, self.width() - 2 * MARGIN)
        self.card.setGeometry(QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h))

    @property
    def columns(self) -> int:
        return self._ncols

