"""Incident 847: distinguish ledger/tool waits from a spinning Relay renderer.

All writes and subprocesses belong to these tests. A completed ledger append can
precede delivery of its ID: stalled output must never trigger a blind retry.
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "devctx.py"
MODULE_SPEC = importlib.util.spec_from_file_location("devctx_incident847", SCRIPT)
assert MODULE_SPEC and MODULE_SPEC.loader
devctx = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = devctx
MODULE_SPEC.loader.exec_module(devctx)


@pytest.fixture()
def cli(tmp_path: Path) -> tuple[list[str], dict[str, str], Path]:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=5)
    control = tmp_path / "control"
    control.mkdir()
    env = {"PATH": os.defpath, "JARVIS_CONTROL_DIR": str(control), "DEVCTX_AUTHOR": "incident847-test"}
    command = [sys.executable, str(SCRIPT), "--repo", str(repo), "note", "attempt"]
    return command, env, control / "ledger.jsonl"


def wait_until(predicate: Callable[[], bool], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("owned subprocess did not reach the expected state within the deadline")
        time.sleep(0.01)


@contextmanager
def owned_process(command: list[str], env: dict[str, str], **kwargs) -> Iterator[subprocess.Popen]:
    process = subprocess.Popen(command, env=env, stderr=subprocess.PIPE, **kwargs)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_concurrent_cli_writers_preserve_every_complete_record(cli) -> None:
    command, env, ledger = cli

    def write(index: int) -> tuple[str, str]:
        text = f"isolated Lumen Light v2.1 attempt {index}"
        completed = subprocess.run(
            [*command, text], env=env, capture_output=True, text=True, timeout=10, check=True,
        )
        assert completed.stderr == ""
        return completed.stdout.strip(), text

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        expected = dict(pool.map(write, range(24)))
    actual = records(ledger)
    assert len(actual) == len(expected) == 24
    assert {item["id"]: item["text"] for item in actual} == expected


def test_waiting_ledger_writer_interrupts_without_appending(cli) -> None:
    command, env, ledger = cli
    with ledger.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with owned_process([*command, "waiting attempt"], env, stdout=subprocess.PIPE) as process:
            wait_until(lambda: "locks_lock" in Path(f"/proc/{process.pid}/wchan").read_text())
            assert process.poll() is None
            assert ledger.read_bytes() == b""
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode in (-signal.SIGINT, 128 + signal.SIGINT)
            assert stdout == b""
            assert b"KeyboardInterrupt" in stderr
            assert ledger.read_bytes() == b""
        fcntl.flock(holder, fcntl.LOCK_UN)
    completed = subprocess.run(
        [*command, "explicit recovered attempt"], env=env, capture_output=True, text=True, timeout=5, check=True,
    )
    assert [row["id"] for row in records(ledger)] == [completed.stdout.strip()]


def test_long_running_child_completes_and_stuck_child_is_bounded(tmp_path: Path) -> None:
    code, output = devctx.run(
        [sys.executable, "-c", "import time; time.sleep(0.15); print('completed')"], timeout=5,
    )
    assert (code, output) == (0, "completed\n")
    pid_file = tmp_path / "owned-child.pid"
    started = time.monotonic()
    code, output = devctx.run(
        [sys.executable, "-c",
         "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)",
         str(pid_file)], timeout=0.5,
    )
    assert code is None and "timed out" in output
    assert time.monotonic() - started < 5
    assert pid_file.exists()
    assert not Path(f"/proc/{pid_file.read_text()}").exists()


def test_stalled_output_preserves_committed_append_without_repeating_it(cli) -> None:
    command, env, ledger = cli
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        os.set_blocking(write_fd, True)
        with owned_process([*command, "completed before output stalled"], env, stdout=write_fd) as process:
            wait_until(lambda: ledger.exists() and ledger.stat().st_size > 0)
            wait_until(lambda: "pipe_write" in Path(f"/proc/{process.pid}/wchan").read_text())
            saved = ledger.read_bytes()
            assert len(records(ledger)) == 1
            assert process.poll() is None
            # Disconnect/cancel only our blocked CLI; the durable append remains
            # discoverable, so recovery must inspect it rather than retry it.
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            assert process.returncode == -signal.SIGTERM
            assert ledger.read_bytes() == saved
            assert records(ledger)[0]["text"] == "completed before output stalled"
    finally:
        os.close(write_fd)
        os.close(read_fd)
