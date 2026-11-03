"""Shared fail-closed primitives for JarvisOS data-root recovery."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import shutil
import sqlite3
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
COMPLETION_MARKER = "COMPLETE"
INCLUDED_ROOTS = ("workspaces", "artifacts")
EXCLUDED_ROOTS = ("logs",)
DEFAULT_DATABASE_FILENAME = "jarvisos.db"
DEFAULT_DATA_ROOT = Path(r"C:\JarvisOS")
_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


class DataRootError(RuntimeError):
    """Fail-closed data-root operation error."""


@dataclass(frozen=True, order=True)
class FileRecord:
    path: str
    size: int
    sha256: str

    def as_json(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_dir: Path
    snapshot_id: str
    manifest_sha256: str

    def as_json(self) -> dict[str, str]:
        return {
            "snapshot_dir": str(self.snapshot_dir),
            "snapshot_id": self.snapshot_id,
            "manifest_sha256": self.manifest_sha256,
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _default_settings() -> tuple[Path, str]:
    backend_dir = Path(__file__).resolve().parents[2] / "backend"
    if backend_dir.is_dir():
        backend_text = str(backend_dir)
        if backend_text not in sys.path:
            sys.path.insert(0, backend_text)
        try:
            from app.core.config import get_settings  # type: ignore[import-not-found]

            settings = get_settings()
            return settings.data_root, settings.database_filename
        except (ImportError, AttributeError):
            pass
    return (
        Path(os.getenv("JARVISOS_DATA_ROOT", str(DEFAULT_DATA_ROOT))),
        os.getenv("JARVISOS_DATABASE_FILENAME", DEFAULT_DATABASE_FILENAME),
    )


def _resolved_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_same_or_descendant(path: Path, root: Path) -> bool:
    resolved_path = _resolved_path(path)
    resolved_root = _resolved_path(root)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _validate_snapshot_id(snapshot_id: str) -> str:
    if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise DataRootError(
            "snapshot id must contain only bounded letters, digits, '.', '_', or '-'"
        )
    return snapshot_id


def _validate_database_filename(value: str) -> str:
    if not value or value in {MANIFEST_NAME, COMPLETION_MARKER}:
        raise DataRootError("database filename is invalid")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise DataRootError("database filename must be a basename")
    return value


def _new_snapshot_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid4().hex[:12]}"


def _sha256_stream(handle: Any) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_stream(handle)[1]


def _open_regular_file_no_follow(path: Path) -> tuple[Any, os.stat_result]:
    try:
        path_metadata = os.lstat(path)
    except OSError as exc:
        raise DataRootError(f"cannot inspect source file: {path}") from exc
    if stat.S_ISLNK(path_metadata.st_mode):
        raise DataRootError(f"source symlink is forbidden: {path}")
    if not stat.S_ISREG(path_metadata.st_mode):
        raise DataRootError(f"unsupported non-regular file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DataRootError(f"cannot read source file: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DataRootError(f"unsupported non-regular file: {path}")
        if (
            path_metadata.st_ino
            and metadata.st_ino
            and (path_metadata.st_dev, path_metadata.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise DataRootError(f"source file changed before opening: {path}")
        return os.fdopen(descriptor, "rb", closefd=True), metadata
    except Exception:
        os.close(descriptor)
        raise


def _canonical_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise DataRootError("manifest path must be a non-empty string")
    normalized = value.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or normalized.startswith("//"):
        raise DataRootError(f"manifest path must be relative: {value}")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise DataRootError(f"manifest path is not canonical: {value}")
    if str(candidate) != normalized:
        raise DataRootError(f"manifest path is not normalized: {value}")
    return candidate


def _inventory_tree(source_root: Path) -> list[FileRecord]:
    records: list[FileRecord] = []
    normalized_seen: set[str] = set()

    def visit(directory: Path, relative_directory: PurePosixPath) -> None:
        try:
            directory_stat = os.lstat(directory)
        except OSError as exc:
            raise DataRootError(f"cannot inspect directory: {directory}") from exc
        if stat.S_ISLNK(directory_stat.st_mode):
            raise DataRootError(f"source symlink is forbidden: {directory}")
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise DataRootError(f"expected directory: {directory}")
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise DataRootError(f"cannot list directory: {directory}") from exc
        for entry in entries:
            relative = relative_directory / entry.name
            relative_text = relative.as_posix()
            canonical = _canonical_relative_path(relative_text).as_posix()
            collision_key = canonical.casefold()
            if collision_key in normalized_seen:
                raise DataRootError(f"duplicate normalized relative path: {canonical}")
            normalized_seen.add(collision_key)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise DataRootError(
                    f"cannot inspect source entry: {entry.path}"
                ) from exc
            mode = metadata.st_mode
            if stat.S_ISLNK(mode):
                raise DataRootError(f"source symlink is forbidden: {entry.path}")
            if stat.S_ISDIR(mode):
                visit(Path(entry.path), relative)
                continue
            if not stat.S_ISREG(mode):
                raise DataRootError(f"unsupported special source entry: {entry.path}")
            handle, opened_metadata = _open_regular_file_no_follow(Path(entry.path))
            with handle:
                size, digest = _sha256_stream(handle)
            if size != opened_metadata.st_size:
                raise DataRootError(
                    f"source file changed while inventorying: {entry.path}"
                )
            records.append(FileRecord(path=canonical, size=size, sha256=digest))

    for root_name in INCLUDED_ROOTS:
        path = source_root / root_name
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DataRootError(f"cannot inspect included root: {path}") from exc
        visit(path, PurePosixPath(root_name))
    return sorted(records)


def _copy_inventory(
    source_root: Path, target_root: Path, records: Sequence[FileRecord]
) -> None:
    for root_name in INCLUDED_ROOTS:
        (target_root / root_name).mkdir(parents=True, exist_ok=True)
    for record in records:
        relative = _canonical_relative_path(record.path)
        source = source_root.joinpath(*relative.parts)
        target = target_root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, metadata = _open_regular_file_no_follow(source)
        try:
            with handle, target.open("xb") as destination:
                shutil.copyfileobj(handle, destination, length=1024 * 1024)
        except OSError as exc:
            raise DataRootError(f"failed to copy source file: {source}") from exc
        target_stat = target.stat()
        if metadata.st_size != record.size or target_stat.st_size != record.size:
            raise DataRootError(f"source file size changed while copying: {source}")
        if sha256_file(target) != record.sha256:
            raise DataRootError(f"source file content changed while copying: {source}")


def _assert_regular_not_symlink(path: Path, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise DataRootError(f"missing {label}: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DataRootError(f"{label} must be a regular non-symlink file: {path}")


def _integrity_check(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise DataRootError(f"SQLite integrity_check failed: {rows}")


def _foreign_key_check(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    if rows:
        raise DataRootError(f"SQLite foreign_key_check failed: {rows}")


def _table_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _table_names(connection):
        row = connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
        ).fetchone()
        counts[table] = int(row[0])
    return counts


def _current_migration_id(connection: sqlite3.Connection) -> str | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if exists is None:
        return None
    row = connection.execute(
        "SELECT migration_id FROM schema_migrations ORDER BY migration_id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else str(row[0])


def _canonical_json_bytes(data: Mapping[str, object]) -> bytes:
    return (
        json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _write_exclusive(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _directory_file_inventory(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()

    def visit(directory: Path, relative: PurePosixPath | None = None) -> None:
        metadata = os.lstat(directory)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise DataRootError(f"snapshot contains an invalid directory: {directory}")
        if relative is not None:
            directories.add(relative.as_posix())
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            child_relative = (
                PurePosixPath(entry.name) if relative is None else relative / entry.name
            )
            entry_metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(entry_metadata.st_mode):
                raise DataRootError(f"snapshot symlink is forbidden: {entry.path}")
            if stat.S_ISDIR(entry_metadata.st_mode):
                visit(Path(entry.path), child_relative)
            elif stat.S_ISREG(entry_metadata.st_mode):
                files.add(
                    _canonical_relative_path(child_relative.as_posix()).as_posix()
                )
            else:
                raise DataRootError(f"snapshot special file is forbidden: {entry.path}")

    visit(root)
    return files, directories


def _load_manifest(snapshot_dir: Path) -> tuple[dict[str, Any], bytes]:
    manifest_path = snapshot_dir / MANIFEST_NAME
    marker_path = snapshot_dir / COMPLETION_MARKER
    _assert_regular_not_symlink(manifest_path, "manifest")
    _assert_regular_not_symlink(marker_path, "completion marker")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DataRootError("snapshot metadata is unreadable or malformed") from exc
    if not isinstance(manifest, dict) or not isinstance(marker, dict):
        raise DataRootError("snapshot metadata must be JSON objects")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise DataRootError("unsupported manifest schema version")
    if manifest.get("completion_state") != "complete":
        raise DataRootError("snapshot manifest is not complete")
    snapshot_id = manifest.get("snapshot_id")
    if not isinstance(snapshot_id, str):
        raise DataRootError("manifest snapshot id is missing")
    _validate_snapshot_id(snapshot_id)
    expected_manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if marker != {"manifest_sha256": expected_manifest_sha, "snapshot_id": snapshot_id}:
        raise DataRootError("completion marker does not bind the manifest")
    return manifest, manifest_bytes


def _path_flavor(value: str) -> str:
    return "windows" if _WINDOWS_ABSOLUTE_RE.match(value) else "posix"


def _pure_path(value: str, flavor: str) -> PureWindowsPath | PurePosixPath:
    return PureWindowsPath(value) if flavor == "windows" else PurePosixPath(value)


def _is_absolute_path_string(value: str) -> bool:
    return _pure_path(value, _path_flavor(value)).is_absolute()


def _absolute_parts(value: str, flavor: str) -> tuple[str, ...]:
    path = _pure_path(value, flavor)
    if not path.is_absolute():
        raise DataRootError(f"canonical stored path is not absolute: {value}")
    return path.parts


def _relative_parts_under_root(value: str, source_root: str) -> tuple[str, ...] | None:
    source_flavor = _path_flavor(source_root)
    if _path_flavor(value) != source_flavor:
        return None
    value_path = _pure_path(value, source_flavor)
    source_path = _pure_path(source_root, source_flavor)
    if not value_path.is_absolute() or not source_path.is_absolute():
        return None
    value_parts = value_path.parts
    root_parts = source_path.parts
    if len(value_parts) < len(root_parts):
        return None
    if source_flavor == "windows":
        equal = all(
            ntpath.normcase(left) == ntpath.normcase(right)
            for left, right in zip(value_parts, root_parts, strict=False)
        )
    else:
        equal = value_parts[: len(root_parts)] == root_parts
    return value_parts[len(root_parts) :] if equal else None


def rebase_absolute_path(value: str, source_root: str, target_root: str) -> str:
    relative = _relative_parts_under_root(value, source_root)
    if relative is None:
        raise DataRootError(
            f"canonical path is outside the snapshot source root: {value}"
        )
    target_flavor = _path_flavor(target_root)
    target = _pure_path(target_root, target_flavor)
    return str(target.joinpath(*relative))


def _string_mentions_source_root(value: str, source_root: str) -> bool:
    if _relative_parts_under_root(value, source_root) is not None:
        return True
    normalized_value = value.replace("\\", "/")
    normalized_root = source_root.rstrip("/\\").replace("\\", "/")
    if not normalized_root:
        return False
    if _path_flavor(source_root) == "windows":
        normalized_value = normalized_value.casefold()
        normalized_root = normalized_root.casefold()
    return (
        re.search(re.escape(normalized_root) + r"(?=$|/)", normalized_value) is not None
    )


def _json_mentions_source_root(value: object, source_root: str) -> bool:
    if isinstance(value, str):
        return _string_mentions_source_root(value, source_root)
    if isinstance(value, list):
        return any(_json_mentions_source_root(item, source_root) for item in value)
    if isinstance(value, dict):
        return any(
            _json_mentions_source_root(item, source_root) for item in value.values()
        )
    return False
