"""Atomic snapshot creation and verification."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from .common import (
    COMPLETION_MARKER,
    EXCLUDED_ROOTS,
    INCLUDED_ROOTS,
    MANIFEST_NAME,
    MANIFEST_SCHEMA_VERSION,
    DataRootError,
    FileRecord,
    SnapshotResult,
    _assert_regular_not_symlink,
    _canonical_json_bytes,
    _canonical_relative_path,
    _copy_inventory,
    _current_migration_id,
    _default_settings,
    _directory_file_inventory,
    _foreign_key_check,
    _integrity_check,
    _inventory_tree,
    _is_same_or_descendant,
    _load_manifest,
    _new_snapshot_id,
    _path_flavor,
    _pure_path,
    _table_row_counts,
    _utc_now,
    _validate_database_filename,
    _validate_snapshot_id,
    _write_exclusive,
    sha256_file,
)


def verify_snapshot(
    snapshot_dir: Path, *, allow_partial_name: bool = False
) -> dict[str, Any]:
    supplied_snapshot_dir = snapshot_dir.expanduser()
    try:
        supplied_metadata = os.lstat(supplied_snapshot_dir)
    except OSError as exc:
        raise DataRootError(
            f"snapshot directory is missing: {supplied_snapshot_dir}"
        ) from exc
    if stat.S_ISLNK(supplied_metadata.st_mode):
        raise DataRootError("snapshot path must not be a symlink")
    snapshot_dir = supplied_snapshot_dir.resolve(strict=True)
    if snapshot_dir.name.startswith(".partial-") and not allow_partial_name:
        raise DataRootError(
            "partial snapshot directories are not valid completed snapshots"
        )
    try:
        snapshot_metadata = os.lstat(snapshot_dir)
    except OSError as exc:
        raise DataRootError(f"snapshot directory is missing: {snapshot_dir}") from exc
    if stat.S_ISLNK(snapshot_metadata.st_mode) or not stat.S_ISDIR(
        snapshot_metadata.st_mode
    ):
        raise DataRootError("snapshot path must be a non-symlink directory")
    manifest, _ = _load_manifest(snapshot_dir)
    database_filename = manifest.get("database_filename")
    if not isinstance(database_filename, str):
        raise DataRootError("manifest database filename is invalid")
    _validate_database_filename(database_filename)
    source_root = manifest.get("source_root")
    if (
        not isinstance(source_root, str)
        or not _pure_path(source_root, _path_flavor(source_root)).is_absolute()
    ):
        raise DataRootError("manifest source root must be an absolute path")
    snapshot_id = str(manifest["snapshot_id"])
    expected_name = (
        f".partial-{snapshot_id}" if allow_partial_name else f"snapshot-{snapshot_id}"
    )
    if snapshot_dir.name != expected_name:
        raise DataRootError("snapshot directory name does not match the manifest id")
    included_roots = manifest.get("included_roots")
    excluded_roots = manifest.get("excluded_roots")
    if included_roots != list(INCLUDED_ROOTS) or excluded_roots != list(EXCLUDED_ROOTS):
        raise DataRootError("manifest root policy is unsupported")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise DataRootError("manifest file inventory is missing")
    records: list[FileRecord] = []
    seen: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict):
            raise DataRootError("manifest file entry must be an object")
        path_value = item.get("path")
        size = item.get("size")
        digest = item.get("sha256")
        if not isinstance(path_value, str) or not isinstance(size, int) or size < 0:
            raise DataRootError("manifest file entry has invalid path or size")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DataRootError("manifest file entry has invalid SHA-256")
        canonical = _canonical_relative_path(path_value)
        key = canonical.as_posix().casefold()
        if key in seen:
            raise DataRootError("manifest contains duplicate normalized paths")
        seen.add(key)
        first = canonical.parts[0]
        if first not in INCLUDED_ROOTS and canonical.as_posix() != database_filename:
            raise DataRootError(
                f"manifest path is outside the allowed snapshot roots: {path_value}"
            )
        if canonical.as_posix() in {MANIFEST_NAME, COMPLETION_MARKER}:
            raise DataRootError("manifest cannot list its own metadata files")
        records.append(FileRecord(canonical.as_posix(), size, digest))
    if database_filename not in {record.path for record in records}:
        raise DataRootError("snapshot database is absent from the manifest")
    actual_files, actual_dirs = _directory_file_inventory(snapshot_dir)
    expected_files = {record.path for record in records} | {
        MANIFEST_NAME,
        COMPLETION_MARKER,
    }
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise DataRootError(
            f"snapshot file set mismatch; missing={missing}, extra={extra}"
        )
    allowed_dirs = set(INCLUDED_ROOTS)
    for record in records:
        relative = PurePosixPath(record.path)
        for parent in relative.parents:
            if str(parent) != ".":
                allowed_dirs.add(parent.as_posix())
    if not actual_dirs.issubset(allowed_dirs):
        raise DataRootError(
            f"snapshot contains forbidden directories: {sorted(actual_dirs - allowed_dirs)}"
        )
    for record in records:
        path = snapshot_dir.joinpath(*PurePosixPath(record.path).parts)
        _assert_regular_not_symlink(path, "snapshot file")
        if path.stat().st_size != record.size or sha256_file(path) != record.sha256:
            raise DataRootError(f"snapshot file hash or size mismatch: {record.path}")
    database_path = snapshot_dir / database_filename
    try:
        with sqlite3.connect(
            f"file:{database_path.as_posix()}?mode=ro", uri=True
        ) as connection:
            _integrity_check(connection)
            counts = _table_row_counts(connection)
            migration = _current_migration_id(connection)
    except sqlite3.Error as exc:
        raise DataRootError("snapshot database verification failed") from exc
    if manifest.get("table_row_counts") != counts:
        raise DataRootError("snapshot database row counts differ from the manifest")
    if manifest.get("schema_migration_id") != migration:
        raise DataRootError("snapshot database migration id differs from the manifest")
    return manifest


def create_snapshot(
    *,
    source_root: Path | None,
    destination: Path,
    database_filename: str | None = None,
    snapshot_id: str | None = None,
    keep_last: int | None = None,
    mutation_hook: Callable[[str, Path, sqlite3.Connection], None] | None = None,
) -> SnapshotResult:
    default_root, default_database_filename = _default_settings()
    supplied_source_root = (source_root or default_root).expanduser()
    if supplied_source_root.is_symlink():
        raise DataRootError("source data root must not be a symlink")
    source_root = supplied_source_root.resolve(strict=False)
    database_filename = _validate_database_filename(
        database_filename or default_database_filename
    )
    if keep_last is not None and keep_last < 1:
        raise DataRootError("keep-last must be at least 1")
    supplied_destination = destination.expanduser()
    if supplied_destination.is_symlink():
        raise DataRootError("snapshot destination must not be a symlink")
    destination = supplied_destination.resolve(strict=False)
    if _is_same_or_descendant(destination, source_root):
        raise DataRootError("snapshot destination must be outside the source data root")
    if not source_root.is_dir() or source_root.is_symlink():
        raise DataRootError("source data root must be a non-symlink directory")
    database_path = source_root / database_filename
    _assert_regular_not_symlink(database_path, "source database")
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise DataRootError("snapshot destination must be a non-symlink directory")
    snapshot_id = _validate_snapshot_id(snapshot_id or _new_snapshot_id())
    final_dir = destination / f"snapshot-{snapshot_id}"
    partial_dir = destination / f".partial-{snapshot_id}"
    if final_dir.exists() or partial_dir.exists():
        raise DataRootError("snapshot target already exists")
    partial_dir.mkdir(mode=0o700)
    completed = False
    try:
        source_uri = f"file:{database_path.as_posix()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            data_version_before = int(
                source_connection.execute("PRAGMA data_version").fetchone()[0]
            )
            inventory_before = _inventory_tree(source_root)
            if mutation_hook:
                mutation_hook("after_inventory", source_root, source_connection)
            snapshot_db = partial_dir / database_filename
            with sqlite3.connect(snapshot_db) as snapshot_connection:
                source_connection.backup(snapshot_connection)
                snapshot_connection.commit()
                snapshot_connection.execute("PRAGMA journal_mode = DELETE")
            if mutation_hook:
                mutation_hook("after_database_backup", source_root, source_connection)
            _copy_inventory(source_root, partial_dir, inventory_before)
            if mutation_hook:
                mutation_hook("after_file_copy", source_root, source_connection)
            inventory_after = _inventory_tree(source_root)
            data_version_after = int(
                source_connection.execute("PRAGMA data_version").fetchone()[0]
            )
            if inventory_before != inventory_after:
                raise DataRootError("source file inventory changed during snapshot")
            if data_version_before != data_version_after:
                raise DataRootError("source database changed during snapshot")
        with sqlite3.connect(snapshot_db) as snapshot_connection:
            _integrity_check(snapshot_connection)
            _foreign_key_check(snapshot_connection)
            row_counts = _table_row_counts(snapshot_connection)
            migration_id = _current_migration_id(snapshot_connection)
        database_record = FileRecord(
            path=database_filename,
            size=snapshot_db.stat().st_size,
            sha256=sha256_file(snapshot_db),
        )
        copied_records = [
            FileRecord(
                record.path,
                (partial_dir.joinpath(*PurePosixPath(record.path).parts))
                .stat()
                .st_size,
                sha256_file(partial_dir.joinpath(*PurePosixPath(record.path).parts)),
            )
            for record in inventory_before
        ]
        manifest: dict[str, object] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "created_at_utc": _utc_now(),
            "source_root": str(source_root),
            "database_filename": database_filename,
            "schema_migration_id": migration_id,
            "table_row_counts": row_counts,
            "included_roots": list(INCLUDED_ROOTS),
            "excluded_roots": list(EXCLUDED_ROOTS),
            "files": [
                record.as_json()
                for record in sorted([database_record, *copied_records])
            ],
            "completion_state": "complete",
        }
        manifest_bytes = _canonical_json_bytes(manifest)
        _write_exclusive(partial_dir / MANIFEST_NAME, manifest_bytes)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        marker_bytes = _canonical_json_bytes(
            {"manifest_sha256": manifest_sha, "snapshot_id": snapshot_id}
        )
        _write_exclusive(partial_dir / COMPLETION_MARKER, marker_bytes)
        verify_snapshot(partial_dir, allow_partial_name=True)
        os.replace(partial_dir, final_dir)
        completed = True
        verify_snapshot(final_dir)
        if keep_last is not None:
            _rotate_snapshots(destination, keep_last)
        return SnapshotResult(final_dir, snapshot_id, manifest_sha)
    except Exception:
        if partial_dir.exists() and not completed:
            # An incomplete directory is never represented as complete. Remove only
            # the operation-owned partial; source and prior snapshots are untouched.
            shutil.rmtree(partial_dir, ignore_errors=True)
        raise


def _rotate_snapshots(destination: Path, keep_last: int) -> None:
    if keep_last < 1:
        raise DataRootError("keep-last must be at least 1")
    verified: list[tuple[str, Path]] = []
    for child in destination.iterdir():
        if (
            child.name.startswith(".partial-")
            or child.is_symlink()
            or not child.is_dir()
        ):
            continue
        try:
            manifest = verify_snapshot(child)
        except DataRootError:
            continue
        created = manifest.get("created_at_utc")
        if isinstance(created, str):
            verified.append((f"{created}\0{child.name}", child))
    verified.sort()
    for _, path in verified[:-keep_last]:
        shutil.rmtree(path)
