"""One-shot, fail-closed bubblewrap boundary for user-authored Python."""

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings

MECHANISM = "bubblewrap-userns-v1"
MAX_SOURCE_BYTES = 65_536
MAX_REQUEST_BYTES = 131_072
MAX_RESULT_BYTES = 65_536
MAX_LOG_BYTES = 16_384
# One table of host-selected profiles (spec 189). These are engineering defaults, not
# architectural limits; user code and requests can never select a profile or exceed it.
PROFILES: dict[str, dict[str, int]] = {
    "calculation": {"wall_seconds": 15, "cpu_seconds": 12, "memory_bytes": 1_073_741_824, "processes": 32,
                    "file_bytes": 131_072, "scratch_bytes": 4 * 1024 * 1024, "artifact_bytes": 0},
    # CPU is four times wall time: the OCCT mesher may use several threads; wall time binds.
    "geometry": {"wall_seconds": 120, "cpu_seconds": 480, "memory_bytes": 4 * 1_073_741_824, "processes": 32,
                 "file_bytes": 64 * 1024 * 1024, "scratch_bytes": 256 * 1024 * 1024,
                 "artifact_bytes": 128 * 1024 * 1024},
}
ARTIFACT_NAMES = ("model.step", "model.stl", "model.glb")
_CALC = PROFILES["calculation"]
MAX_WALL_SECONDS = _CALC["wall_seconds"]
MAX_CPU_SECONDS = _CALC["cpu_seconds"]
MAX_MEMORY_BYTES = _CALC["memory_bytes"]
MAX_PROCESSES = _CALC["processes"]
MAX_FILE_BYTES = _CALC["file_bytes"]
MAX_SCRATCH_BYTES = _CALC["scratch_bytes"]
# Worker exit codes that name the limit a run hit (worker.py).
_EXIT_LIMITS = {3: "memory_bytes", 4: "file_bytes", 5: "scratch_bytes"}


class AppliedLimits(BaseModel):
    """The limits a run actually used; a profile's ceilings are enforced where they are chosen."""
    model_config = ConfigDict(extra="forbid")
    wall_seconds: int
    cpu_seconds: int
    memory_bytes: int
    processes: int
    file_bytes: int


class Limits(AppliedLimits):
    wall_seconds: int = Field(default=10, ge=1, le=MAX_WALL_SECONDS)
    cpu_seconds: int = Field(default=8, ge=1, le=MAX_CPU_SECONDS)
    memory_bytes: int = Field(default=MAX_MEMORY_BYTES, ge=64 * 1024 * 1024, le=MAX_MEMORY_BYTES)
    processes: int = Field(default=MAX_PROCESSES, ge=1, le=MAX_PROCESSES)
    file_bytes: int = Field(default=MAX_FILE_BYTES, ge=1024, le=MAX_FILE_BYTES)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(max_length=MAX_SOURCE_BYTES)
    inputs: dict[str, Any] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)


class Artifact(BaseModel):
    name: str
    sha256: str
    size_bytes: int


class RunRecord(BaseModel):
    source_sha256: str
    limits: AppliedLimits
    mechanism: str
    duration_seconds: float
    classification: Literal["ok", "error", "timeout", "limit", "invalid_result", "unavailable", "disabled"]
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    logs_truncated: bool = False
    profile: Literal["calculation", "geometry"] = "calculation"
    # Which limit stopped the run and its configured value (spec 189); None when no limit was hit.
    limit: str | None = None
    limit_value: int | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    artifact_error: str | None = None


def _finite(value: Any) -> bool:
    if isinstance(value, dict):
        return all(isinstance(k, str) and _finite(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_finite(v) for v in value)
    if isinstance(value, float):
        return value == value and abs(value) != float("inf")
    return value is None or isinstance(value, (str, int, bool))


def _paths() -> tuple[str, Path, Path] | None:
    if sys.platform != "linux":
        return None
    bwrap = shutil.which("bwrap")
    venv = Path(sys.prefix).resolve()
    worker = Path(__file__).with_name("worker.py").resolve()
    if not bwrap or not (venv / "bin/python").exists() or not worker.is_file():
        return None
    return bwrap, venv, worker


def _args(bwrap: str, venv: Path, worker: Path, scratch: Path, scratch_bytes: int = MAX_SCRATCH_BYTES,
          artifacts: bool = False) -> list[str]:
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev",
            "--dir", "/opt", "--dir", "/work", "--size", str(scratch_bytes),
            "--tmpfs", "/work", "--dir", "/work/tmp", "--symlink", "/work/tmp", "/tmp"]
    for directory in ("/lib", "/lib64", "/bin", "/sbin"):
        if Path(directory).exists():
            args += ["--ro-bind", directory, directory]
    for parent in reversed(venv.parents):
        if str(parent) not in ("/", "/usr", "/opt"):
            args += ["--dir", str(parent)]
    args += ["--ro-bind", str(venv), str(venv), "--ro-bind", str(worker), "/opt/worker.py",
             "--ro-bind", str(scratch / "request.json"), "/work/request.json",
             "--bind", str(scratch / "result.json"), "/work/result.json"]
    if artifacts:
        # Each artifact is one pre-created host file bound on its own: the sandbox can write
        # into it but cannot create, rename or link any other host entry (spec 189).
        args += ["--dir", "/work/artifacts"]
        for name in ARTIFACT_NAMES:
            args += ["--bind", str(scratch / "artifacts" / name), f"/work/artifacts/{name}"]
    args += ["--setenv", "HOME", "/tmp",
             "--setenv", "PATH", f"{venv}/bin:/usr/bin:/bin", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
             "--setenv", "OPENBLAS_NUM_THREADS", "1", "--chdir", "/work", "--",
             str(venv / "bin/python"), "-I", "/opt/worker.py"]
    return args


def _launch(args: list[str], scratch: Path, limits: AppliedLimits) -> tuple[int | None, bool]:
    with (scratch / "stdout.log").open("wb") as stdout, (scratch / "stderr.log").open("wb") as stderr:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   env={}, start_new_session=True)
        try:
            return process.wait(timeout=limits.wall_seconds), False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return process.returncode, True


def _log(path: Path) -> tuple[str, bool]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        data = os.read(fd, MAX_LOG_BYTES)
    finally:
        os.close(fd)
    return data.decode("utf-8", errors="replace"), info.st_size > MAX_LOG_BYTES


def _result(path: Path) -> Any:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESULT_BYTES:
            raise ValueError("invalid result file")
        data = os.read(fd, MAX_RESULT_BYTES + 1)
    finally:
        os.close(fd)
    value = json.loads(data.decode("utf-8"), parse_constant=lambda _: float("nan"))
    if not _finite(value):
        raise ValueError("non-finite result")
    return value


def status() -> dict[str, Any]:
    enabled = get_settings().studio_user_code_enabled
    paths = _paths()
    if not enabled:
        return {"enabled": False, "available": False, "mechanism": MECHANISM, "reason": "disabled"}
    if paths is None:
        return {"enabled": True, "available": False, "mechanism": MECHANISM, "reason": "bubblewrap_or_python_unavailable"}
    with tempfile.TemporaryDirectory(prefix="jarvis-studio-probe-") as directory:
        (Path(directory) / "request.json").touch()
        (Path(directory) / "result.json").touch()
        args = _args(*paths, Path(directory))
        args[-3:] = [str(paths[1] / "bin/python"), "-c", "print('studio-probe-ok')"]
        try:
            probe = subprocess.run(args, capture_output=True, timeout=3, env={}, check=False)
        except (OSError, subprocess.TimeoutExpired):
            probe = None
    available = probe is not None and probe.returncode == 0 and probe.stdout == b"studio-probe-ok\n"
    return {"enabled": True, "available": available, "mechanism": MECHANISM,
            "reason": None if available else "bubblewrap_probe_failed"}


def _well_formed(name: str, path: Path, size: int) -> bool:
    """Structural header checks only; the bytes stay untrusted and are never parsed further."""
    with path.open("rb") as handle:
        head = handle.read(84)
    if name == "model.glb":
        return (len(head) >= 20 and head[:4] == b"glTF" and int.from_bytes(head[4:8], "little") == 2
                and int.from_bytes(head[8:12], "little") == size and head[16:20] == b"JSON")
    if name == "model.step":
        return head.startswith(b"ISO-10303-21;")
    # Binary STL: 80-byte header, triangle count, 50 bytes per triangle; or ASCII "solid".
    return (len(head) == 84 and size == 84 + 50 * int.from_bytes(head[80:84], "little")) or head.startswith(b"solid")


def _admit(source: Path, destination: Path, profile: dict[str, int]) -> tuple[list[Artifact], str | None, str | None]:
    """After sandbox exit, copy all three artifacts to host-owned immutable files, or none.

    Returns the admitted artifacts, or an error code, or the name of the limit the output hit.
    """
    if sorted(os.listdir(source)) != sorted(ARTIFACT_NAMES):
        return [], "artifact_set_invalid", None
    descriptors: list[tuple[str, int, int]] = []
    try:
        for name in ARTIFACT_NAMES:
            try:
                fd = os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                return [], "artifact_not_regular", None
            info = os.fstat(fd)
            descriptors.append((name, fd, info.st_size))
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return [], "artifact_not_regular", None
            if info.st_size == 0:
                return [], "artifact_missing", None
            # RLIMIT_FSIZE stops writes at the cap, so a file at the cap was truncated.
            if info.st_size >= profile["file_bytes"]:
                return [], None, "file_bytes"
        if sum(size for _, _, size in descriptors) > profile["artifact_bytes"]:
            return [], None, "artifact_bytes"
        try:
            destination.mkdir(mode=0o700, parents=True)
        except OSError:
            # Never clean up a destination this call did not create.
            return [], "artifact_copy_failed", None
        admitted = []
        try:
            for name, fd, size in descriptors:
                digest = hashlib.sha256()
                target = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    remaining = size
                    while remaining:
                        chunk = os.read(fd, min(remaining, 1 << 20))
                        if not chunk:
                            raise ValueError("artifact shrank")
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(target, view):]
                        remaining -= len(chunk)
                    if os.read(fd, 1):
                        raise ValueError("artifact grew")
                    os.fsync(target)
                    os.fchmod(target, 0o444)
                finally:
                    os.close(target)
                copied = hashlib.sha256((destination / name).read_bytes()).hexdigest()
                if copied != digest.hexdigest():
                    raise ValueError("copied artifact hash differs")
                if not _well_formed(name, destination / name, size):
                    shutil.rmtree(destination, ignore_errors=True)
                    return [], "artifact_format_invalid", None
                admitted.append(Artifact(name=name, sha256=copied, size_bytes=size))
        except (OSError, ValueError):
            shutil.rmtree(destination, ignore_errors=True)
            return [], "artifact_copy_failed", None
        return admitted, None, None
    finally:
        for _, fd, _ in descriptors:
            os.close(fd)


def run(request: RunRequest, profile: Literal["calculation", "geometry"] = "calculation",
        artifact_dir: Path | None = None) -> RunRecord:
    """Run user source in a fresh sandbox.

    The caller selects the profile. A geometry run uses the fixed geometry limits, ignores
    request limits, and copies its verified artifacts into ``artifact_dir``, which must not exist.
    """
    started = time.monotonic()
    digest = hashlib.sha256(request.source.encode("utf-8")).hexdigest()
    table = PROFILES[profile]
    limits: AppliedLimits = request.limits
    if profile == "geometry":
        if artifact_dir is None:
            raise ValueError("A geometry run needs a host artifact directory.")
        limits = AppliedLimits(**{name: table[name] for name in AppliedLimits.model_fields})
    record = RunRecord(source_sha256=digest, limits=limits, mechanism=MECHANISM,
                       duration_seconds=0, classification="unavailable", profile=profile)
    if len(request.source.encode("utf-8")) > MAX_SOURCE_BYTES:
        record.classification = "invalid_result"
        return record
    state = status()
    if not state["enabled"] or not state["available"]:
        record.classification = "disabled" if not state["enabled"] else "unavailable"
        return record
    if not _finite(request.inputs):
        record.classification = "invalid_result"
        return record
    try:
        payload = json.dumps({"source": request.source, "inputs": request.inputs, "profile": profile,
                              "limits": limits.model_dump()}, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        record.classification = "invalid_result"
        return record
    if len(payload) > MAX_REQUEST_BYTES:
        record.classification = "invalid_result"
        return record
    paths = _paths()
    if paths is None:
        return record
    with tempfile.TemporaryDirectory(prefix="jarvis-studio-run-") as directory:
        scratch = Path(directory)
        (scratch / "request.json").write_bytes(payload)
        (scratch / "result.json").touch()
        if profile == "geometry":
            (scratch / "artifacts").mkdir(mode=0o700)
            for name in ARTIFACT_NAMES:
                (scratch / "artifacts" / name).touch(mode=0o600)
        try:
            code, timed_out = _launch(_args(*paths, scratch, table["scratch_bytes"], profile == "geometry"),
                                      scratch, limits)
        except OSError:
            record.duration_seconds = time.monotonic() - started
            return record
        try:
            record.stdout, out_cut = _log(scratch / "stdout.log")
            record.stderr, err_cut = _log(scratch / "stderr.log")
        except (OSError, ValueError):
            record.classification = "invalid_result"
            record.duration_seconds = time.monotonic() - started
            return record
        record.logs_truncated = out_cut or err_cut
        record.duration_seconds = time.monotonic() - started
        if timed_out:
            record.classification, record.limit = "timeout", "wall_seconds"
        elif code is not None and code < 0 and -code in (signal.SIGXCPU, signal.SIGXFSZ, signal.SIGKILL):
            record.classification = "limit"
            record.limit = {int(signal.SIGXCPU): "cpu_seconds", int(signal.SIGXFSZ): "file_bytes"}.get(-code)
        elif code in _EXIT_LIMITS:
            record.classification, record.limit = "limit", _EXIT_LIMITS[code]
        elif code != 0:
            record.classification = "error"
        else:
            try:
                data = _result(scratch / "result.json")
            except (OSError, UnicodeError, ValueError, RecursionError):
                record.classification = "invalid_result"
            else:
                record.classification = "ok"
                record.result = data
        if record.classification == "ok" and profile == "geometry":
            assert artifact_dir is not None
            record.artifacts, record.artifact_error, hit = _admit(scratch / "artifacts", artifact_dir, table)
            if hit:
                record.classification, record.limit = "limit", hit
            elif record.artifact_error:
                record.classification = "invalid_result"
            if record.classification != "ok":
                record.result = None
        if record.limit:
            record.limit_value = table[record.limit]
    return record
