"""Keys: every keyboard shortcut (K)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from .widgets import label, page_header, panel

KEYS = [
    ("Everywhere", [
        ("K", "This list of keys"),
        ("/ or Ctrl+F", "Search (the library's search box)"),
        ("F or F11", "Full screen"),
        ("H", "Hide the panels so only the list or video shows (H or Esc brings them back)"),
        ("L", "Open or close the log"),
        ("M", "Show or hide the rail on the left"),
        ("S", "Settings"),
        ("Ctrl+Z", "Undo the last rename or move"),
        ("Esc", "Close what's open: a dialog, then hidden panels, full screen, then the log"),
    ]),
    ("Playing", [
        ("Space", "Play / pause"),
        ("← →, PgUp PgDn", "Previous / next song (or video in Auto-music)"),
        ("Home, End", "First / last"),
        ("↑ ↓", "Move through the list; volume when the player has focus"),
        ("Enter", "Play the selected song"),
    ]),
    ("Library", [
        ("V", "Favourite / unfavourite the selected songs"),
        ("A", "Approve / remove approval"),
        ("Delete", "Delete the selected songs (asks first; they go to the Recycle Bin)"),
    ]),
    ("Auto-music", [
        ("R", "Mark the video ready (or not)"),
    ]),
]


class KeysPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)
        head, _ = page_header("Keys", "Keyboard shortcuts. Letters do nothing while you're typing in a box.")
        root.addLayout(head)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        inner.setObjectName("transparent")
        grid = QGridLayout(inner)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        for n, (title, rows) in enumerate(KEYS):
            p = panel()
            pl = QVBoxLayout(p)
            pl.setContentsMargins(12, 10, 12, 12)
            pl.setSpacing(4)
            pl.addWidget(label(title, "h2"))
            for k, what in rows:
                r = QHBoxLayout()
                r.setSpacing(12)
                kl = QLabel(k)
                kl.setObjectName("mono")
                kl.setFixedWidth(130)
                r.addWidget(kl)
                wl = label(what, wrap=True)
                r.addWidget(wl, 1)
                pl.addLayout(r)
            pl.addStretch(1)
            grid.addWidget(p, n // 2, n % 2)
        grid.setRowStretch(len(KEYS), 1)
        sc.setWidget(inner)
        root.addWidget(sc, 1)
