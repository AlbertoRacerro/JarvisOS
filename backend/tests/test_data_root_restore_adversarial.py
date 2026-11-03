from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import jarvisos_data_root as jdr
import pytest
from data_root_recovery_support import seed_data_root

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "jarvisos_data_root.py"


def test_restore_rejects_path_outside_source_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seed_data_root(source)
    with sqlite3.connect(source / "jarvisos.db") as connection:
        connection.execute(
            "UPDATE artifacts SET stored_path=? WHERE id=?",
            (str(tmp_path / "outside.txt"), "a1"),
        )
        connection.commit()
    result = jdr.create_snapshot(
        source_root=source,
        destination=tmp_path / "snapshots",
        snapshot_id="outside",
    )
    target = tmp_path / "target"
    with pytest.raises(jdr.DataRootError, match="outside"):
        jdr.restore_snapshot(snapshot_dir=result.snapshot_dir, target_root=target)
    assert not target.exists()
    assert result.snapshot_dir.exists()


def test_restore_rejects_absolute_runner_argv_outside_source_root(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seed_data_root(source)
    with sqlite3.connect(source / "jarvisos.db") as connection:
        command = json.loads(
            connection.execute("SELECT command_json FROM runner_jobs").fetchone()[0]
        )
        command["argv"].append(str(tmp_path / "outside-input.json"))
        connection.execute(
            "UPDATE runner_jobs SET command_json=?",
            (json.dumps(command),),
        )
        connection.commit()
    result = jdr.create_snapshot(
        source_root=source,
        destination=tmp_path / "snapshots",
        snapshot_id="outside-argv",
    )
    target = tmp_path / "target"

    with pytest.raises(jdr.DataRootError, match="outside"):
        jdr.restore_snapshot(snapshot_dir=result.snapshot_dir, target_root=target)
    assert not target.exists()
    assert result.snapshot_dir.exists()


def test_restore_rejects_registered_artifact_without_sha256(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seed_data_root(source)
    with sqlite3.connect(source / "jarvisos.db") as connection:
        connection.execute("UPDATE artifacts SET sha256=NULL WHERE id='a1'")
        connection.commit()
    result = jdr.create_snapshot(
        source_root=source,
        destination=tmp_path / "snapshots",
        snapshot_id="missing-artifact-sha",
    )
    target = tmp_path / "target"

    with pytest.raises(jdr.DataRootError, match="SHA-256"):
        jdr.restore_snapshot(snapshot_dir=result.snapshot_dir, target_root=target)
    assert not target.exists()
    assert result.snapshot_dir.exists()


def test_restore_rejects_undocumented_old_root_occurrence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seed_data_root(source)
    with sqlite3.connect(source / "jarvisos.db") as connection:
        connection.execute(
            "UPDATE runner_jobs SET command_json=?",
            (
                json.dumps(
                    {
                        "executable": "python",
                        "argv": ["python"],
                        "mystery": str(source / "secret"),
                    }
                ),
            ),
        )
        connection.commit()
    result = jdr.create_snapshot(
        source_root=source,
        destination=tmp_path / "snapshots",
        snapshot_id="unknown",
    )
    target = tmp_path / "target"
    with pytest.raises(jdr.DataRootError, match="undocumented"):
        jdr.restore_snapshot(snapshot_dir=result.snapshot_dir, target_root=target)
    assert not target.exists()
    assert result.snapshot_dir.exists()


def test_snapshot_and_restore_target_symlinks_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seed_data_root(source)
    result = jdr.create_snapshot(
        source_root=source,
        destination=tmp_path / "snapshots",
        snapshot_id="links",
    )
    snapshot_link = tmp_path / "snapshot-link"
    try:
        os.symlink(result.snapshot_dir, snapshot_link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(jdr.DataRootError, match="symlink"):
        jdr.verify_snapshot(snapshot_link)

    real_target = tmp_path / "real-target"
    real_target.mkdir()
    target_link = tmp_path / "target-link"
    os.symlink(real_target, target_link, target_is_directory=True)
    with pytest.raises(jdr.DataRootError, match="symlink"):
        jdr.restore_snapshot(snapshot_dir=result.snapshot_dir, target_root=target_link)


def test_cli_snapshot_verify_restore(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seed_data_root(source)
    destination = tmp_path / "snapshots"
    created = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "snapshot",
            "--source-root",
            str(source),
            "--destination",
            str(destination),
            "--snapshot-id",
            "cli",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    snapshot = destination / "snapshot-cli"
    checked = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", str(snapshot)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    target = tmp_path / "restored"
    restored = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "restore",
            str(snapshot),
            "--target-root",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert restored.returncode == 0, restored.stderr
    assert (target / "jarvisos.db").is_file()
