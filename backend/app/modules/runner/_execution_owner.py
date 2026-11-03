from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

CHILD_READY_TIMEOUT_SECONDS = 5.0


def _lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        locking = msvcrt.locking  # type: ignore[attr-defined]
        lk_nblck = msvcrt.LK_NBLCK  # type: ignore[attr-defined]
        try:
            locking(handle.fileno(), lk_nblck, 1)
        except OSError:
            handle.close()
            return None
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
    return handle


def _unlock_file(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            locking = msvcrt.locking  # type: ignore[attr-defined]
            lk_unlck = msvcrt.LK_UNLCK  # type: ignore[attr-defined]
            locking(handle.fileno(), lk_unlck, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _bounded_text(value: str | bytes, max_bytes: int) -> tuple[str, bool]:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _emit(payload: Mapping[str, object]) -> bool:
    try:
        sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return True
    except BrokenPipeError:
        return False


def _wait_until_child_owns_lock(process: subprocess.Popen[str], ready_path: Path) -> None:
    deadline = time.monotonic() + CHILD_READY_TIMEOUT_SECONDS
    while not ready_path.exists():
        return_code = process.poll()
        if return_code is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(
                f"Runner execution child exited before acquiring ownership: {stderr.strip()}"
            )
        if time.monotonic() >= deadline:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("Runner execution child did not acquire ownership in time.")
        time.sleep(0.01)


def _run_owned_child(
    *,
    owner_lock_path: Path,
    command: list[str],
    env: dict[str, str],
    cwd: str,
    timeout_seconds: int,
) -> tuple[int | None, bool, str, str]:
    child_script = Path(__file__).with_name("_execution_child.py")
    child_lock_path = owner_lock_path.with_name(f"{owner_lock_path.name}.child")
    ready_path = child_lock_path.with_name(f"{child_lock_path.name}.ready")
    ready_path.unlink(missing_ok=True)

    child_command = [
        sys.executable,
        str(child_script),
        str(child_lock_path),
        str(ready_path),
        command[1],
        command[2],
        command[3],
    ]
    process = subprocess.Popen(
        child_command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_until_child_owns_lock(process, ready_path)
        try:
            stdout, stderr = process.communicate("run\n", timeout=timeout_seconds)
            return process.returncode, False, stdout, stderr
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            return None, True, stdout, stderr
    finally:
        ready_path.unlink(missing_ok=True)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    lock_path = Path(sys.argv[1])
    lock_handle = _lock_file(lock_path)
    if lock_handle is None:
        _emit({"state": "busy"})
        return 3

    try:
        if not _emit({"state": "ready"}):
            return 0
        request_line = sys.stdin.readline()
        if not request_line:
            return 0
        request = json.loads(request_line)
        if request.get("action") != "run":
            return 0

        command = [str(value) for value in request["command"]]
        env = {str(key): str(value) for key, value in dict(request["env"]).items()}
        cwd = str(request["cwd"])
        timeout_seconds = int(request["timeout_seconds"])
        max_stdout_bytes = int(request["max_stdout_bytes"])
        max_stderr_bytes = int(request["max_stderr_bytes"])

        return_code, timed_out, raw_stdout, raw_stderr = _run_owned_child(
            owner_lock_path=lock_path,
            command=command,
            env=env,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        stdout, stdout_truncated = _bounded_text(raw_stdout, max_stdout_bytes)
        stderr, stderr_truncated = _bounded_text(raw_stderr, max_stderr_bytes)
        result = {
            "state": "completed",
            "return_code": return_code,
            "timed_out": timed_out,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }

        if not _emit(result):
            return 0

        # Keep ownership alive through the caller's durable result/failure commit.
        # If the caller dies, pipe EOF releases the owner lock after child completion.
        sys.stdin.readline()
        return 0
    finally:
        _unlock_file(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
