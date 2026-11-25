"""Spec 378: move the whole data root, verified, without touching the source."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import jarvisos_data_root as jdr
import pytest
from data_root_recovery.move import MOVE_RECORD, walk_tree
from data_root_recovery_support import seed_data_root


def _full_root(root: Path) -> None:
    root.mkdir()
    seed_data_root(root)
    (root / "settings").mkdir()
    (root / "settings" / "local_models.json").write_text('{"model": "fake"}', encoding="utf-8")
    (root / "secrets").mkdir()
    (root / "secrets" / "token").write_text("fake-secret-value", encoding="utf-8")
    blobs = root / "models" / "cache" / "blobs"
    snapshot = root / "models" / "cache" / "snapshots" / "rev"
    blobs.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    (blobs / "abc").write_bytes(b"weights")
    os.symlink("../../blobs/abc", snapshot / "model.onnx")
    (root / "bluecad" / "empty").mkdir(parents=True)
    (root / "logs" / "app.log").write_text(f"started on {root}\n", encoding="utf-8")
    with sqlite3.connect(root / "retrieval-index.sqlite3") as connection:
        connection.execute("CREATE TABLE chunks(id TEXT)")
        connection.execute("INSERT INTO chunks VALUES ('one')")


def _snapshot(root: Path) -> dict[str, object]:
    """Independent byte-level view of a root; SQLite's own -wal/-shm sidecars of the main database are ignored."""
    state: dict[str, object] = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative in {"jarvisos.db-wal", "jarvisos.db-shm"}:
                continue
            if path.is_symlink():
                state[relative] = ("link", os.readlink(path))
            elif path.is_file():
                state[relative] = ("file", path.read_bytes())
            else:
                state[relative] = ("dir",)
    return state


def test_move_copies_every_entry_rebases_paths_and_leaves_the_source_unchanged(tmp_path: Path) -> None:
    source, target = tmp_path / "live", tmp_path / "vault" / "jarvis-data"
    _full_root(source)
    before = _snapshot(source)

    result = jdr.move_data_root(source_root=source, target_root=target)

    assert _snapshot(source) == before
    assert result["target_root"] == str(target)
    assert result["old_root_mentions"] == ["logs/app.log"]
    copied = walk_tree(target, {"jarvisos.db", MOVE_RECORD})
    assert copied == walk_tree(source, {"jarvisos.db", "jarvisos.db-wal", "jarvisos.db-shm"})
    assert os.readlink(target / "models/cache/snapshots/rev/model.onnx") == "../../blobs/abc"
    assert (target / "models/cache/snapshots/rev/model.onnx").read_bytes() == b"weights"
    assert (target / "bluecad" / "empty").is_dir()
    with sqlite3.connect(target / "jarvisos.db") as connection:
        stored = [row[0] for row in connection.execute("SELECT stored_path FROM artifacts")]
        script = connection.execute("SELECT script_path FROM runner_jobs").fetchone()[0]
    assert stored and all(Path(path).is_relative_to(target) for path in stored)
    assert Path(script).is_relative_to(target)
    record = json.loads((target / MOVE_RECORD).read_text(encoding="utf-8"))
    assert record["source_root"] == str(source) and record["manifest_sha256"] == result["manifest_sha256"]
    assert "fake-secret-value" not in json.dumps(result)

    verified = jdr.verify_move(source_root=source, target_root=target)
    assert verified == {"ok": True, "missing": [], "changed": [], "added": []}
    (target / "settings" / "local_models.json").write_text('{"model": "other"}', encoding="utf-8")
    (target / "logs" / "new.log").write_text("x", encoding="utf-8")
    verified = jdr.verify_move(source_root=source, target_root=target)
    assert verified["ok"] and verified["changed"] == ["settings/local_models.json"]
    assert verified["added"] == ["logs/new.log"]
    (target / "secrets" / "token").unlink()
    assert jdr.verify_move(source_root=source, target_root=target)["missing"] == ["secrets/token"]


@pytest.mark.parametrize("case", ["target_in_source", "source_in_target", "nonempty_target", "escaping_link",
                                  "absolute_link", "config_names_old_root"])
def test_move_refusals_publish_nothing_and_leave_the_source_unchanged(tmp_path: Path, case: str) -> None:
    source = tmp_path / "live"
    _full_root(source)
    target = tmp_path / "vault" / "jarvis-data"
    if case == "target_in_source":
        target = source / "nested"
    elif case == "source_in_target":
        target = tmp_path
    elif case == "nonempty_target":
        target.mkdir(parents=True)
        (target / "keep").write_text("x", encoding="utf-8")
    elif case == "escaping_link":
        os.symlink("../../../outside", source / "models" / "cache" / "bad")
    elif case == "absolute_link":
        os.symlink(str(tmp_path), source / "models" / "abs")
    else:
        (source / "settings" / "paths.json").write_text(json.dumps({"root": str(source)}), encoding="utf-8")
    before = _snapshot(source)
    with pytest.raises(jdr.DataRootError) as refused:
        jdr.move_data_root(source_root=source, target_root=target)
    assert _snapshot(source) == before
    if case == "nonempty_target":
        assert sorted(p.name for p in target.iterdir()) == ["keep"]
    elif case not in {"target_in_source", "source_in_target"}:
        assert not target.exists()
    if case == "config_names_old_root":
        assert "settings/paths.json" in str(refused.value) and str(source) not in str(refused.value)


def test_failure_after_copy_keeps_the_partial_directory_and_publishes_no_target(tmp_path: Path) -> None:
    source, target = tmp_path / "live", tmp_path / "vault" / "jarvis-data"
    _full_root(source)

    def fail(stage: str, _partial: Path) -> None:
        if stage == "before_verify":
            raise jdr.DataRootError("injected")

    with pytest.raises(jdr.DataRootError):
        jdr.move_data_root(source_root=source, target_root=target, failure_hook=fail)
    assert not target.exists()
    partials = [p.name for p in target.parent.iterdir()]
    assert len(partials) == 1 and partials[0].startswith(".partial-move-jarvis-data-")


def test_cli_move_and_verify_move(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source, target = tmp_path / "live", tmp_path / "vault" / "jarvis-data"
    _full_root(source)
    assert jdr.main(["move", "--source-root", str(source), "--target-root", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["files"] > 10
    assert jdr.main(["verify-move", "--source-root", str(source), "--target-root", str(target)]) == 0
    assert jdr.main(["verify-move", "--source-root", str(tmp_path / "vault"), "--target-root", str(target)]) == 1
