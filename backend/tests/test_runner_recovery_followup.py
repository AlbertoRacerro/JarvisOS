from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app import main
from app.modules.runner import recovery


def test_live_stranded_paths_include_only_coherent_live_owners(monkeypatch) -> None:
    rows = [
        {
            "runner_job_id": "live",
            "workspace_id": "w",
            "simulation_run_id": "s-live",
            "runner_status": "running",
            "simulation_status": "running",
            "working_dir": "/live",
        },
        {
            "runner_job_id": "gone",
            "workspace_id": "w",
            "simulation_run_id": "s-gone",
            "runner_status": "running",
            "simulation_status": "running",
            "working_dir": "/gone",
        },
        {
            "runner_job_id": "inconsistent",
            "workspace_id": "w",
            "simulation_run_id": "s-bad",
            "runner_status": "running",
            "simulation_status": "failed",
            "working_dir": "/live-bad",
        },
    ]

    class _Selection:
        def fetchall(self):
            return rows

    class _Connection:
        def execute(self, _query):
            return _Selection()

    @contextmanager
    def _open_connection():
        yield _Connection()

    states = {"/live": "live", "/gone": "gone", "/live-bad": "live"}
    monkeypatch.setattr(recovery, "open_sqlite_connection", _open_connection)
    monkeypatch.setattr(recovery, "execution_ownership_state", lambda path: states[str(path)])

    assert recovery.live_stranded_runner_working_dirs() == (Path("/live"),)


def test_startup_live_owner_is_reconciled_after_it_becomes_gone(monkeypatch) -> None:
    working_dir = Path("/orphan")
    states = iter(("live", "gone"))
    reconciliations: list[str] = []

    monkeypatch.setattr(main, "execution_ownership_state", lambda _path: next(states))
    monkeypatch.setattr(
        main,
        "reconcile_stranded_runner_jobs",
        lambda: reconciliations.append("reconciled") or 1,
    )

    asyncio.run(
        main._reconcile_after_live_owners_exit((working_dir,), poll_seconds=0.0)
    )

    assert reconciliations == ["reconciled"]


def test_startup_live_owner_that_becomes_unknown_fails_closed(monkeypatch) -> None:
    reconciliations: list[str] = []
    monkeypatch.setattr(main, "execution_ownership_state", lambda _path: "unknown")
    monkeypatch.setattr(
        main,
        "reconcile_stranded_runner_jobs",
        lambda: reconciliations.append("unexpected") or 1,
    )

    asyncio.run(
        main._reconcile_after_live_owners_exit((Path("/orphan"),), poll_seconds=0.0)
    )

    assert reconciliations == []


def test_startup_live_owner_retries_after_transient_database_failure(monkeypatch) -> None:
    attempts = 0
    monkeypatch.setattr(main, "execution_ownership_state", lambda _path: "gone")

    def _reconcile() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return 1

    monkeypatch.setattr(main, "reconcile_stranded_runner_jobs", _reconcile)

    asyncio.run(
        main._reconcile_after_live_owners_exit((Path("/orphan"),), poll_seconds=0.0)
    )

    assert attempts == 2
