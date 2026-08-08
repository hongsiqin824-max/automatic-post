"""Small scheduler that shares the web run controller's single-flight lock."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone


logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, controller, interval_seconds: int):
        self.controller = controller
        self.interval_seconds = interval_seconds
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._enabled = False
        self._next_run_at: str | None = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def next_run_at(self) -> str | None:
        with self._lock:
            return self._next_run_at

    def _schedule_next_locked(self) -> None:
        next_run = datetime.now(timezone.utc) + timedelta(seconds=self.interval_seconds)
        self._next_run_at = next_run.isoformat(timespec="seconds").replace("+00:00", "Z")

    def start(self) -> None:
        with self._lock:
            self._enabled = True
            self._schedule_next_locked()
            if self._thread and self._thread.is_alive():
                return
            self._shutdown.clear()
            self._thread = threading.Thread(target=self._loop, name="automatic-post-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Pause future runs; an already-started run is allowed to finish."""

        with self._lock:
            self._enabled = False
            self._next_run_at = None
            self._wake.clear()

    def trigger(self) -> bool:
        with self._lock:
            if not self._enabled:
                return False
            self._wake.set()
            return True

    def shutdown(self, timeout: float | None = 2.0) -> bool:
        """Stop the scheduler thread and report whether it exited in time."""

        with self._lock:
            self._enabled = False
            self._next_run_at = None
            self._shutdown.set()
            self._wake.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def _loop(self) -> None:
        # Do not immediately call the remote API on a fresh process. The UI can
        # trigger the first run, and the first scheduled run follows the interval.
        while not self._shutdown.is_set():
            self._wake.wait(self.interval_seconds)
            with self._lock:
                self._wake.clear()
                if self._shutdown.is_set():
                    return
                if not self._enabled:
                    continue
                self._schedule_next_locked()
                # Keep the scheduler lock through the quick start decision so
                # stop() guarantees no new run begins after it returns.
                try:
                    self.controller.start()
                except Exception:  # noqa: BLE001 - keep future schedule ticks alive
                    logger.exception("定时任务启动失败")
