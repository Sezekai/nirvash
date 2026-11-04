"""Run background jobs off the UI thread and report back with Qt signals."""
from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from ..core import log as _log
from ..jobs.context import JobContext

log = _log.get("ui")


class _Signals(QObject):
    progress = Signal(float, str)
    event = Signal(str, object)
    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()


class Job(QRunnable):
    """fn(ctx, *args) runs in the thread pool; connect to .sig before start()."""

    def __init__(self, fn: Callable[..., Any], *args: Any):
        super().__init__()
        self.setAutoDelete(False)
        self.fn, self.args = fn, args
        self.sig = _Signals()
        self.ctx = JobContext(lambda f, m: self.sig.progress.emit(f, m), lambda k, p: self.sig.event.emit(k, p))
        self.running = False

    def run(self) -> None:
        self.running = True
        try:
            res = self.fn(self.ctx, *self.args)
            if self.ctx.cancel.is_set():
                self.sig.cancelled.emit()
            else:
                self.sig.done.emit(res)
        except InterruptedError:
            self.sig.cancelled.emit()
        except Exception as e:
            log.error("%s failed: %s\n%s", getattr(self.fn, "__name__", "job"), e, traceback.format_exc())
            self.sig.failed.emit(str(e) or e.__class__.__name__)
        finally:
            self.running = False
            self.sig.finished.emit()

    def cancel(self) -> None:
        self.ctx.cancel.set()


_pool: QThreadPool | None = None
_alive: set = set()


def pool() -> QThreadPool:
    global _pool
    if _pool is None:
        _pool = QThreadPool()
        _pool.setMaxThreadCount(4)
    return _pool


def start(job: Job) -> Job:
    _alive.add(job)
    job.sig.finished.connect(lambda: _alive.discard(job))
    pool().start(job)
    return job


def run_bg(fn: Callable[..., Any], *args: Any, done: Callable[[Any], None] | None = None,
           failed: Callable[[str], None] | None = None) -> Job:
    """Quick helper for small background calls that don't need a context."""
    job = Job(lambda ctx, *a: fn(*a), *args)
    if done:
        job.sig.done.connect(done)
    if failed:
        job.sig.failed.connect(failed)
    return start(job)
