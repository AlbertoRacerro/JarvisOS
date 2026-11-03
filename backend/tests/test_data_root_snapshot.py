from __future__ import annotations

import sqlite3
from pathlib import Path

import jarvisos_data_root as jdr
import pytest
from data_root_recovery_support import digest, seed_data_root


def test_wal_snapshot_is_single_file_and_contains_uncheckpointed_commit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seed_data_root(source)
    writer = sqlite3.connect(source / "jarvisos.db")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO workspaces VALUES (?)", ("wal-row",))
    writer.commit()

    result = jdr.create_snapshot(
        source_root=source,
        destination=tmp_path / "snapshots",
        snapshot_id="wal",
    )

    with sqlite3.connect(result.snapshot_dir / "jarvisos.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workspaces WHERE id=?", ("wal-row",)
        ).fetchone() == (1,)
    assert not (result.snapshot_dir / "jarvisos.db-wal").exists()
    assert not (result.snapshot_dir / "jarvisos.db-shm").exists()
    writer.close()


def test_snapshot_manifest_hashes_and_logs_exclusion(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seed_data_root(source)
    result = jdr.create_snapshot(
        source_root=source,
        destination=tmp_path / "snapshots",
        snapshot_id="manifest",
    )

    manifest = jdr.verify_snapshot(result.snapshot_dir)
    assert manifest["excluded_roots"] == ["logs"]
    assert not (result.snapshot_dir / "logs").exists()
    for item in manifest["files"]:
        path = result.snapshot_dir.joinpath(*Path(item["path"]).parts)
        assert path.stat().st_size == item["size"]
        assert digest(path) == item["sha256"]


def test_source_file_mutation_fails_without_publishing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seed_data_root(source)

    def mutate(stage: str, root: Path, _connection: sqlite3.Connection) -> None:
        if stage == "after_database_backup":
            (root / "artifacts/ws/report.json").write_text("changed", encoding="utf-8")

    with pytest.raises(jdr.DataRootError):
        jdr.create_snapshot(
            source_root=source,
            destination=tmp_path / "snapshots",
            snapshot_id="file-mutation",
            mutation_hook=mutate,
        )
    assert not (tmp_path / "snapshots/snapshot-file-mutation").exists()


def test_source_database_mutation_fails_without_publishing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seed_data_root(source)

    def mutate(stage: str, root: Path, _connection: sqlite3.Connection) -> None:
        if stage == "after_database_backup":
            with sqlite3.connect(root / "jarvisos.db") as writer:
                writer.execute("INSERT INTO workspaces VALUES (?)", ("late",))
                writer.commit()

    with pytest.raises(jdr.DataRootError, match="database changed"):
        jdr.create_snapshot(
            source_root=source,
            destination=tmp_path / "snapshots",
            snapshot_id="db-mutation",
            mutation_hook=mutate,
        )
    assert not (tmp_path / "snapshots/snapshot-db-mutation").exists()
