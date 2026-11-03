from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.modules.runner import resilient_service
from app.modules.runner.local_python import ExecutionOwnerBusy
from app.modules.runner.safety import RunnerSafetyError


def _runner_row(working_dir: Path) -> dict[str, str]:
    return {
        "workspace_id": "workspace-1",
        "simulation_run_id": "run-1",
        "working_dir": str(working_dir),
        "input_file": str(working_dir / "input.json"),
        "output_dir": str(working_dir / "output"),
    }


def _validated_paths(
    _workspace_id: str,
    _simulation_run_id: str,
    *,
    working_dir: str,
    input_file: str,
    output_dir: str,
) -> tuple[Path, Path, Path]:
    return Path(working_dir), Path(input_file), Path(output_dir)


def test_owned_invocation_failure_reconciles_after_child_ownership_is_gone(monkeypatch) -> None:
    states = iter(("live", "gone"))
    reconciliations: list[str] = []

    monkeypatch.setattr(
        resilient_service,
        "execution_ownership_state",
        lambda _path: next(states),
    )
    monkeypatch.setattr(resilient_service.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        resilient_service,
        "reconcile_stranded_runner_jobs",
        lambda: reconciliations.append("reconciled") or 1,
    )

    resilient_service._reconcile_after_owned_invocation_exit(
        Path("/owned-run"), poll_seconds=0.0
    )

    assert reconciliations == ["reconciled"]


def test_owned_invocation_failure_retries_transient_reconcile_lock(monkeypatch) -> None:
    attempts: list[str] = []

    monkeypatch.setattr(
        resilient_service,
        "execution_ownership_state",
        lambda _path: "gone",
    )
    monkeypatch.setattr(resilient_service.time, "sleep", lambda _seconds: None)

    def _reconcile() -> int:
        attempts.append("attempt")
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return 1

    monkeypatch.setattr(resilient_service, "reconcile_stranded_runner_jobs", _reconcile)

    resilient_service._reconcile_after_owned_invocation_exit(
        Path("/owned-run"), poll_seconds=0.0
    )

    assert attempts == ["attempt", "attempt"]


def test_owned_invocation_failure_with_unknown_ownership_fails_closed(monkeypatch) -> None:
    reconciliations: list[str] = []
    monkeypatch.setattr(
        resilient_service,
        "execution_ownership_state",
        lambda _path: "unknown",
    )
    monkeypatch.setattr(
        resilient_service,
        "reconcile_stranded_runner_jobs",
        lambda: reconciliations.append("unexpected") or 1,
    )

    resilient_service._reconcile_after_owned_invocation_exit(Path("/owned-run"))

    assert reconciliations == []


def test_exception_after_owner_entry_registers_bounded_recovery_followup(monkeypatch) -> None:
    working_dir = Path("/run-root")
    followups: list[Path] = []

    class _Selection:
        def fetchone(self):
            return _runner_row(working_dir)

    class _Connection:
        def execute(self, _query, _params):
            return _Selection()

    @contextmanager
    def _open_connection():
        yield _Connection()

    @contextmanager
    def _owner(_working_dir):
        yield

    monkeypatch.setattr(resilient_service, "open_sqlite_connection", _open_connection)
    monkeypatch.setattr(resilient_service, "validate_run_paths", _validated_paths)
    monkeypatch.setattr(resilient_service, "prepare_execution_owner", _owner)
    monkeypatch.setattr(
        resilient_service._guarded,
        "run_runner_job",
        lambda _runner_job_id: (_ for _ in ()).throw(RuntimeError("owner helper exited")),
    )
    monkeypatch.setattr(
        resilient_service,
        "_start_owner_failure_followup",
        lambda path: followups.append(path),
    )

    with pytest.raises(RuntimeError, match="owner helper exited"):
        resilient_service.run_runner_job("job-1")

    assert followups == [working_dir]


def test_busy_before_owner_entry_does_not_register_recovery_followup(monkeypatch) -> None:
    working_dir = Path("/run-root")
    followups: list[Path] = []

    class _Selection:
        def fetchone(self):
            return _runner_row(working_dir)

    class _Connection:
        def execute(self, _query, _params):
            return _Selection()

    @contextmanager
    def _open_connection():
        yield _Connection()

    @contextmanager
    def _busy_owner(_working_dir):
        raise ExecutionOwnerBusy("busy")
        yield

    monkeypatch.setattr(resilient_service, "open_sqlite_connection", _open_connection)
    monkeypatch.setattr(resilient_service, "validate_run_paths", _validated_paths)
    monkeypatch.setattr(resilient_service, "prepare_execution_owner", _busy_owner)
    monkeypatch.setattr(
        resilient_service,
        "_start_owner_failure_followup",
        lambda path: followups.append(path),
    )

    with pytest.raises(RunnerSafetyError) as exc_info:
        resilient_service.run_runner_job("job-1")

    assert exc_info.value.code == "runner_job_not_queued"
    assert followups == []


def test_invalid_paths_are_rejected_before_execution_owner_lock(monkeypatch) -> None:
    working_dir = Path("/outside-run-root")
    owner_calls: list[Path] = []

    class _Selection:
        def fetchone(self):
            return _runner_row(working_dir)

    class _Connection:
        def execute(self, _query, _params):
            return _Selection()

    @contextmanager
    def _open_connection():
        yield _Connection()

    def _reject_paths(*_args, **_kwargs):
        raise RunnerSafetyError("runner_path_invalid", "Runner path is outside the data root.")

    @contextmanager
    def _owner(path):
        owner_calls.append(path)
        yield

    monkeypatch.setattr(resilient_service, "open_sqlite_connection", _open_connection)
    monkeypatch.setattr(resilient_service, "validate_run_paths", _reject_paths)
    monkeypatch.setattr(resilient_service, "prepare_execution_owner", _owner)

    with pytest.raises(RunnerSafetyError) as exc_info:
        resilient_service.run_runner_job("job-1")

    assert exc_info.value.code == "runner_path_invalid"
    assert owner_calls == []
