"""Move a whole data root to a new place, verified, without touching the source (spec 378)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .common import (
    DataRootError,
    _canonical_json_bytes,
    _current_migration_id,
    _default_settings,
    _integrity_check,
    _is_same_or_descendant,
    _table_row_counts,
    _utc_now,
    _validate_database_filename,
    _write_exclusive,
)
from .restore import _rebase_database, _verify_restored_database

MOVE_RECORD = "MOVED_FROM.json"
CONFIG_SUFFIXES = frozenset({".json", ".toml", ".env", ".ini", ".yaml", ".yml"})
HISTORY_ROOTS = frozenset({"logs", "artifacts", "workspaces"})
SQLITE_SUFFIXES = frozenset({".sqlite3", ".sqlite", ".db"})
_CHUNK = 1024 * 1024


@dataclass(frozen=True, order=True)
class Entry:
    """One tree entry by relative POSIX path: a directory, a regular file (size, SHA-256) or a relative link."""

    path: str
    kind: str
    size: int = 0
    digest: str = ""

    def as_json(self) -> dict[str, object]:
        return {"path": self.path, "kind": self.kind, "size": self.size, "digest": self.digest}


def _database_entries(database_filename: str) -> set[str]:
    return {database_filename, *(f"{database_filename}{suffix}" for suffix in ("-wal", "-shm", "-journal"))}


def _hash_file(path: Path, relative: str) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DataRootError(f"cannot read file: {relative}") from exc
    digest, size = hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def walk_tree(root: Path, skip: Iterable[str] = ()) -> dict[str, Entry]:
    """Inventory every entry under ``root`` without following links; fail closed on anything not copyable."""
    skipped = set(skip)
    entries: dict[str, Entry] = {}

    def visit(directory: Path, prefix: str) -> None:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise DataRootError(f"cannot list directory: {prefix or '.'}") from exc
        for child in children:
            relative = f"{prefix}/{child.name}" if prefix else child.name
            if relative in skipped:
                continue
            mode = child.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(child.path)
                resolved = os.path.normpath(os.path.join(os.path.dirname(relative), target))
                if os.path.isabs(target) or resolved == ".." or resolved.startswith("../"):
                    raise DataRootError(f"symlink leaves the data root: {relative}")
                entries[relative] = Entry(relative, "link", 0, target)
            elif stat.S_ISDIR(mode):
                entries[relative] = Entry(relative, "dir")
                visit(Path(child.path), relative)
            elif stat.S_ISREG(mode):
                size, digest = _hash_file(Path(child.path), relative)
                entries[relative] = Entry(relative, "file", size, digest)
            else:
                raise DataRootError(f"unsupported special file: {relative}")

    visit(root, "")
    return entries


def _copy_entries(source_root: Path, target_root: Path, entries: dict[str, Entry]) -> None:
    for entry in sorted(entries.values()):
        source, target = source_root / entry.path, target_root / entry.path
        if entry.kind == "dir":
            target.mkdir(mode=0o700, exist_ok=True)
        elif entry.kind == "link":
            os.symlink(entry.digest, target)
        else:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            try:
                with os.fdopen(os.open(source, flags), "rb") as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=_CHUNK)
            except OSError as exc:
                raise DataRootError(f"failed to copy file: {entry.path}") from exc
            shutil.copystat(source, target, follow_symlinks=False)


def _mentions(path: Path, needle: bytes) -> bool:
    overlap = len(needle) - 1
    tail = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            if needle in tail + chunk:
                return True
            tail = chunk[-overlap:] if overlap else b""
    return False


def scan_old_root(root: Path, entries: dict[str, Entry], old_root: str) -> tuple[list[str], list[str]]:
    """Return (blocking, reported) relative paths of files that still name the old root."""
    needle = old_root.encode("utf-8")
    blocking, reported = [], []
    for entry in sorted(entries.values()):
        if entry.kind != "file" or not _mentions(root / entry.path, needle):
            continue
        top = entry.path.split("/", 1)[0]
        is_config = Path(entry.path).suffix.lower() in CONFIG_SUFFIXES and top not in HISTORY_ROOTS
        (blocking if is_config else reported).append(entry.path)
    return blocking, reported


def _manifest_digest(entries: dict[str, Entry], row_counts: dict[str, int]) -> str:
    payload = {"entries": [entry.as_json() for entry in sorted(entries.values())], "table_row_counts": row_counts}
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _check_other_databases(root: Path, entries: dict[str, Entry]) -> None:
    for entry in entries.values():
        if entry.kind == "file" and Path(entry.path).suffix.lower() in SQLITE_SUFFIXES:
            with (root / entry.path).open("rb") as handle:
                if handle.read(16) != b"SQLite format 3\x00":
                    continue
            try:
                with sqlite3.connect(f"file:{(root / entry.path).as_posix()}?mode=ro", uri=True) as connection:
                    _integrity_check(connection)
            except (sqlite3.Error, DataRootError) as exc:
                raise DataRootError(f"database integrity check failed: {entry.path}") from exc


def _roots(source_root: Path, target_root: Path) -> tuple[Path, Path]:
    source = source_root.expanduser()
    if source.is_symlink() or not source.is_dir():
        raise DataRootError("source data root must be an existing non-symlink directory")
    target = target_root.expanduser()
    if target.is_symlink():
        raise DataRootError("target root must not be a symlink")
    source, target = source.resolve(), target.resolve(strict=False)
    if _is_same_or_descendant(target, source) or _is_same_or_descendant(source, target):
        raise DataRootError("source and target roots must not contain one another")
    return source, target


def move_data_root(
    *,
    source_root: Path,
    target_root: Path,
    database_filename: str | None = None,
    failure_hook: Callable[[str, Path], None] | None = None,
) -> dict[str, Any]:
    database_filename = _validate_database_filename(database_filename or _default_settings()[1])
    source, target = _roots(source_root, target_root)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise DataRootError("target root must be absent or an empty directory")
    database = source / database_filename
    if database.is_symlink() or not database.is_file():
        raise DataRootError("source database must be a regular file")
    skip = _database_entries(database_filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.parent / f".partial-move-{target.name}-{uuid4().hex[:12]}"
    partial.mkdir(mode=0o700)
    # On any failure the partial directory stays for diagnosis; it is never renamed to look complete.
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as source_connection:
        source_connection.execute("PRAGMA query_only = ON")
        version_before = int(source_connection.execute("PRAGMA data_version").fetchone()[0])
        before = walk_tree(source, skip)
        with sqlite3.connect(partial / database_filename) as copy_connection:
            source_connection.backup(copy_connection)
            copy_connection.commit()
            copy_connection.execute("PRAGMA journal_mode = DELETE")
        _copy_entries(source, partial, before)
        if failure_hook:
            failure_hook("after_copy", partial)
        if walk_tree(source, skip) != before:
            raise DataRootError("source files changed during the move; stop every writer and retry")
        if int(source_connection.execute("PRAGMA data_version").fetchone()[0]) != version_before:
            raise DataRootError("source database changed during the move; stop every writer and retry")
    copied_database = partial / database_filename
    with sqlite3.connect(copied_database) as connection:
        row_counts = _table_row_counts(connection)
        migration_id = _current_migration_id(connection)
    _rebase_database(copied_database, str(source), str(target))
    _verify_restored_database(
        copied_database,
        {"table_row_counts": row_counts, "schema_migration_id": migration_id},
        final_target_root=target,
        staged_target_root=partial,
    )
    if failure_hook:
        failure_hook("before_verify", partial)
    if walk_tree(partial, skip) != before:
        raise DataRootError("copied tree differs from the source")
    _check_other_databases(partial, before)
    blocking, reported = scan_old_root(partial, before, str(source))
    if blocking:
        raise DataRootError("configuration files still name the old root: " + ", ".join(blocking))
    files = [entry for entry in before.values() if entry.kind == "file"]
    result = {
        "target_root": str(target),
        "files": len(files) + 1,
        "bytes": sum(entry.size for entry in files) + copied_database.stat().st_size,
        "manifest_sha256": _manifest_digest(before, row_counts),
        "old_root_mentions": reported,
    }
    if failure_hook:
        failure_hook("before_publish", partial)
    record = {"source_root": str(source), "moved_at": _utc_now(), "database_filename": database_filename, **result}
    _write_exclusive(partial / MOVE_RECORD, _canonical_json_bytes(record))
    if target.exists():
        target.rmdir()
    os.replace(partial, target)
    return result


def verify_move(*, source_root: Path, target_root: Path) -> dict[str, Any]:
    """Read-only comparison after the app restarted on the target. Missing files fail; changed or added are listed."""
    source, target = _roots(source_root, target_root)
    try:
        record = json.loads((target / MOVE_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataRootError(f"target has no readable {MOVE_RECORD}") from exc
    if record.get("source_root") != str(source):
        raise DataRootError("target was not moved from this source root")
    database_filename = _validate_database_filename(str(record.get("database_filename", "")))
    skip = _database_entries(database_filename)
    old, new = walk_tree(source, skip), walk_tree(target, skip | {MOVE_RECORD})
    missing = sorted(set(old) - set(new))
    with sqlite3.connect(f"file:{(target / database_filename).as_posix()}?mode=ro", uri=True) as connection:
        _integrity_check(connection)
    return {
        "ok": not missing,
        "missing": missing,
        "changed": sorted(path for path in set(old) & set(new) if old[path] != new[path]),
        "added": sorted(set(new) - set(old)),
    }
