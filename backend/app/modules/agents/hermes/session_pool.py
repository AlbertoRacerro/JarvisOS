"""Lazy app-scoped Hermes workers, one bounded session per Jarvis thread."""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from app.modules.agents.hermes.supervisor import HermesSupervisor


class HermesSessionPool:
    def __init__(self, factory: Callable[[], HermesSupervisor], *, maximum: int = 8,
                 idle_seconds: int = 300) -> None:
        self.factory = factory
        self.maximum = maximum
        self.idle_seconds = idle_seconds
        self._items: OrderedDict[str, HermesSupervisor] = OrderedDict()
        self._timers: dict[str, threading.Timer] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.RLock()

    def for_thread(self, thread_id: str) -> HermesSupervisor:
        with self._guard:
            worker = self._items.get(thread_id)
            if worker is None:
                if len(self._items) >= self.maximum:
                    evicted = False
                    for old_id, old in list(self._items.items()):
                        old_lock = self._locks[old_id]
                        if old_lock.acquire(blocking=False):
                            try:
                                self._stop(old_id, old)
                            finally:
                                old_lock.release()
                            evicted = True
                            break
                    if not evicted:
                        raise RuntimeError("Hermes session capacity is busy")
                worker = self.factory()
                self._items[thread_id] = worker
                self._locks[thread_id] = threading.RLock()
            self._items.move_to_end(thread_id)
            timer = self._timers.pop(thread_id, None)
            if timer:
                timer.cancel()
            return worker

    def lock_for(self, thread_id: str) -> threading.RLock:
        self.for_thread(thread_id)
        return self._locks[thread_id]

    def schedule_idle_stop(self, thread_id: str) -> None:
        with self._guard:
            timer = self._timers.pop(thread_id, None)
            if timer:
                timer.cancel()
            timer = threading.Timer(self.idle_seconds, self._idle_stop, args=(thread_id,))
            timer.daemon = True
            self._timers[thread_id] = timer
            timer.start()

    def _idle_stop(self, thread_id: str) -> None:
        # Hold the guard for the whole stop so for_thread cannot hand out a worker
        # that is being stopped; a for_thread since scheduling replaced this timer.
        with self._guard:
            if self._timers.get(thread_id) is not threading.current_thread():
                return
            lock = self._locks.get(thread_id)
            worker = self._items.get(thread_id)
            if worker is None or lock is None:
                return
            if not lock.acquire(blocking=False):
                self.schedule_idle_stop(thread_id)
                return
            try:
                self._stop(thread_id, worker)
            finally:
                lock.release()

    def _stop(self, thread_id: str, worker: HermesSupervisor) -> None:
        process = worker.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except Exception:
                process.kill()
        worker.session = None
        with self._guard:
            self._items.pop(thread_id, None)
            timer = self._timers.pop(thread_id, None)
            self._locks.pop(thread_id, None)
            if timer:
                timer.cancel()

    def status(self) -> dict[str, object]:
        with self._guard:
            if not self._items:
                from app.modules.agents.hermes.supervisor import UPSTREAM_REVISION
                return {"worker_pid": None, "state": "stopped", "upstream_revision": UPSTREAM_REVISION,
                        "generation": None, "last_error": None}
            return {"threads": {thread_id: worker.status() for thread_id, worker in self._items.items()},
                    "state": "running" if any(worker.status()["state"] == "running"
                                               for worker in self._items.values()) else "stopped"}


def configured_supervisor() -> HermesSupervisor:
    venv = os.getenv("JARVIS_HERMES_VENV", "")
    python = str(Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")) if venv else ""
    return HermesSupervisor(python, route_for_task=lambda _task: os.getenv(
        "JARVIS_HERMES_INFERENCE_ROUTE", "local:llamacpp"))
