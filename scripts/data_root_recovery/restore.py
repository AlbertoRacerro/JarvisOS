"""Relocation-safe restore for verified JarvisOS snapshots."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from .common import (
    INCLUDED_ROOTS,
    DataRootError,
    _assert_regular_not_symlink,
    _canonical_relative_path,
    _current_migration_id,
    _foreign_key_check,
    _integrity_check,
    _is_absolute_path_string,
    _is_same_or_descendant,
    _json_mentions_source_root,
    _open_regular_file_no_follow,
    _relative_parts_under_root,
    _string_mentions_source_root,
    _table_row_counts,
    rebase_absolute_path,
    sha256_file,
)
from .snapshot import verify_snapshot


def _parse_json_object(raw: str | None, label: str) -> object | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataRootError(f"{label} contains malformed JSON") from exc


def _rebase_command_metadata(
    value: object | None, source_root: str, target_root: str
) -> object | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DataRootError("runner command_json must be an object")
    result = dict(value)
    argv = result.get("argv")
    if argv is not None:
        if not isinstance(argv, list) or not all(
            isinstance(item, str) for item in argv
        ):
            raise DataRootError("runner command_json.argv must be a string list")
        new_argv: list[str] = []
        for index, item in enumerate(argv):
            if index > 0 and _is_absolute_path_string(item):
                if _relative_parts_under_root(item, source_root) is None:
                    raise DataRootError(
                        f"runner command path is outside the snapshot source root: {item}"
                    )
                new_argv.append(rebase_absolute_path(item, source_root, target_root))
            else:
                new_argv.append(item)
        result["argv"] = new_argv
    for key in ("cwd", "working_dir", "script_path", "input_file", "output_dir"):
        item = result.get(key)
        if item is None:
            continue
        if not isinstance(item, str):
            raise DataRootError(f"runner command_json.{key} must be a string")
        result[key] = rebase_absolute_path(item, source_root, target_root)
    if _json_mentions_source_root(result, source_root):
        raise DataRootError(
            "runner command_json contains an undocumented source-root path"
        )
    return result


def _rebase_environment_metadata(
    value: object | None, source_root: str, target_root: str
) -> object | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DataRootError("runner environment_json must be an object")
    result = dict(value)
    for key in ("cwd", "working_dir", "script_path", "input_file", "output_dir"):
        item = result.get(key)
        if item is None:
            continue
        if not isinstance(item, str):
            raise DataRootError(f"runner environment_json.{key} must be a string")
        result[key] = rebase_absolute_path(item, source_root, target_root)
    if _json_mentions_source_root(result, source_root):
        raise DataRootError(
            "runner environment_json contains an undocumented source-root path"
        )
    return result


def _rebase_simulation_payload(
    raw: str | None, source_root: str, target_root: str, *, allow_geometry: bool
) -> str | None:
    if raw is None:
        return None
    value = _parse_json_object(raw, "simulation payload")
    if allow_geometry and isinstance(value, dict):
        geometry = value.get("geometry")
        if geometry is not None:
            if not isinstance(geometry, dict):
                raise DataRootError(
                    "simulation parameter_payload.geometry must be an object"
                )
            geometry = dict(geometry)
            for key in ("step_path", "manifest_path"):
                item = geometry.get(key)
                if item is None:
                    continue
                if not isinstance(item, str):
                    raise DataRootError(f"simulation geometry {key} must be a string")
                geometry[key] = rebase_absolute_path(item, source_root, target_root)
            value = dict(value)
            value["geometry"] = geometry
    if _json_mentions_source_root(value, source_root):
        raise DataRootError(
            "simulation payload contains an undocumented source-root path"
        )
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _rebase_database(database_path: Path, source_root: str, target_root: str) -> None:
    try:
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            if _table_exists(connection, "artifacts"):
                rows = connection.execute(
                    "SELECT id, stored_path FROM artifacts ORDER BY id"
                ).fetchall()
                for record_id, stored_path in rows:
                    if not isinstance(stored_path, str):
                        raise DataRootError("artifacts.stored_path must be text")
                    connection.execute(
                        "UPDATE artifacts SET stored_path=? WHERE id=?",
                        (
                            rebase_absolute_path(stored_path, source_root, target_root),
                            record_id,
                        ),
                    )
            if _table_exists(connection, "runner_jobs"):
                rows = connection.execute(
                    "SELECT id, script_path, working_dir, input_file, output_dir, command_json, environment_json "
                    "FROM runner_jobs ORDER BY id"
                ).fetchall()
                for row in rows:
                    (
                        record_id,
                        script_path,
                        working_dir,
                        input_file,
                        output_dir,
                        command_raw,
                        environment_raw,
                    ) = row
                    rebased_input = (
                        None
                        if input_file is None
                        else rebase_absolute_path(input_file, source_root, target_root)
                    )
                    command = _rebase_command_metadata(
                        _parse_json_object(command_raw, "runner command_json"),
                        source_root,
                        target_root,
                    )
                    environment = _rebase_environment_metadata(
                        _parse_json_object(environment_raw, "runner environment_json"),
                        source_root,
                        target_root,
                    )
                    connection.execute(
                        "UPDATE runner_jobs SET script_path=?, working_dir=?, input_file=?, output_dir=?, "
                        "command_json=?, environment_json=? WHERE id=?",
                        (
                            rebase_absolute_path(script_path, source_root, target_root),
                            rebase_absolute_path(working_dir, source_root, target_root),
                            rebased_input,
                            rebase_absolute_path(output_dir, source_root, target_root),
                            None
                            if command is None
                            else json.dumps(
                                command, sort_keys=True, separators=(",", ":")
                            ),
                            None
                            if environment is None
                            else json.dumps(
                                environment, sort_keys=True, separators=(",", ":")
                            ),
                            record_id,
                        ),
                    )
            if _table_exists(connection, "simulation_runs"):
                rows = connection.execute(
                    "SELECT id, input_payload, parameter_payload, output_payload FROM simulation_runs ORDER BY id"
                ).fetchall()
                for record_id, input_raw, parameter_raw, output_raw in rows:
                    connection.execute(
                        "UPDATE simulation_runs SET input_payload=?, parameter_payload=?, output_payload=? WHERE id=?",
                        (
                            _rebase_simulation_payload(
                                input_raw,
                                source_root,
                                target_root,
                                allow_geometry=False,
                            ),
                            _rebase_simulation_payload(
                                parameter_raw,
                                source_root,
                                target_root,
                                allow_geometry=True,
                            ),
                            _rebase_simulation_payload(
                                output_raw,
                                source_root,
                                target_root,
                                allow_geometry=False,
                            ),
                            record_id,
                        ),
                    )
            _assert_old_root_absent(connection, source_root)
            _integrity_check(connection)
            _foreign_key_check(connection)
            connection.commit()
    except sqlite3.Error as exc:
        raise DataRootError("failed to rebase restored database") from exc


def _assert_old_root_absent(connection: sqlite3.Connection, source_root: str) -> None:
    checks: list[tuple[str, Iterable[tuple[Any, ...]]]] = []
    if _table_exists(connection, "artifacts"):
        checks.append(
            (
                "artifacts.stored_path",
                connection.execute("SELECT stored_path FROM artifacts"),
            )
        )
    if _table_exists(connection, "runner_jobs"):
        checks.append(
            (
                "runner_jobs paths",
                connection.execute(
                    "SELECT script_path, working_dir, input_file, output_dir, command_json, environment_json FROM runner_jobs"
                ),
            )
        )
    if _table_exists(connection, "simulation_runs"):
        checks.append(
            (
                "simulation_runs payloads",
                connection.execute(
                    "SELECT input_payload, parameter_payload, output_payload FROM simulation_runs"
                ),
            )
        )
    for label, rows in checks:
        for row in rows:
            for value in row:
                if isinstance(value, str) and _string_mentions_source_root(
                    value, source_root
                ):
                    raise DataRootError(f"old source-root prefix remains in {label}")


def _verify_restored_database(
    database_path: Path,
    manifest: Mapping[str, Any],
    *,
    final_target_root: Path,
    staged_target_root: Path,
) -> None:
    try:
        with sqlite3.connect(database_path) as connection:
            _integrity_check(connection)
            _foreign_key_check(connection)
            if _table_row_counts(connection) != manifest.get("table_row_counts"):
                raise DataRootError(
                    "restored database row counts differ from the manifest"
                )
            if _current_migration_id(connection) != manifest.get("schema_migration_id"):
                raise DataRootError(
                    "restored database migration id differs from the manifest"
                )
            if _table_exists(connection, "artifacts"):
                rows = connection.execute(
                    "SELECT id, stored_path, sha256 FROM artifacts ORDER BY id"
                ).fetchall()
                for artifact_id, stored_path, digest in rows:
                    if not isinstance(stored_path, str):
                        raise DataRootError(
                            f"artifact {artifact_id} has no stored path"
                        )
                    relative = _relative_parts_under_root(
                        stored_path, str(final_target_root)
                    )
                    if relative is None:
                        raise DataRootError(
                            f"artifact {artifact_id} escapes the restored root"
                        )
                    staged_path = staged_target_root.joinpath(*relative)
                    _assert_regular_not_symlink(staged_path, f"artifact {artifact_id}")
                    if not isinstance(digest, str) or len(digest) != 64:
                        raise DataRootError(
                            f"artifact {artifact_id} has no valid stored SHA-256"
                        )
                    if sha256_file(staged_path) != digest:
                        raise DataRootError(
                            f"artifact {artifact_id} hash mismatch after restore"
                        )
    except sqlite3.Error as exc:
        raise DataRootError("restored database verification failed") from exc


def _copy_snapshot_payload(
    snapshot_dir: Path, partial_root: Path, manifest: Mapping[str, Any]
) -> None:
    partial_root.mkdir(mode=0o700)
    for root_name in INCLUDED_ROOTS:
        (partial_root / root_name).mkdir(parents=True, exist_ok=True)
    for item in manifest["files"]:
        relative = _canonical_relative_path(item["path"])
        source = snapshot_dir.joinpath(*relative.parts)
        target = partial_root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, _ = _open_regular_file_no_follow(source)
        try:
            with handle, target.open("xb") as destination:
                shutil.copyfileobj(handle, destination, length=1024 * 1024)
        except OSError as exc:
            raise DataRootError(
                f"failed to copy snapshot payload: {relative.as_posix()}"
            ) from exc
        if (
            target.stat().st_size != item["size"]
            or sha256_file(target) != item["sha256"]
        ):
            raise DataRootError(
                f"snapshot payload changed during restore: {relative.as_posix()}"
            )


def restore_snapshot(
    *,
    snapshot_dir: Path,
    target_root: Path,
    allow_nonempty_target: bool = False,
    failure_hook: Callable[[str, Path], None] | None = None,
) -> Path:
    snapshot_dir = snapshot_dir.expanduser()
    manifest = verify_snapshot(snapshot_dir)
    supplied_target_root = target_root.expanduser()
    if supplied_target_root.is_symlink():
        raise DataRootError("restore target must not be a symlink")
    target_root = supplied_target_root.resolve(strict=False)
    target_root.parent.mkdir(parents=True, exist_ok=True)
    if _is_same_or_descendant(target_root, snapshot_dir) or _is_same_or_descendant(
        snapshot_dir, target_root
    ):
        raise DataRootError(
            "restore target and source snapshot must not contain one another"
        )
    target_exists = target_root.exists()
    target_nonempty = False
    if target_exists:
        if target_root.is_symlink() or not target_root.is_dir():
            raise DataRootError("restore target must be an ordinary directory")
        target_nonempty = any(target_root.iterdir())
        if target_nonempty and not allow_nonempty_target:
            raise DataRootError(
                "non-empty restore target requires explicit destructive approval"
            )
    token = uuid4().hex[:12]
    partial_root = target_root.parent / f".partial-restore-{target_root.name}-{token}"
    previous_root = target_root.parent / f".previous-{target_root.name}-{token}"
    if partial_root.exists() or previous_root.exists():
        raise DataRootError("restore staging path already exists")
    moved_previous = False
    published = False
    try:
        _copy_snapshot_payload(snapshot_dir, partial_root, manifest)
        source_root = manifest.get("source_root")
        database_filename = manifest.get("database_filename")
        if not isinstance(source_root, str) or not isinstance(database_filename, str):
            raise DataRootError("snapshot manifest has invalid root metadata")
        database_path = partial_root / database_filename
        _rebase_database(database_path, source_root, str(target_root))
        _verify_restored_database(
            database_path,
            manifest,
            final_target_root=target_root,
            staged_target_root=partial_root,
        )
        if failure_hook:
            failure_hook("before_publish", partial_root)
        if target_exists:
            os.replace(target_root, previous_root)
            moved_previous = True
        try:
            os.replace(partial_root, target_root)
            published = True
        except Exception:
            if moved_previous and not target_root.exists():
                os.replace(previous_root, target_root)
                moved_previous = False
            raise
        if moved_previous:
            shutil.rmtree(previous_root, ignore_errors=True)
            moved_previous = False
        return target_root
    except Exception:
        if partial_root.exists() and not published:
            shutil.rmtree(partial_root, ignore_errors=True)
        if moved_previous and previous_root.exists() and not target_root.exists():
            os.replace(previous_root, target_root)
        raise
