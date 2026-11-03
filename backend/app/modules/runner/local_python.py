from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

OWNER_LOCK_NAME = ".jarvis-runner-owner.lock"


@dataclass(frozen=True)
class LocalPythonResult:
    return_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    command_metadata: dict[str, object]
    environment_metadata: dict[str, object]


class ExecutionOwnerBusy(RuntimeError):
    pass


class _ExecutionOwnerSession:
    def __init__(self, working_dir: Path) -> None:
        self.working_dir = working_dir.resolve()
        self.lock_path = ownership_lock_path(self.working_dir)
        self.caller_lock_path = caller_ownership_lock_path(self.working_dir)
        self.caller_lock_handle: BinaryIO | None = None
        self.process: subprocess.Popen[str] | None = None
        self.executed = False

    def start(self) -> None:
        owner_dir = self.working_dir.parent
        owner_dir.mkdir(parents=True, exist_ok=True)
        self.caller_lock_handle = _acquire_process_lock(self.caller_lock_path)
        owner_script = Path(__file__).with_name("_execution_owner.py")
        owner_env = {"PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}
        try:
            process = subprocess.Popen(
                [sys.executable, str(owner_script), str(self.lock_path)],
                cwd=str(owner_dir),
                env=owner_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self.process = process
            assert process.stdout is not None
            line = process.stdout.readline()
            if not line:
                stderr = process.stderr.read() if process.stderr is not None else ""
                process.wait(timeout=5)
                raise RuntimeError(f"Runner execution owner failed to start: {stderr.strip()}")
            payload = json.loads(line)
            if payload.get("state") == "busy":
                process.wait(timeout=5)
                raise ExecutionOwnerBusy("Runner execution ownership is already held.")
            if payload.get("state") != "ready":
                process.terminate()
                process.wait(timeout=5)
                raise RuntimeError("Runner execution owner returned an invalid readiness state.")
        except Exception:
            self._release_caller_lock()
            raise

    def execute(
        self,
        *,
        script_path: Path,
        input_file: Path,
        output_dir: Path,
        timeout_seconds: int,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> LocalPythonResult:
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("Runner execution owner is not started.")
        if self.executed:
            raise RuntimeError("Runner execution owner can execute only once.")
        self.executed = True

        command = [sys.executable, str(script_path), str(input_file), str(output_dir)]
        env = {"PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}
        request = {
            "action": "run",
            "command": command,
            "env": env,
            "cwd": str(self.working_dir),
            "timeout_seconds": timeout_seconds,
            "max_stdout_bytes": max_stdout_bytes,
            "max_stderr_bytes": max_stderr_bytes,
        }
        self.process.stdin.write(json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read() if self.process.stderr is not None else ""
            raise RuntimeError(f"Runner execution owner exited before returning a result: {stderr.strip()}")
        payload = json.loads(line)
        if payload.get("state") != "completed":
            raise RuntimeError("Runner execution owner returned an invalid completion state.")

        command_metadata = {
            "executable": Path(sys.executable).name,
            "argv": [Path(command[0]).name, str(script_path), str(input_file), str(output_dir)],
            "shell": False,
        }
        environment_metadata = {
            "inherited_environment": False,
            "allowlisted_keys": sorted(env.keys()),
        }
        return LocalPythonResult(
            return_code=payload.get("return_code"),
            timed_out=bool(payload["timed_out"]),
            stdout=str(payload["stdout"]),
            stderr=str(payload["stderr"]),
            stdout_truncated=bool(payload["stdout_truncated"]),
            stderr_truncated=bool(payload["stderr_truncated"]),
            command_metadata=command_metadata,
            environment_metadata=environment_metadata,
        )

    def release(self) -> None:
        process = self.process
        try:
            if process is not None:
                try:
                    if process.poll() is None and process.stdin is not None:
                        try:
                            process.stdin.write("release\n")
                            process.stdin.flush()
                            process.stdin.close()
                        except (BrokenPipeError, OSError):
                            pass
                    if process.poll() is None:
                        process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        finally:
            self.process = None
            self._release_caller_lock()

    def _release_caller_lock(self) -> None:
        handle = self.caller_lock_handle
        if handle is None:
            return
        self.caller_lock_handle = None
        _release_process_lock(handle)


_sessions: dict[Path, _ExecutionOwnerSession] = {}
_sessions_lock = threading.Lock()


def ownership_lock_path(working_dir: Path) -> Path:
    resolved = working_dir.resolve()
    return resolved.parent / f".{resolved.name}{OWNER_LOCK_NAME}"


def caller_ownership_lock_path(working_dir: Path) -> Path:
    owner_lock_path = ownership_lock_path(working_dir)
    return owner_lock_path.with_name(f"{owner_lock_path.name}.caller")


def child_ownership_lock_path(working_dir: Path) -> Path:
    owner_lock_path = ownership_lock_path(working_dir)
    return owner_lock_path.with_name(f"{owner_lock_path.name}.child")


def _acquire_process_lock(lock_path: Path) -> BinaryIO:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            locking = msvcrt.locking  # type: ignore[attr-defined]
            lk_nblck = msvcrt.LK_NBLCK  # type: ignore[attr-defined]
            try:
                locking(handle.fileno(), lk_nblck, 1)
            except OSError as exc:
                raise ExecutionOwnerBusy("Runner execution ownership is already held.") from exc
            return handle
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExecutionOwnerBusy("Runner execution ownership is already held.") from exc
        return handle
    except Exception:
        handle.close()
        raise


def _release_process_lock(handle: BinaryIO) -> None:
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


@contextmanager
def prepare_execution_owner(working_dir: Path) -> Iterator[None]:
    key = working_dir.resolve()
    session = _ExecutionOwnerSession(key)
    with _sessions_lock:
        if key in _sessions:
            raise ExecutionOwnerBusy("Runner execution ownership is already held in this process.")
        _sessions[key] = session
    try:
        session.start()
        yield
    finally:
        with _sessions_lock:
            if _sessions.get(key) is session:
                _sessions.pop(key, None)
        session.release()


def _lock_state(lock_path: Path) -> str:
    if not lock_path.exists():
        return "unknown"
    handle = lock_path.open("r+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            locking = msvcrt.locking  # type: ignore[attr-defined]
            lk_nblck = msvcrt.LK_NBLCK  # type: ignore[attr-defined]
            lk_unlck = msvcrt.LK_UNLCK  # type: ignore[attr-defined]
            try:
                locking(handle.fileno(), lk_nblck, 1)
            except OSError:
                return "live"
            locking(handle.fileno(), lk_unlck, 1)
            return "gone"
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "live"
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return "gone"
    finally:
        handle.close()


def execution_ownership_state(working_dir: Path) -> str:
    """Return live, gone, or unknown from runner-specific OS lock evidence."""

    owner_state = _lock_state(ownership_lock_path(working_dir))
    if owner_state == "live":
        return "live"

    caller_state = _lock_state(caller_ownership_lock_path(working_dir))
    if caller_state == "live":
        return "live"

    child_state = _lock_state(child_ownership_lock_path(working_dir))
    if child_state == "live":
        return "live"
    if owner_state == "gone" or caller_state == "gone" or child_state == "gone":
        return "gone"
    return "unknown"


def execute_python_script(
    *,
    script_path: Path,
    input_file: Path,
    output_dir: Path,
    working_dir: Path,
    timeout_seconds: int,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> LocalPythonResult:
    key = working_dir.resolve()
    with _sessions_lock:
        session = _sessions.get(key)
    if session is not None:
        return session.execute(
            script_path=script_path,
            input_file=input_file,
            output_dir=output_dir,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )

    command = [sys.executable, str(script_path), str(input_file), str(output_dir)]
    env = {"PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}
    command_metadata = {
        "executable": Path(sys.executable).name,
        "argv": [Path(command[0]).name, str(script_path), str(input_file), str(output_dir)],
        "shell": False,
    }
    environment_metadata = {
        "inherited_environment": False,
        "allowlisted_keys": sorted(env.keys()),
    }

    try:
        completed = subprocess.run(
            command,
            cwd=str(working_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = _bounded_text(exc.stdout or "", max_stdout_bytes)
        stderr, stderr_truncated = _bounded_text(exc.stderr or "", max_stderr_bytes)
        return LocalPythonResult(
            return_code=None,
            timed_out=True,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            command_metadata=command_metadata,
            environment_metadata=environment_metadata,
        )

    stdout, stdout_truncated = _bounded_text(completed.stdout, max_stdout_bytes)
    stderr, stderr_truncated = _bounded_text(completed.stderr, max_stderr_bytes)
    return LocalPythonResult(
        return_code=completed.returncode,
        timed_out=False,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        command_metadata=command_metadata,
        environment_metadata=environment_metadata,
    )


def _bounded_text(value: str | bytes, max_bytes: int) -> tuple[str, bool]:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True
