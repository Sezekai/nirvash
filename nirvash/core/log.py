"""Logging to a file in the data folder plus listeners (the in-app log drawer)."""
from __future__ import annotations

import logging
import logging.handlers
import threading
from typing import Callable

from . import paths

_listeners: list[Callable[[str, str], None]] = []
_lock = threading.Lock()


class _ListenerHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        with _lock:
            ls = list(_listeners)
        for fn in ls:
            try:
                fn(record.levelname, msg)
            except Exception:
                pass


def setup() -> logging.Logger:
    log = logging.getLogger("nirvash")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    d = paths.ensure(paths.data_dir() / "logs")
    fh = logging.handlers.RotatingFileHandler(d / "nirvash.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(fh)
    lh = _ListenerHandler()
    lh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    log.addHandler(lh)
    return log


def clear() -> int:
    """Empty the log files (they name the videos that were scanned). Returns how many files were cleared."""
    n = 0
    d = paths.data_dir() / "logs"
    log = logging.getLogger("nirvash")
    for h in log.handlers:
        if isinstance(h, logging.FileHandler):
            h.acquire()
            try:
                if h.stream:
                    h.stream.seek(0)
                    h.stream.truncate()
                    n += 1
            finally:
                h.release()
    for f in list(d.glob("nirvash.log.*")) + [d / "crash.log"]:
        try:
            if f.exists():
                f.write_text("", encoding="utf-8") if f.name == "crash.log" else f.unlink()
                n += 1
        except OSError:
            pass
    if not log.handlers and (d / "nirvash.log").exists():
        (d / "nirvash.log").write_text("", encoding="utf-8")
        n += 1
    return n


def add_listener(fn: Callable[[str, str], None]) -> None:
    with _lock:
        _listeners.append(fn)


def get(name: str = "") -> logging.Logger:
    setup()
    return logging.getLogger("nirvash" + ("." + name if name else ""))
