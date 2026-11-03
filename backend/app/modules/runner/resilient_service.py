from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from app.core.database import open_sqlite_connection
from app.modules.runner import guarded_service as _guarded
from app.modules.runner.local_python import (
    ExecutionOwnerBusy,
    execution_ownership_state,
    prepare_execution_owner,
)
from app.modules.runner.models import RunnerJobRunResponse
from app.modules.runner.recovery import reconcile_stranded_runner_jobs
from app.modules.runner.safety import RunnerSafetyError, validate_run_paths

OWNER_FAILURE_RECHECK_SECONDS = 0.25


def _reconcile_after_owned_invocation_exit(
    working_dir: Path, *, poll_seconds: float = OWNER_FAILURE_RECHECK_SECONDS
) -> None:
    """Wait for this failed invocation's runner ownership to become conclusive."""

    while True:
        state = execution_ownership_state(working_dir)
        if state == "live":
            time.sleep(poll_seconds)
            continue
        if state == "gone":
            try:
                reconcile_stranded_runner_jobs()
            except sqlite3.OperationalError:
                time.sleep(poll_seconds)
                continue
        return


def _start_owner_failure_followup(working_dir: Path) -> None:
    threading.Thread(
        target=_reconcile_after_owned_invocation_exit,
        args=(working_dir,),
        name="jarvis-runner-owner-recovery",
        daemon=True,
    ).start()


def run_runner_job(runner_job_id: str) -> RunnerJobRunResponse:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT workspace_id, simulation_run_id, working_dir, input_file, output_dir
            FROM runner_jobs
            WHERE id = ?
            """,
            (runner_job_id,),
        ).fetchone()
    if row is None:
        raise RunnerSafetyError("runner_job_not_found", "Runner job not found.")

    working_dir, _, _ = validate_run_paths(
        str(row["workspace_id"]),
        str(row["simulation_run_id"]),
        working_dir=str(row["working_dir"]),
        input_file=str(row["input_file"]),
        output_dir=str(row["output_dir"]),
    )
    owner_entered = False
    try:
        with prepare_execution_owner(working_dir):
            owner_entered = True
            return _guarded.run_runner_job(runner_job_id)
    except ExecutionOwnerBusy as exc:
        raise RunnerSafetyError(
            "runner_job_not_queued",
            "Only one execution owner may claim a queued runner job.",
        ) from exc
    except Exception:
        if owner_entered:
            _start_owner_failure_followup(working_dir)
        raise
