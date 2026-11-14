"""App-wide signals shared by the pages."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class Hub(QObject):
    songChanged = Signal(int)        # one song changed (or was added)
    songsReset = Signal()            # reload everything
    identifyStatus = Signal(dict)       # pending, current, done, started, busy
    categoriesChanged = Signal()
    modelsChanged = Signal()
    logLine = Signal(str, str)
    toast = Signal(str, str)         # message, kind (info|ok|warn|error)
    movesChanged = Signal()          # the move log changed
    themeChanged = Signal()          # the look changed (Appearance)
    editVideos = Signal(list)        # open these videos in the editor


hub = Hub()
