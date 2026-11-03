"""Pinned, visual-only Studio geometry references owned by BLUECAD (spec 194)."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.database import open_sqlite_connection
from app.modules.events.service import utc_now
from app.modules.runner.linked_parameters import require_linked_parameters_usable
from app.modules.runner.safety import RunnerSafetyError, model_implementation_root, run_root

FRAME = "mm_z_up_rotation_RxRyRz_deg"


class ReferenceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


class Transform(BaseModel):
    model_config = ConfigDict(extra="forbid")
    translation_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_deg: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @field_validator("translation_mm", "rotation_deg")
    @classmethod
    def finite(cls, values: tuple[float, float, float]) -> tuple[float, float, float]:
        if any(isinstance(value, bool) or not math.isfinite(value) for value in values):
            raise ValueError("Transform values must be finite numbers.")
        return values


class ReferenceCreate(Transform):
    simulation_run_id: str = Field(min_length=1)


class ReferenceMove(Transform):
    expected_revision: int = Field(ge=1)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate(connection: sqlite3.Connection, workspace_id: str, candidate_id: str) -> None:
    if connection.execute("SELECT 1 FROM bluecad_candidates WHERE id = ? AND workspace_id = ?",
                          (candidate_id, workspace_id)).fetchone() is None:
        raise ReferenceError("bluecad_candidate_not_found", "BLUECAD candidate not found.", 404)


def _generation(connection: sqlite3.Connection, workspace_id: str, run_id: str) -> dict:
    row = connection.execute("""SELECT sr.id, sr.model_version_id, sr.input_payload, sr.status,
        mv.model_spec_id, mv.version_label, sp.source_sha256, sp.contract_sha256,
        sp.contract_json, a.stored_path AS source_path
        FROM simulation_runs sr
        JOIN model_versions mv ON mv.id = sr.model_version_id AND mv.workspace_id = sr.workspace_id
        JOIN studio_published_versions sp ON sp.version_id = mv.id
        JOIN artifacts a ON a.id = mv.implementation_artifact_id AND a.workspace_id = sr.workspace_id
        WHERE sr.id = ? AND sr.workspace_id = ? AND mv.implementation_kind = 'studio_geometry_v1'""",
        (run_id, workspace_id)).fetchone()
    if row is None or row["status"] != "succeeded":
        raise ReferenceError("studio_generation_unavailable", "A successful published Studio geometry generation is required.")
    source_path = Path(row["source_path"])
    if (source_path != model_implementation_root(workspace_id) / row["model_version_id"] / "studio_geometry.py"
            or source_path.is_symlink() or not source_path.is_file()
            or _digest(source_path) != row["source_sha256"]):
        raise ReferenceError("studio_source_invalid", "The pinned Studio source is missing or changed.")
    try:
        contract = json.loads(row["contract_json"])
        input_payload = json.loads(row["input_payload"])
    except (TypeError, ValueError) as exc:
        raise ReferenceError("studio_contract_invalid", "The Studio generation contract is invalid.") from exc
    if (hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            != row["contract_sha256"] or contract.get("category") != "geometry"):
        raise ReferenceError("studio_contract_invalid", "The published Studio contract is invalid.")
    try:
        names = {item["name"]: item["unit"] for item in contract["inputs"]}
    except (KeyError, TypeError) as exc:
        raise ReferenceError("studio_contract_invalid", "The published Studio contract is invalid.") from exc
    if (not isinstance(input_payload, dict) or set(input_payload) != set(names) or any(
        not isinstance(input_payload[name], dict) or input_payload[name].get("unit") != unit
        or not isinstance(input_payload[name].get("value"), (int, float))
        or not math.isfinite(input_payload[name]["value"])
        for name, unit in names.items()
    )):
        raise ReferenceError("studio_inputs_invalid", "Generation inputs do not match the published contract.")
    artifacts: dict[str, str] = {}
    for artifact in connection.execute("""SELECT a.filename, a.stored_path, a.sha256 FROM run_artifacts ra
        JOIN artifacts a ON a.id = ra.artifact_id AND a.workspace_id = ra.workspace_id
        WHERE ra.workspace_id = ? AND ra.simulation_run_id = ? AND a.artifact_type = 'studio_geometry'
        AND a.source_ref = ?""", (workspace_id, run_id, f"simulation_run:{run_id}")):
        name = artifact["filename"]
        if name not in {"model.glb", "model.step", "model.stl"} or name in artifacts:
            continue
        path = Path(artifact["stored_path"])
        if (path != run_root(workspace_id, run_id) / "geometry" / name
                or path.is_symlink() or not path.is_file() or _digest(path) != artifact["sha256"]):
            raise ReferenceError("studio_artifact_invalid", "A generated Studio artifact is missing or changed.")
        artifacts[name] = artifact["sha256"]
    if set(artifacts) != {"model.glb", "model.step", "model.stl"}:
        raise ReferenceError("studio_artifact_missing", "The geometry generation lacks GLB, STEP or STL.")
    return {**dict(row), "input_payload": input_payload, "artifacts": artifacts}


def _present(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    result = dict(row)
    result["translation_mm"] = json.loads(result.pop("translation_mm_json"))
    result["rotation_deg"] = json.loads(result.pop("rotation_deg_json"))
    expected = {"model.glb": result["glb_sha256"], "model.step": result["step_sha256"],
                "model.stl": result["stl_sha256"]}
    try:
        generation = _generation(connection, row["workspace_id"], row["simulation_run_id"])
        if (generation["model_version_id"] != row["model_version_id"]
                or generation["model_spec_id"] != row["model_spec_id"]
                or generation["source_sha256"] != row["source_sha256"]
                or generation["contract_sha256"] != row["contract_sha256"]
                or generation["artifacts"] != expected
                or generation["input_payload"] != json.loads(row["input_payload"])):
            raise ReferenceError("studio_reference_changed", "Pinned generation identity changed.")
        result["valid"] = True
        result["finding"] = None
        result["glb_url"] = (f"/workspaces/{row['workspace_id']}/bluecad/candidates/{row['candidate_id']}"
                             f"/studio-references/{row['id']}/model.glb")
    except ReferenceError as exc:
        result["valid"] = False
        result["finding"] = exc.message
        result["glb_url"] = None
    result["inputs"] = json.loads(result.pop("input_payload"))
    # Deterministic scene identity and placement provenance: the same pin and transform always
    # produce the same scene_id and placement_sha256. The frame is declared, never inferred.
    result["scene_id"] = f"studio-reference:{row['id']}"
    result["visual_only"] = True
    result["provenance"] = {
        "reference_id": row["id"], "candidate_id": row["candidate_id"],
        "simulation_run_id": row["simulation_run_id"], "model_version_id": row["model_version_id"],
        "source_sha256": row["source_sha256"], "contract_sha256": row["contract_sha256"],
        "glb_sha256": row["glb_sha256"], "frame": FRAME,
        "translation_mm": result["translation_mm"], "rotation_deg": result["rotation_deg"],
    }
    result["placement_sha256"] = hashlib.sha256(
        json.dumps(result["provenance"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result["superseded"] = connection.execute("""SELECT 1 FROM studio_published_versions newer
        JOIN studio_published_versions pinned ON pinned.version_id = ?
        WHERE newer.model_spec_id = pinned.model_spec_id AND newer.rowid > pinned.rowid LIMIT 1""",
        (row["model_version_id"],)).fetchone() is not None
    result["stale"] = result["superseded"]
    try:
        require_linked_parameters_usable(connection, row["workspace_id"], row["input_payload"], studio_pint=True)
    except RunnerSafetyError:
        result["stale"] = True
    return result


def reference_glb(workspace_id: str, candidate_id: str, ref_id: str) -> bytes:
    """The pinned GLB bytes, served only after the whole pin is re-verified (spec 194: verify when serving)."""
    with open_sqlite_connection() as connection:
        _candidate(connection, workspace_id, candidate_id)
        row = connection.execute("""SELECT * FROM bluecad_studio_references
            WHERE id = ? AND workspace_id = ? AND candidate_id = ?""",
            (ref_id, workspace_id, candidate_id)).fetchone()
        if row is None or not _present(connection, row)["valid"]:
            raise ReferenceError("studio_reference_invalid", "Studio reference is missing or no longer valid.", 404)
    # Hash the bytes actually returned, so a swap after validation cannot be served.
    data = (run_root(workspace_id, row["simulation_run_id"]) / "geometry" / "model.glb").read_bytes()
    if hashlib.sha256(data).hexdigest() != row["glb_sha256"]:
        raise ReferenceError("studio_reference_invalid", "Studio reference is missing or no longer valid.", 404)
    return data


def list_references(workspace_id: str, candidate_id: str) -> list[dict]:
    with open_sqlite_connection() as connection:
        _candidate(connection, workspace_id, candidate_id)
        rows = connection.execute("""SELECT * FROM bluecad_studio_references
            WHERE workspace_id = ? AND candidate_id = ? ORDER BY created_at, id""",
            (workspace_id, candidate_id)).fetchall()
        return [_present(connection, row) for row in rows]


def add_reference(workspace_id: str, candidate_id: str, data: ReferenceCreate) -> dict:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _candidate(connection, workspace_id, candidate_id)
        generation = _generation(connection, workspace_id, data.simulation_run_id)
        now, ref_id = utc_now(), str(uuid4())
        try:
            connection.execute("""INSERT INTO bluecad_studio_references
                (id, workspace_id, candidate_id, simulation_run_id, model_spec_id, model_version_id,
                version_label, source_sha256, contract_sha256, input_payload, glb_sha256, step_sha256,
                stl_sha256, translation_mm_json, rotation_deg_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ref_id, workspace_id, candidate_id, data.simulation_run_id, generation["model_spec_id"],
                 generation["model_version_id"], generation["version_label"], generation["source_sha256"],
                 generation["contract_sha256"], json.dumps(generation["input_payload"], sort_keys=True),
                 generation["artifacts"]["model.glb"], generation["artifacts"]["model.step"],
                 generation["artifacts"]["model.stl"], json.dumps(data.translation_mm),
                 json.dumps(data.rotation_deg), now, now))
        except sqlite3.IntegrityError as exc:
            raise ReferenceError("studio_reference_duplicate", "This generation is already referenced.") from exc
        row = connection.execute("SELECT * FROM bluecad_studio_references WHERE id = ?", (ref_id,)).fetchone()
        result = _present(connection, row)
        connection.commit()
        return result


def move_reference(workspace_id: str, candidate_id: str, ref_id: str, data: ReferenceMove) -> dict:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _candidate(connection, workspace_id, candidate_id)
        row = connection.execute("""SELECT * FROM bluecad_studio_references
            WHERE id = ? AND workspace_id = ? AND candidate_id = ?""",
            (ref_id, workspace_id, candidate_id)).fetchone()
        if row is None:
            raise ReferenceError("studio_reference_not_found", "Studio reference not found.", 404)
        if row["revision"] != data.expected_revision:
            raise ReferenceError("studio_reference_stale_edit", "Reference changed; refresh before moving.")
        if not _present(connection, row)["valid"]:
            raise ReferenceError("studio_reference_invalid", "Invalid reference cannot be moved.")
        connection.execute("""UPDATE bluecad_studio_references SET translation_mm_json = ?,
            rotation_deg_json = ?, updated_at = ?, revision = revision + 1 WHERE id = ?""",
            (json.dumps(data.translation_mm), json.dumps(data.rotation_deg), utc_now(), ref_id))
        result = _present(connection, connection.execute(
            "SELECT * FROM bluecad_studio_references WHERE id = ?", (ref_id,)).fetchone())
        connection.commit()
        return result


def remove_reference(workspace_id: str, candidate_id: str, ref_id: str, expected_revision: int) -> None:
    with open_sqlite_connection() as connection:
        _candidate(connection, workspace_id, candidate_id)
        count = connection.execute("""DELETE FROM bluecad_studio_references
            WHERE id = ? AND workspace_id = ? AND candidate_id = ? AND revision = ?""",
            (ref_id, workspace_id, candidate_id, expected_revision)).rowcount
        if not count:
            raise ReferenceError("studio_reference_stale_edit", "Reference not found or changed; refresh first.")
        connection.commit()
