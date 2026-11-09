"""Progress / cancel / pause / event plumbing shared by background jobs (no Qt here)."""
from __future__ import annotations

import threading
from typing import Any, Callable


class JobContext:
    def __init__(self, on_progress: Callable[[float, str], None] | None = None,
                 on_event: Callable[[str, Any], None] | None = None):
        self.cancel = threading.Event()
        self.running = threading.Event()      # cleared while paused
        self.running.set()
        self._p = on_progress
        self._e = on_event

    def progress(self, frac: float, msg: str = "") -> None:
        if self._p:
            self._p(max(0.0, min(1.0, frac)), msg)

    def event(self, kind: str, payload: Any = None) -> None:
        if self._e:
            self._e(kind, payload)

    def check(self) -> None:
        """Stop here if the job was stopped; wait here while it's paused."""
        while not self.running.is_set():
            if self.cancel.is_set():
                break
            self.running.wait(0.25)
        if self.cancel.is_set():
            raise InterruptedError("cancelled")

    def pause(self, on: bool) -> None:
        (self.running.clear if on else self.running.set)()

    @property
    def paused(self) -> bool:
        return not self.running.is_set()
