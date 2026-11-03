"""Opt-in local handoff of a registered BLUECAD 3MF to the Windows default program.

Three gates must all pass (flag, WSL, configured /mnt/<drive> directory). The
file is copied under a server-built name, converted with ``wslpath -w`` and
handed to one fixed Windows binary with an argv list. No network, no printer,
no slicer CLI.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.core.wsl import running_under_wsl
from app.modules.events.service import utc_now

_SOURCE_REF = re.compile(r"^bluecad_candidate:([0-9a-f-]{36}):attempt:(\d+)$")
_HANDOFF_DIR = re.compile(r"^/mnt/[a-z]/.+")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:\\")
_WINDOWS_PATH_FORBIDDEN = re.compile(r'[\x00-\x1f"<>|*?&%^]')
_FILENAME_RE = re.compile(r"^[0-9a-f]{8}-attempt[0-9]{1,6}-[0-9a-f]{8}\.3mf$")
_EXPLORER = Path("/mnt/c/Windows/explorer.exe")
_EXPLORER_FALLBACK = re.compile(r"^/mnt/[a-z]/windows/explorer\.exe$", re.IGNORECASE)
_COPY_CHUNK = 1024 * 1024


class PrintHandoffError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class PrintHandoffCapability(BaseModel):
    available: bool
    code: str | None = None


class PrintHandoffEventRead(BaseModel):
    id: str
    workspace_id: str
    artifact_id: str
    sha256: str
    handoff_path: str
    windows_path: str
    outcome: str
    created_at: str
    message: str


@dataclass(frozen=True)
class _Gates:
    directory: Path | None
    code: str | None


def _check_gates() -> _Gates:
    settings = get_settings()
    if not settings.print_handoff_enabled:
        return _Gates(None, "print_handoff_disabled")
    if not running_under_wsl():
        return _Gates(None, "print_handoff_not_wsl")
    raw = settings.print_handoff_dir
    if not raw:
        return _Gates(None, "print_handoff_dir_unset")
    if not _HANDOFF_DIR.match(raw) or ".." in Path(raw).parts:
        return _Gates(None, "print_handoff_dir_invalid")
    return _Gates(Path(raw), None)


def capability() -> PrintHandoffCapability:
    gates = _check_gates()
    return PrintHandoffCapability(available=gates.code is None, code=gates.code)


_GATE_MESSAGES = {
    "print_handoff_disabled": "Print handoff is disabled (JARVISOS_PRINT_HANDOFF_ENABLED is not 1).",
    "print_handoff_not_wsl": "Print handoff requires the backend to run under WSL.",
    "print_handoff_dir_unset": "JARVISOS_PRINT_HANDOFF_DIR is not configured.",
    "print_handoff_dir_invalid": "JARVISOS_PRINT_HANDOFF_DIR must be a /mnt/<drive>/... directory.",
}


def handoff_filename(candidate_id: str, attempt_no: str | int, sha256: str) -> str:
    """Build the handoff file name only from server-held values; fail closed otherwise."""
    name = f"{str(candidate_id)[:8]}-attempt{attempt_no}-{str(sha256)[:8]}.3mf"
    if _FILENAME_RE.fullmatch(name) is None:
        raise PrintHandoffError(409, "print_handoff_unsafe_name", "Handoff file name inputs are not in the allowed charset.")
    return name


def _sha_matches_shape(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


def open_in_slicer(workspace_id: str, artifact_id: str) -> PrintHandoffEventRead:
    gates = _check_gates()
    if gates.code is not None or gates.directory is None:
        code = gates.code or "print_handoff_dir_unset"
        raise PrintHandoffError(409, code, _GATE_MESSAGES[code])

    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT stored_path, artifact_type, sha256, source_ref FROM artifacts WHERE id = ? AND workspace_id = ?",
            (artifact_id, workspace_id),
        ).fetchone()
    if row is None:
        raise PrintHandoffError(404, "print_handoff_artifact_not_found", "BLUECAD artifact not found.")
    if str(row["artifact_type"] or "") != "bluecad_3mf":
        raise PrintHandoffError(409, "print_handoff_not_3mf", "Only a BLUECAD 3MF artifact can be opened in a slicer.")
    registered_sha = str(row["sha256"] or "")
    match = _SOURCE_REF.match(str(row["source_ref"] or ""))
    if match is None or not _sha_matches_shape(registered_sha):
        raise PrintHandoffError(409, "print_handoff_unsafe_name", "Artifact provenance is not usable for a handoff name.")
    source = _confined_source(str(row["stored_path"]))
    filename = handoff_filename(match.group(1), match.group(2), registered_sha)

    directory = _resolve_directory(gates.directory)
    destination = directory / filename
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise PrintHandoffError(409, "print_handoff_dir_invalid", "Configured handoff directory cannot be opened.") from None
    try:
        _verify_directory(directory, directory_fd)
        copied_identity = _copy_verified(source, filename, directory_fd, registered_sha)
        windows_path = _to_windows_path(destination)
        argv = [_explorer_binary(), windows_path]
        _verify_destination(destination, directory, directory_fd, copied_identity, registered_sha)
        outcome = "handed_to_windows"
        failure: str | None = None
        try:
            # explorer.exe commonly exits 1 on success; success is "started without timeout".
            subprocess.run(argv, shell=False, timeout=15, capture_output=True)
        except subprocess.TimeoutExpired:
            outcome, failure = "timeout", "explorer.exe did not return within 15 seconds."
        except OSError as exc:
            outcome, failure = "launch_failed", f"explorer.exe could not be started: {exc.__class__.__name__}"
    finally:
        os.close(directory_fd)

    event = PrintHandoffEventRead(
        id=str(uuid4()),
        workspace_id=workspace_id,
        artifact_id=artifact_id,
        sha256=registered_sha,
        handoff_path=str(destination),
        windows_path=windows_path,
        outcome=outcome,
        created_at=utc_now(),
        message=failure or "Handed to Windows. The default program for .3mf will open the file.",
    )
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO print_handoff_events (
                id, workspace_id, artifact_id, sha256, handoff_path, windows_path, outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id, event.workspace_id, event.artifact_id, event.sha256,
                event.handoff_path, event.windows_path, event.outcome, event.created_at,
            ),
        )
        connection.commit()
    if failure is not None:
        raise PrintHandoffError(502, f"print_handoff_{outcome}", failure)
    return event


def _confined_source(stored_path_value: str) -> Path:
    raw = Path(stored_path_value)
    try:
        resolved = raw.resolve()
        resolved.relative_to(build_paths().data_root.resolve())
    except (OSError, RuntimeError, ValueError):
        raise PrintHandoffError(404, "print_handoff_artifact_not_found", "BLUECAD artifact not found.") from None
    if raw.is_symlink() or not resolved.is_file():
        raise PrintHandoffError(404, "print_handoff_artifact_not_found", "BLUECAD artifact not found.")
    return resolved


def _resolve_directory(configured: Path) -> Path:
    try:
        resolved = configured.resolve(strict=True)
    except (OSError, RuntimeError):
        raise PrintHandoffError(409, "print_handoff_dir_invalid", "Configured handoff directory does not exist.") from None
    if not resolved.is_dir() or not _HANDOFF_DIR.match(str(resolved)):
        raise PrintHandoffError(409, "print_handoff_dir_invalid", "Configured handoff directory is not a /mnt/<drive> directory.")
    return resolved


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _verify_directory(directory: Path, directory_fd: int) -> None:
    try:
        if _identity(os.stat(directory, follow_symlinks=False)) != _identity(os.fstat(directory_fd)):
            raise PrintHandoffError(409, "print_handoff_unsafe_target", "Handoff directory changed.")
    except OSError:
        raise PrintHandoffError(409, "print_handoff_unsafe_target", "Handoff directory changed.") from None


def _copy_verified(source: Path, filename: str, directory_fd: int, expected_sha256: str) -> tuple[int, int]:
    temporary = f".bluecad-{uuid4().hex}"
    digest = hashlib.sha256()
    try:
        try:
            target_info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            target_info = None
        if target_info is not None and stat.S_ISLNK(target_info.st_mode):
            raise PrintHandoffError(409, "print_handoff_unsafe_target", "Handoff target is a symlink.")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "wb") as out, source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_COPY_CHUNK), b""):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise PrintHandoffError(409, "print_handoff_hash_mismatch", "Artifact bytes do not match the registered sha256.")
        # Both names are relative to the same open directory, so a pathname swap
        # cannot redirect the copy. Replacing an entry also leaves hard links intact.
        os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        return _identity(os.stat(filename, dir_fd=directory_fd, follow_symlinks=False))
    except OSError:
        raise PrintHandoffError(409, "print_handoff_copy_failed", "Could not copy the artifact to the handoff directory.") from None
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _verify_destination(destination: Path, directory: Path, directory_fd: int, identity: tuple[int, int], expected_sha256: str) -> None:
    _verify_directory(directory, directory_fd)
    try:
        fd = os.open(destination.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        with os.fdopen(fd, "rb") as handle:
            if _identity(os.fstat(handle.fileno())) != identity:
                raise PrintHandoffError(409, "print_handoff_unsafe_target", "Handoff target changed before launch.")
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        path_info = os.stat(destination, follow_symlinks=False)
        if _identity(path_info) != identity or digest != expected_sha256:
            raise PrintHandoffError(409, "print_handoff_unsafe_target", "Handoff target changed before launch.")
    except OSError:
        raise PrintHandoffError(409, "print_handoff_unsafe_target", "Handoff target changed before launch.") from None


def _to_windows_path(destination: Path) -> str:
    try:
        completed = subprocess.run(
            ["wslpath", "-w", str(destination)], shell=False, timeout=10, capture_output=True, text=True
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PrintHandoffError(502, "print_handoff_wslpath_failed", "wslpath could not convert the path.") from None
    value = str(completed.stdout or "").strip()
    if (
        completed.returncode != 0
        or _WINDOWS_PATH.match(value) is None
        or _WINDOWS_PATH_FORBIDDEN.search(value) is not None
    ):
        raise PrintHandoffError(502, "print_handoff_wslpath_failed", "wslpath returned an unusable Windows path.")
    return value


def _explorer_binary() -> str:
    if _EXPLORER.is_file():
        return str(_EXPLORER)
    found = shutil.which("explorer.exe")
    # Only the Windows-mounted binary; a PATH entry elsewhere is not explorer.exe.
    if found and _EXPLORER_FALLBACK.match(str(Path(found).resolve())):
        return found
    raise PrintHandoffError(409, "print_handoff_explorer_missing", "explorer.exe is unavailable; check WSL interop.")
