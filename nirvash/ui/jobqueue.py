"""One queue for every long job (scans, matching, exports, renames, deletes, downloads, moves).

Starting work while something runs adds it to the queue instead of refusing. One status bar shows the job that's
running and how many more are waiting; Stop stops it and empties the queue, keeping finished work (STANDARDS.md,
"A queue, not busy" and "Progress you can trust")."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from ..core import log as _log
from . import workers

log = _log.get("queue")


@dataclass
class QueuedJob:
    name: str                                   # "Finding music"
    fn: Callable[..., Any]
    args: tuple
    detail: str = ""                            # what it's working on now
    pausable: bool = False
    ai: bool = False                            # uses the AI models (shows the hardware chip)
    sized: bool = True                          # False: no total known, show a sliding bar
    on_start: Callable[[], None] | None = None
    on_event: Callable[[str, Any], None] | None = None
    on_progress: Callable[[float, str], None] | None = None
    on_done: Callable[[Any], None] | None = None
    on_failed: Callable[[str], None] | None = None
    on_stopped: Callable[[], None] | None = None
    on_finished: Callable[[], None] | None = None
    done_text: Callable[[Any], str] | None = None
    state: str = "queued"                       # queued | running | done | failed | stopped
    frac: float = 0.0
    started: float = 0.0
    paused_for: float = 0.0
    paused_at: float = 0.0
    last_change: float = 0.0
    result_text: str = ""
    job: workers.Job | None = field(default=None, repr=False)

    @property
    def paused(self) -> bool:
        return bool(self.job and self.job.ctx.paused)

    def elapsed(self, now: float | None = None) -> float:
        now = now or time.time()
        if not self.started:
            return 0.0
        held = self.paused_for + ((now - self.paused_at) if self.paused_at else 0.0)
        return max(0.0, now - self.started - held)


class JobQueue(QObject):
    changed = Signal()            # a job started, finished or was queued
    progressed = Signal()         # the running job moved on
    finishedJob = Signal(object)  # QueuedJob that just ended

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current: QueuedJob | None = None
        self.waiting: list[QueuedJob] = []
        self.last: QueuedJob | None = None

    # ---- adding --------------------------------------------------------------
    def add(self, name: str, fn: Callable[..., Any], *args: Any, **kw: Any) -> QueuedJob:
        qj = QueuedJob(name, fn, args, **kw)
        self.waiting.append(qj)
        log.info("queued: %s", name)
        if self.current is None:
            self._next()
        else:
            self.changed.emit()
        return qj

    def busy(self) -> bool:
        return self.current is not None

    def count(self) -> int:
        return (1 if self.current else 0) + len(self.waiting)

    # ---- control ---------------------------------------------------------------
    def stop_all(self) -> None:
        """Stop the running job and empty the queue. Finished work is kept."""
        dropped = self.waiting
        self.waiting = []
        for qj in dropped:
            qj.state = "stopped"
            if qj.on_stopped:
                qj.on_stopped()
            if qj.on_finished:
                qj.on_finished()
        if self.current and self.current.job:
            self.current.job.cancel()
            self.pause(False)
        self.changed.emit()

    def stop_current(self) -> None:
        if self.current and self.current.job:
            self.current.job.cancel()
            self.pause(False)

    def pause(self, on: bool) -> None:
        qj = self.current
        if not qj or not qj.job:
            return
        if on and not qj.paused:
            qj.paused_at = time.time()
            qj.job.ctx.pause(True)
        elif not on and qj.paused:
            qj.paused_for += time.time() - qj.paused_at
            qj.paused_at = 0.0
            qj.last_change = time.time()
            qj.job.ctx.pause(False)
        self.changed.emit()

    # ---- running ---------------------------------------------------------------
    def _next(self) -> None:
        if self.current is not None or not self.waiting:
            self.changed.emit()
            return
        qj = self.waiting.pop(0)
        self.current = qj
        qj.state = "running"
        qj.started = qj.last_change = time.time()
        job = workers.Job(qj.fn, *qj.args)
        qj.job = job

        def prog(f: float, m: str, qj=qj):
            f = max(0.0, min(0.99, float(f)))            # 100% only once it has truly finished
            if f > qj.frac + 1e-6 or (m and m != qj.detail):
                qj.last_change = time.time()
            qj.frac = max(qj.frac, f)
            if m:
                qj.detail = m
            if qj.on_progress:
                qj.on_progress(f, m)
            self.progressed.emit()

        job.sig.progress.connect(prog)
        if qj.on_event:
            job.sig.event.connect(lambda k, p, qj=qj: qj.on_event(k, p))
        job.sig.done.connect(lambda res, qj=qj: self._ended(qj, "done", res))
        job.sig.failed.connect(lambda msg, qj=qj: self._ended(qj, "failed", msg))
        job.sig.cancelled.connect(lambda qj=qj: self._ended(qj, "stopped", None))
        log.info("started: %s", qj.name)
        if qj.on_start:
            try:
                qj.on_start()
            except Exception as e:
                log.error("starting %s: %s", qj.name, e)
        self.changed.emit()
        workers.start(job)

    def _ended(self, qj: QueuedJob, state: str, payload) -> None:
        if qj.state != "running":
            return
        qj.state = state
        took = qj.elapsed()
        from .widgets import fmt_dur
        if state == "done":
            qj.frac = 1.0
            try:
                txt = qj.done_text(payload) if qj.done_text else ""
            except Exception:
                txt = ""
            qj.result_text = f"Finished{': ' + txt if txt else ''} · took {fmt_dur(took)}"
            cb = qj.on_done
            args = (payload,)
        elif state == "failed":
            qj.result_text = f"{qj.name} failed: {payload} · the log has the details"
            cb = qj.on_failed
            args = (payload,)
        else:
            qj.result_text = f"Stopped {qj.name.lower()} · took {fmt_dur(took)}. Finished work is kept."
            cb = qj.on_stopped
            args = ()
        log.info("%s: %s", state, qj.name)
        try:
            if cb:
                cb(*args)
        except Exception as e:  # a page callback must never jam the queue
            log.error("after %s: %s", qj.name, e)
        try:
            if qj.on_finished:
                qj.on_finished()
        except Exception as e:
            log.error("after %s: %s", qj.name, e)
        self.current = None
        self.last = qj
        self.finishedJob.emit(qj)
        self._next()


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue()
    return _queue
