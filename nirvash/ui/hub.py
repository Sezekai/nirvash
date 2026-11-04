"""App-wide signals shared by the pages."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class Hub(QObject):
    songChanged = Signal(int)        # one song changed (or was added)
    songsReset = Signal()            # reload everything
    identifyStatus = Signal(int, str)
    categoriesChanged = Signal()
    modelsChanged = Signal()
    logLine = Signal(str, str)
    toast = Signal(str, str)         # message, kind (info|ok|warn|error)


hub = Hub()
