"""BLUECAD tool registry loading, resolution, and subprocess execution."""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from threading import Event
from typing import Any

import yaml

REGISTRY_VERSION = "bluecad_tool_registry_v0_1"
REGISTRY_ENV_VAR = "JARVISOS_BLUECAD_TOOL_REGISTRY"
_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_REGISTRY_PATH = _REPO_ROOT / "configs" / "bluecad_tools.yaml"

_TOOL_KEYS = {
    "id",
    "kind",
    "integration_mode",
    "version_pin",
    "license",
    "enabled",
    "entrypoint",
    "binary_sha256",
    "provenance_url",
    "capabilities",
    "health_check",
}
_LICENSE_KEYS = {"spdx", "boundary", "verified_date"}
_KINDS = {"cad_kernel", "mesher", "fem_solver", "cfd_solver", "viewer"}
_MODES = {"in_process", "subprocess", "container"}
_BOUNDARIES = {"A", "B", "C", "D"}
_HASH_MODES = {"subprocess", "container"}
_MINIMAL_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "PYTHONIOENCODING": "utf-8",
    "OMP_NUM_THREADS": "1",
}


@dataclass(frozen=True)
class ToolRegistryError(Exception):
    """Structured error raised for fail-closed registry failures."""

    code: str
    message: str
    detail: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class ToolRunResult:
    """Captured tool process result."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    code: str | None = None


ToolEntry = dict[str, Any]
Registry = dict[str, Any]


def resolve_registry_path(path: str | Path | None = None) -> Path:
    """Resolve one operator-owned registry path for the whole tool call chain."""
    if path is not None:
        value = str(path).strip()
        if not value:
            raise ToolRegistryError("REGISTRY_PATH_INVALID", "Explicit registry path must not be blank")
        return Path(value)
    configured = os.getenv(REGISTRY_ENV_VAR)
    if configured is not None and configured.strip():
        return Path(configured.strip())
    return DEFAULT_REGISTRY_PATH


def _load_raw_registry(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ToolRegistryError("REGISTRY_NOT_FOUND", f"Registry not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ToolRegistryError("REGISTRY_INVALID", f"Registry is not valid YAML: {exc}") from exc


def _validate_registry(raw: Any) -> Registry:
    if not isinstance(raw, dict):
        raise ToolRegistryError("REGISTRY_INVALID", "Registry root must be an object")
    unknown_root = set(raw) - {"registry_version", "tools"}
    if unknown_root:
        raise ToolRegistryError("UNKNOWN_FIELD", "Registry contains unknown root fields", {"fields": sorted(unknown_root)})
    if raw.get("registry_version") != REGISTRY_VERSION:
        raise ToolRegistryError("REGISTRY_INVALID", "Registry version is unsupported")
    tools = raw.get("tools")
    if not isinstance(tools, list):
        raise ToolRegistryError("REGISTRY_INVALID", "Registry tools must be a list")

    seen: set[str] = set()
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ToolRegistryError("REGISTRY_INVALID", "Tool entry must be an object", {"index": index})
        unknown = set(tool) - _TOOL_KEYS
        if unknown:
            raise ToolRegistryError("UNKNOWN_FIELD", "Tool entry contains unknown fields", {"index": index, "fields": sorted(unknown)})
        required = {"id", "kind", "integration_mode", "version_pin", "license", "enabled", "entrypoint", "capabilities", "health_check"}
        missing = required - set(tool)
        if missing:
            raise ToolRegistryError("REGISTRY_INVALID", "Tool entry is missing required fields", {"index": index, "fields": sorted(missing)})
        tool_id = tool["id"]
        if not isinstance(tool_id, str) or not tool_id:
            raise ToolRegistryError("REGISTRY_INVALID", "Tool id must be a non-empty string", {"index": index})
        if tool_id in seen:
            raise ToolRegistryError("DUPLICATE_TOOL_ID", "Registry contains duplicate tool ids", {"tool_id": tool_id})
        seen.add(tool_id)
        _validate_tool(tool, index)
    return {"registry_version": raw["registry_version"], "tools": tools}


def _validate_tool(tool: ToolEntry, index: int) -> None:
    if tool["kind"] not in _KINDS:
        raise ToolRegistryError("REGISTRY_INVALID", "Tool kind is unsupported", {"index": index})
    if tool["integration_mode"] not in _MODES:
        raise ToolRegistryError("REGISTRY_INVALID", "Tool integration mode is unsupported", {"index": index})
    if not isinstance(tool["enabled"], bool):
        raise ToolRegistryError("REGISTRY_INVALID", "Tool enabled must be boolean", {"index": index})
    version_pin = tool["version_pin"]
    if not isinstance(version_pin, str) or not version_pin or any(marker in version_pin for marker in "<>=~^*"):
        raise ToolRegistryError("REGISTRY_INVALID", "Tool version_pin must be an exact pin", {"index": index})
    _validate_license(tool["license"], index)
    if tool["entrypoint"] is not None and not isinstance(tool["entrypoint"], str):
        raise ToolRegistryError("REGISTRY_INVALID", "Tool entrypoint must be a string or null", {"index": index})
    if not isinstance(tool["capabilities"], list) or not all(isinstance(item, str) and item for item in tool["capabilities"]):
        raise ToolRegistryError("REGISTRY_INVALID", "Tool capabilities must be non-empty strings", {"index": index})
    if len(tool["capabilities"]) != len(set(tool["capabilities"])):
        raise ToolRegistryError("REGISTRY_INVALID", "Tool capabilities must be unique", {"index": index})
    if tool["health_check"] is not None and not isinstance(tool["health_check"], str):
        raise ToolRegistryError("REGISTRY_INVALID", "Tool health_check must be a string or null", {"index": index})
    if tool["integration_mode"] in _HASH_MODES and tool["enabled"]:
        if not tool["entrypoint"]:
            raise ToolRegistryError("UNHASHED_SUBPROCESS_TOOL", "Enabled subprocess/container tool requires entrypoint", {"tool_id": tool["id"]})
        _validate_hash_field(tool.get("binary_sha256"), tool["id"])
        if not tool.get("provenance_url"):
            raise ToolRegistryError("REGISTRY_INVALID", "Enabled subprocess/container tool requires provenance_url", {"tool_id": tool["id"]})
    elif tool.get("binary_sha256") is not None:
        _validate_hash_field(tool.get("binary_sha256"), tool["id"])


def _validate_license(value: Any, index: int) -> None:
    if not isinstance(value, dict):
        raise ToolRegistryError("REGISTRY_INVALID", "Tool license must be an object", {"index": index})
    unknown = set(value) - _LICENSE_KEYS
    if unknown:
        raise ToolRegistryError("UNKNOWN_FIELD", "Tool license contains unknown fields", {"index": index, "fields": sorted(unknown)})
    missing = _LICENSE_KEYS - set(value)
    if missing:
        raise ToolRegistryError("SPDX_REQUIRED", "Tool license is missing SPDX, boundary, or verified_date", {"index": index, "fields": sorted(missing)})
    if not isinstance(value["spdx"], str) or not value["spdx"]:
        raise ToolRegistryError("SPDX_REQUIRED", "Tool license SPDX must be a non-empty string", {"index": index})
    if value["boundary"] not in _BOUNDARIES:
        raise ToolRegistryError("REGISTRY_INVALID", "Tool license boundary is unsupported", {"index": index})
    try:
        date.fromisoformat(value["verified_date"])
    except (TypeError, ValueError) as exc:
        raise ToolRegistryError("REGISTRY_INVALID", "Tool license verified_date must be an ISO date", {"index": index}) from exc


def _validate_hash_field(value: Any, tool_id: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise ToolRegistryError("UNHASHED_SUBPROCESS_TOOL", "Subprocess/container tool requires a SHA-256 hash", {"tool_id": tool_id})


def load_registry(path: str | Path | None = None) -> Registry:
    """Load and validate the explicitly selected or operator-configured registry."""
    registry_path = resolve_registry_path(path)
    registry = _validate_registry(_load_raw_registry(registry_path))
    validate_license_boundaries(registry)
    return registry


def validate_license_boundaries(registry: Registry) -> None:
    """Reject in-process integrations for boundary C/D tools."""
    for tool in registry["tools"]:
        if tool["license"]["boundary"] in {"C", "D"} and tool["integration_mode"] == "in_process":
            raise ToolRegistryError(
                "LICENSE_BOUNDARY_VIOLATION",
                "Boundary C/D tools must not use in-process integration",
                {"tool_id": tool["id"]},
            )


def _find_tool(registry: Registry, tool_id: str) -> ToolEntry | None:
    return next((tool for tool in registry["tools"] if tool["id"] == tool_id), None)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_tool(tool_id: str, registry_path: str | Path | None = None) -> ToolEntry:
    """Return an enabled registry entry after fail-closed hash checks."""
    registry = load_registry(registry_path)
    tool = _find_tool(registry, tool_id)
    if tool is None:
        raise ToolRegistryError("TOOL_UNKNOWN", "Tool id is not registered", {"tool_id": tool_id})
    if not tool["enabled"]:
        raise ToolRegistryError("TOOL_DISABLED", "Tool is disabled", {"tool_id": tool_id})
    if tool["integration_mode"] in _HASH_MODES:
        if not tool.get("entrypoint") or not tool.get("binary_sha256"):
            raise ToolRegistryError("UNHASHED_SUBPROCESS_TOOL", "Enabled subprocess/container tool is missing entrypoint or hash", {"tool_id": tool_id})
        entrypoint = Path(tool["entrypoint"])
        if not entrypoint.is_file():
            raise ToolRegistryError("TOOL_BINARY_MISSING", "Tool binary is missing", {"tool_id": tool_id, "entrypoint": str(entrypoint)})
        actual = _sha256_file(entrypoint)
        if actual.lower() != tool["binary_sha256"].lower():
            raise ToolRegistryError("TOOL_HASH_MISMATCH", "Tool binary hash does not match registry", {"tool_id": tool_id})
    return dict(tool)


def _kill_group(process: subprocess.Popen) -> None:
    """SIGKILL the tool's whole process group (it leads its own session), so MPI ranks cannot be orphaned."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass


def run_tool(tool_id: str, args: list[str], cwd: str | Path, timeout: float, registry_path: str | Path | None = None, *, cancel: Event | None = None, on_stdout: Callable[[str], None] | None = None) -> ToolRunResult:
    """Run a pinned tool with captured output and optional live stdout lines."""
    tool = resolve_tool(tool_id, registry_path)
    if tool["integration_mode"] not in _HASH_MODES:
        raise ToolRegistryError("TOOL_NOT_SUBPROCESS", "Tool is not configured for subprocess execution", {"tool_id": tool_id})
    command = [tool["entrypoint"], *args]
    env = dict(_MINIMAL_ENV)
    if tool_id == "openfoam" or tool_id.startswith("openfoam_"):
        prefix = Path(tool["entrypoint"]).resolve().parent.parent
        env.update({
            "PATH": str(prefix / "bin"),
            "LD_LIBRARY_PATH": str(prefix / "lib"),
            "WM_PROJECT_DIR": str(prefix),
            "FOAM_CONFIG_ETC": str(prefix / "etc"),
            "WM_PROJECT_VERSION": tool["version_pin"],
            "PWD": str(Path(cwd).resolve()),
        })
    if on_stdout is not None:
        process = subprocess.Popen(  # noqa: S603 - registry-pinned and hash-verified.
            command, cwd=Path(cwd), env=env, shell=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1, start_new_session=True,
        )
        output: list[str] = []
        errors: list[str] = []
        callback_errors: list[Exception] = []

        def kill_running() -> None:
            _kill_group(process)

        def drain(stream, captured: list[str], callback: Callable[[str], None] | None = None) -> None:
            try:
                for line in stream:
                    captured.append(line)
                    if callback is not None and not callback_errors:
                        callback(line)
            except Exception as exc:
                callback_errors.append(exc)
                kill_running()
            finally:
                stream.close()

        assert process.stdout is not None and process.stderr is not None
        readers = (
            threading.Thread(target=drain, args=(process.stdout, output, on_stdout), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, errors), daemon=True),
        )
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout
        code = None
        while process.poll() is None:
            if callback_errors or (cancel is not None and cancel.is_set()):
                code = "CANCELLED" if not callback_errors else None
                kill_running()
                break
            if time.monotonic() >= deadline:
                code = "TIMEOUT"
                kill_running()
                break
            try:
                process.wait(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        process.wait()
        _kill_group(process)  # a descendant still holding the pipes would block the readers; the group is ours alone
        for reader in readers:
            reader.join()
        if callback_errors:
            raise callback_errors[0]
        return ToolRunResult(130 if code == "CANCELLED" else 124 if code == "TIMEOUT" else process.returncode,
                             "".join(output), "".join(errors), timed_out=code == "TIMEOUT", code=code)
    if cancel is not None:
        process = subprocess.Popen(  # noqa: S603 - registry-pinned and hash-verified.
            command, cwd=Path(cwd), env=env, shell=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while True:
            if cancel.is_set() or time.monotonic() >= deadline:
                _kill_group(process)
                stdout, stderr = process.communicate()
                return ToolRunResult(130 if cancel.is_set() else 124, stdout, stderr,
                                     timed_out=not cancel.is_set(), code="CANCELLED" if cancel.is_set() else "TIMEOUT")
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                return ToolRunResult(process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    try:
        completed = subprocess.run(  # noqa: S603 - command is registry-pinned and hash-verified.
            command,
            cwd=Path(cwd),
            env=env,
            shell=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ToolRunResult(
            returncode=124,
            stdout=(exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or "",
            stderr=(exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or "",
            timed_out=True,
            code="TIMEOUT",
        )
    return ToolRunResult(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


def _health_check_args(tool: ToolEntry) -> list[str]:
    health_check = tool.get("health_check")
    if not health_check:
        return []
    command = shlex.split(health_check)
    if not command:
        return []
    entrypoint = str(tool["entrypoint"])
    first = command[0]
    if first == entrypoint or Path(first).name == Path(entrypoint).name:
        return command[1:]
    raise ToolRegistryError(
        "HEALTH_CHECK_ENTRYPOINT_MISMATCH",
        "Subprocess health_check must start with the registered entrypoint",
        {"tool_id": tool["id"]},
    )


def _run_in_process_health_check(command: str) -> ToolRunResult:
    completed = subprocess.run(  # noqa: S603 - admin-authored in-process health check, shell disabled.
        shlex.split(command),
        env=_MINIMAL_ENV,
        shell=False,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return ToolRunResult(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)


def _run_health_check(tool: ToolEntry, registry_path: str | Path | None) -> ToolRunResult | None:
    health_check = tool.get("health_check")
    if not health_check:
        return None
    if tool["integration_mode"] in _HASH_MODES:
        resolved_path = resolve_registry_path(registry_path)
        return run_tool(tool["id"], _health_check_args(tool), resolved_path.parent, 10, resolved_path)
    return _run_in_process_health_check(health_check)


def check_registry(registry_path: str | Path | None = None) -> tuple[int, str]:
    resolved_path = resolve_registry_path(registry_path)
    registry = load_registry(resolved_path)
    rows = [f"registry\t{resolved_path}", "tool\tenabled\thash\thealth"]
    exit_code = 0
    for tool in registry["tools"]:
        hash_status = "not-required"
        health_status = "skipped"
        if tool["enabled"]:
            try:
                resolved = resolve_tool(tool["id"], resolved_path)
                if resolved["integration_mode"] in _HASH_MODES:
                    hash_status = "ok"
                health_result = _run_health_check(resolved, resolved_path)
                if health_result is None:
                    health_status = "not-configured"
                else:
                    health_status = "ok" if health_result.returncode == 0 else f"failed({health_result.returncode})"
                    if health_result.returncode != 0:
                        exit_code = 1
            except (ToolRegistryError, subprocess.TimeoutExpired, OSError) as exc:
                exit_code = 1
                if isinstance(exc, ToolRegistryError) and exc.code in {"TOOL_HASH_MISMATCH", "TOOL_BINARY_MISSING"}:
                    hash_status = exc.code
                health_status = str(exc)
        rows.append(f"{tool['id']}\t{tool['enabled']}\t{hash_status}\t{health_status}")
    return exit_code, "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BLUECAD tool registry utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check", help="Check registry tool availability")
    check_parser.add_argument("--registry", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "check":
        exit_code, output = check_registry(args.registry)
        print(output)
        return exit_code
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
