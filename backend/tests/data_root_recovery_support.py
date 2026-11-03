from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import jarvisos_data_root as jdr


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_data_root(root: Path) -> dict[str, Path]:
    (root / "workspaces/ws/model_implementations/mv").mkdir(parents=True)
    (root / "workspaces/ws/runs/run").mkdir(parents=True)
    (root / "workspaces/ws/bluecad/candidate/attempt").mkdir(parents=True)
    (root / "artifacts/ws").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)

    script = root / "workspaces/ws/model_implementations/mv/calc.py"
    input_file = root / "workspaces/ws/runs/run/input.json"
    output_file = root / "workspaces/ws/runs/run/result.json"
    step = root / "workspaces/ws/bluecad/candidate/attempt/model.step"
    manifest = root / "workspaces/ws/bluecad/candidate/attempt/manifest.json"
    artifact = root / "artifacts/ws/report.json"
    for path, data in (
        (script, b"print(1)\n"),
        (input_file, b"{}\n"),
        (output_file, b'{"ok":true}\n'),
        (step, b"STEP"),
        (manifest, b"{}\n"),
        (artifact, b'{"report":1}\n'),
    ):
        path.write_bytes(data)
    (root / "logs/ignored.log").write_text("ignore", encoding="utf-8")

    database = root / "jarvisos.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE schema_migrations(
                migration_id TEXT PRIMARY KEY,
                name TEXT,
                applied_at TEXT,
                checksum TEXT,
                status TEXT
            );
            CREATE TABLE workspaces(id TEXT PRIMARY KEY);
            CREATE TABLE artifacts(
                id TEXT PRIMARY KEY,
                stored_path TEXT NOT NULL,
                sha256 TEXT
            );
            CREATE TABLE runner_jobs(
                id TEXT PRIMARY KEY,
                script_path TEXT NOT NULL,
                working_dir TEXT NOT NULL,
                input_file TEXT,
                output_dir TEXT NOT NULL,
                command_json TEXT,
                environment_json TEXT
            );
            CREATE TABLE simulation_runs(
                id TEXT PRIMARY KEY,
                input_payload TEXT,
                parameter_payload TEXT,
                output_payload TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?, ?)",
            ("0008_evidence_records", "test", "now", None, "applied"),
        )
        connection.execute("INSERT INTO workspaces VALUES (?)", ("ws",))
        for artifact_id, path in (
            ("a1", artifact),
            ("a2", script),
            ("a3", output_file),
            ("a4", step),
            ("a5", manifest),
        ):
            connection.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?)",
                (artifact_id, str(path), digest(path)),
            )
        run_dir = root / "workspaces/ws/runs/run"
        command = {
            "executable": "python",
            "argv": ["python", str(script), str(input_file), str(run_dir)],
            "shell": False,
        }
        environment = {
            "inherited_environment": False,
            "allowlisted_keys": ["PYTHONIOENCODING"],
        }
        connection.execute(
            "INSERT INTO runner_jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "j1",
                str(script),
                str(run_dir),
                str(input_file),
                str(run_dir),
                json.dumps(command),
                json.dumps(environment),
            ),
        )
        parameter_payload = {
            "geometry": {
                "step_path": str(step),
                "manifest_path": str(manifest),
            },
            "other": 1,
        }
        connection.execute(
            "INSERT INTO simulation_runs VALUES (?, ?, ?, ?)",
            ("run", "{}", json.dumps(parameter_payload), "{}"),
        )
        connection.commit()

    return {
        "database": database,
        "script": script,
        "input": input_file,
        "artifact": artifact,
        "step": step,
        "manifest": manifest,
    }


def rewrite_manifest(
    snapshot: Path, mutate: Callable[[dict[str, object]], None]
) -> None:
    manifest_path = snapshot / jdr.MANIFEST_NAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(data)
    raw = jdr._canonical_json_bytes(data)
    manifest_path.write_bytes(raw)
    marker = {
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "snapshot_id": data["snapshot_id"],
    }
    (snapshot / jdr.COMPLETION_MARKER).write_bytes(jdr._canonical_json_bytes(marker))
