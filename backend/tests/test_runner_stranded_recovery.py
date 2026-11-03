from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI

from app import main
from app.modules.runner import local_python, recovery
from app.modules.runner.local_python import (
    execute_python_script,
    execution_ownership_state,
    prepare_execution_owner,
)


def _wait_until(predicate, *, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for runner ownership test condition.")


def test_execution_owner_is_live_through_child_completion(tmp_path: Path) -> None:
    working_dir = tmp_path / "run"
    working_dir.mkdir()
    script = working_dir / "script.py"
    script.write_text("print('owner-proof')\n", encoding="utf-8")
    input_file = working_dir / "input.json"
    input_file.write_text("{}", encoding="utf-8")
    output_dir = working_dir / "output"
    output_dir.mkdir()

    assert execution_ownership_state(working_dir) == "unknown"
    with prepare_execution_owner(working_dir):
        assert execution_ownership_state(working_dir) == "live"
        result = execute_python_script(
            script_path=script,
            input_file=input_file,
            output_dir=output_dir,
            working_dir=working_dir,
            timeout_seconds=10,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        )
        assert result.return_code == 0
        assert result.timed_out is False
        assert result.stdout == "owner-proof\n"
        # The owner remains live after the model child exits so durable DB
        # finalization cannot be mistaken for an abandoned execution.
        assert execution_ownership_state(working_dir) == "live"

    assert execution_ownership_state(working_dir) == "gone"


def test_execution_owner_survives_abrupt_parent_until_model_child_exits(tmp_path: Path) -> None:
    working_dir = tmp_path / "run"
    working_dir.mkdir()
    input_file = working_dir / "input.json"
    input_file.write_text("{}", encoding="utf-8")
    output_dir = working_dir / "output"
    output_dir.mkdir()
    started = output_dir / "child-started"
    completed = output_dir / "child-completed"
    script = working_dir / "slow.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "out = Path(sys.argv[2])\n"
        "(out / 'child-started').write_text('started', encoding='utf-8')\n"
        "time.sleep(4)\n"
        "(out / 'child-completed').write_text('completed', encoding='utf-8')\n",
        encoding="utf-8",
    )

    parent_code = """
from pathlib import Path
import sys
from app.modules.runner.local_python import execute_python_script, prepare_execution_owner
working_dir = Path(sys.argv[1])
with prepare_execution_owner(working_dir):
    execute_python_script(
        script_path=working_dir / 'slow.py',
        input_file=working_dir / 'input.json',
        output_dir=working_dir / 'output',
        working_dir=working_dir,
        timeout_seconds=10,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
    )
"""
    backend_root = Path(__file__).resolve().parents[1]
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(working_dir)],
        cwd=str(backend_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until(started.exists)
        assert execution_ownership_state(working_dir) == "live"

        parent.kill()
        parent.wait(timeout=5)

        assert execution_ownership_state(working_dir) == "live"
        _wait_until(completed.exists)
        _wait_until(lambda: execution_ownership_state(working_dir) == "gone")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def test_execution_child_keeps_liveness_after_owner_helper_death(tmp_path: Path) -> None:
    working_dir = tmp_path / "run"
    working_dir.mkdir()
    input_file = working_dir / "input.json"
    input_file.write_text("{}", encoding="utf-8")
    output_dir = working_dir / "output"
    output_dir.mkdir()
    started = output_dir / "child-started"
    completed = output_dir / "child-completed"
    script = working_dir / "slow.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "out = Path(sys.argv[2])\n"
        "(out / 'child-started').write_text('started', encoding='utf-8')\n"
        "time.sleep(4)\n"
        "(out / 'child-completed').write_text('completed', encoding='utf-8')\n",
        encoding="utf-8",
    )

    owner_script = Path(local_python.__file__).with_name("_execution_owner.py")
    lock_path = local_python.ownership_lock_path(working_dir)
    owner = subprocess.Popen(
        [sys.executable, str(owner_script), str(lock_path)],
        cwd=str(working_dir.parent),
        env={"PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert owner.stdout is not None
        assert json.loads(owner.stdout.readline()) == {"state": "ready"}
        assert owner.stdin is not None
        request = {
            "action": "run",
            "command": [sys.executable, str(script), str(input_file), str(output_dir)],
            "env": {"PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"},
            "cwd": str(working_dir),
            "timeout_seconds": 10,
            "max_stdout_bytes": 4096,
            "max_stderr_bytes": 4096,
        }
        owner.stdin.write(json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
        owner.stdin.flush()

        _wait_until(started.exists)
        assert execution_ownership_state(working_dir) == "live"

        owner.kill()
        owner.wait(timeout=5)

        # The actual execution process retains its own OS lock, so helper
        # failure cannot turn live work into a false abandoned signal.
        assert execution_ownership_state(working_dir) == "live"
        _wait_until(completed.exists)
        _wait_until(lambda: execution_ownership_state(working_dir) == "gone")
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


def test_recovery_requires_coherent_running_pair_and_proven_owner_loss(monkeypatch) -> None:
    rows = [
        {
            "runner_job_id": "gone",
            "workspace_id": "w",
            "simulation_run_id": "s-gone",
            "runner_status": "running",
            "simulation_status": "running",
            "working_dir": "/gone",
        },
        {
            "runner_job_id": "live",
            "workspace_id": "w",
            "simulation_run_id": "s-live",
            "runner_status": "running",
            "simulation_status": "running",
            "working_dir": "/live",
        },
        {
            "runner_job_id": "unknown",
            "workspace_id": "w",
            "simulation_run_id": "s-unknown",
            "runner_status": "running",
            "simulation_status": "running",
            "working_dir": "/unknown",
        },
        {
            "runner_job_id": "inconsistent",
            "workspace_id": "w",
            "simulation_run_id": "s-inconsistent",
            "runner_status": "running",
            "simulation_status": "failed",
            "working_dir": "/gone-inconsistent",
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

    states = {
        "/gone": "gone",
        "/live": "live",
        "/unknown": "unknown",
        "/gone-inconsistent": "gone",
    }
    recovered: list[str] = []

    monkeypatch.setattr(recovery, "open_sqlite_connection", _open_connection)
    monkeypatch.setattr(recovery, "execution_ownership_state", lambda path: states[str(path)])
    monkeypatch.setattr(
        recovery,
        "_recover_pair",
        lambda **kwargs: recovered.append(str(kwargs["runner_job_id"])) is None or True,
    )

    assert recovery.reconcile_stranded_runner_jobs() == 1
    assert recovered == ["gone"]


def test_app_lifespan_runs_recovery_after_runtime_startup(monkeypatch) -> None:
    calls: list[str] = []

    class _Lifecycle:
        async def startup(self) -> None:
            calls.append("runtime-startup")

        async def shutdown(self) -> None:
            calls.append("runtime-shutdown")

    monkeypatch.setattr(main, "create_local_ai_runtime_lifecycle_from_env", lambda: _Lifecycle())
    monkeypatch.setattr(main, "reconcile_stranded_runner_jobs", lambda: calls.append("runner-recovery"))

    async def _exercise() -> None:
        app = FastAPI()
        async with main.lifespan(app):
            calls.append("serving")

    asyncio.run(_exercise())
    assert calls == ["runtime-startup", "runner-recovery", "serving", "runtime-shutdown"]
