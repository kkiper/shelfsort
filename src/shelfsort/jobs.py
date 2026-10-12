"""One-at-a-time background job runner."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shelfsort import db

ProgressFn = Callable[[str, int, int], None]


@dataclass
class JobStatus:
    name: str = ""
    stage: str = ""
    done: int = 0
    total: int = 0
    error: str | None = None
    running: bool = False


class Worker:
    """Runs `fn(conn, progress)` in a daemon thread with its own DB connection."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._status = JobStatus()
        self._thread: threading.Thread | None = None

    def start(self, name: str, fn: Callable[[Any, ProgressFn], Any]) -> bool:
        with self._lock:
            if self._status.running:
                return False
            self._status = JobStatus(name=name, running=True)
            self._thread = threading.Thread(
                target=self._run, args=(fn,), name=f"job-{name}", daemon=True)
            self._thread.start()
            return True

    def status(self) -> JobStatus:
        with self._lock:
            return JobStatus(**vars(self._status))

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _progress(self, stage: str, done: int, total: int) -> None:
        with self._lock:
            self._status.stage, self._status.done, self._status.total = stage, done, total

    def _run(self, fn: Callable[[Any, ProgressFn], Any]) -> None:
        error = None
        try:
            conn = db.connect(self._db_path)
            try:
                fn(conn, self._progress)
            finally:
                conn.close()
        except BaseException as exc:  # a job must never take the server down
            error = str(exc) or type(exc).__name__
        with self._lock:
            self._status.error = error
            self._status.running = False
