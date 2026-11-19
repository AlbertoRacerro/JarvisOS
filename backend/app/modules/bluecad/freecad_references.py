"""Content-addressed FreeCAD FCStd uploads and visual-only BLUECAD references."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.modules.bluecad.export import export_imported_step
from app.modules.bluecad.freecad_bridge import _configured_runtime
from app.modules.bluecad.print_handoff import _to_windows_path
from app.modules.events.service import log_event, utc_now

MAX_FILE_BYTES = 64 * 1024 * 1024
OBJECT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
FREECAD_EXPORT_SCRIPT = Path(__file__).with_name("freecad_export.py")
FRAME = "mm_z_up_rotation_RxRyRz_deg"
EXPORT_TIMEOUT_SECONDS = 120
ARTIFACT_NAMES = ("model.step", "model.stl", "model.glb", "model.3mf")


class FreecadReferenceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class FreecadReferenceAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_sha256: str
    object_name: str
    translation_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_deg: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @field_validator("translation_mm", "rotation_deg")
    @classmethod
    def finite(cls, values: tuple[float, float, float]) -> tuple[float, float, float]:
        if any(isinstance(value, bool) or not math.isfinite(value) for value in values):
            raise ValueError("Transform values must be finite numbers.")
        return values


class FreecadReferenceMove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int
    translation_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_deg: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @field_validator("translation_mm", "rotation_deg")
    @classmethod
    def finite(cls, values: tuple[float, float, float]) -> tuple[float, float, float]:
        if any(isinstance(value, bool) or not math.isfinite(value) for value in values):
            raise ValueError("Transform values must be finite numbers.")
        return values


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_file_name(file_name: str) -> str:
    if (
        not file_name
        or len(file_name) > 120
        or "/" in file_name
        or "\\" in file_name
        or file_name != Path(file_name).name
        or not file_name.endswith(".FCStd")
    ):
        raise FreecadReferenceError(
            "FREECAD_FILE_INVALID",
            "Il nome deve essere un nome base che termina con .FCStd.",
            400,
        )
    return file_name


def _valid_zip(body: bytes) -> bool:
    try:
        with zipfile.ZipFile(BytesIO(body), "r") as archive:
            names = {name.replace("\\", "/").lstrip("/") for name in archive.namelist()}
            return "Document.xml" in names or any(name.endswith("/Document.xml") for name in names)
    except (zipfile.BadZipFile, OSError):
        return False


def _windows_path(executable: Path, path: Path) -> str:
    if executable.suffix.lower() == ".exe" and str(path).lower().startswith("/mnt/"):
        try:
            return _to_windows_path(path)
        except Exception as exc:
            raise FreecadReferenceError("FREECAD_BRIDGE_WSLPATH_FAILED", "Il percorso FreeCAD non è stato convertito.", 502) from exc
    return str(path)


def _script_path(executable: Path) -> str:
    if executable.suffix.lower() == ".exe" and str(FREECAD_EXPORT_SCRIPT).lower().startswith("/mnt/"):
        try:
            return _to_windows_path(FREECAD_EXPORT_SCRIPT)
        except Exception as exc:
            raise FreecadReferenceError("FREECAD_BRIDGE_WSLPATH_FAILED", "Il percorso dello script non è stato convertito.", 502) from exc
    return str(FREECAD_EXPORT_SCRIPT)


def _run_freecad(mode: str, fcstd_path: Path, out_dir: Path, object_name: str | None = None) -> list[dict] | None:
    executable, _, reason = _configured_runtime()
    if reason or executable is None:
        raise FreecadReferenceError(reason or "FREECAD_BRIDGE_UNAVAILABLE", "Il collegamento a FreeCAD non è configurato.", 409)
    argv = [
        str(executable),
        _script_path(executable),
        mode,
        _windows_path(executable, fcstd_path),
        _windows_path(executable, out_dir),
    ]
    if object_name is not None:
        if OBJECT_NAME_RE.fullmatch(object_name) is None:
            raise FreecadReferenceError("FREECAD_OBJECT_NAME_INVALID", "Nome oggetto FreeCAD non valido.", 400)
        argv.append(object_name)
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout, _ = process.communicate(timeout=EXPORT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise FreecadReferenceError("FREECAD_BRIDGE_TIMEOUT", "FreeCAD non ha completato l'esportazione in tempo.", 504) from exc
    except OSError as exc:
        raise FreecadReferenceError("FREECAD_BRIDGE_UNAVAILABLE", "Il collegamento a FreeCAD non è partito.") from exc
    if process.returncode != 0:
        raise FreecadReferenceError("FREECAD_BRIDGE_PROTOCOL_ERROR", "FreeCAD ha rifiutato l'esportazione.", 502)
    if mode == "list":
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise FreecadReferenceError("FREECAD_BRIDGE_PROTOCOL_ERROR", "Elenco oggetti non valido.", 502) from exc
        if not isinstance(value, list):
            raise FreecadReferenceError("FREECAD_BRIDGE_PROTOCOL_ERROR", "Elenco oggetti non valido.", 502)
        return value
    return None


def upload_freecad_file(workspace_id: str, file_name: str, body: bytes) -> dict:
    validate_file_name(file_name)
    if len(body) > MAX_FILE_BYTES:
        raise FreecadReferenceError("FREECAD_FILE_TOO_LARGE", "Il file supera il limite di 64 MiB.", 413)
    if not _valid_zip(body):
        raise FreecadReferenceError("FREECAD_FILE_INVALID", "Il file non è un FCStd ZIP valido.", 400)
    file_sha256 = _sha256(body)
    source = build_paths().bluecad_freecad_source_file(workspace_id, file_sha256)
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        temporary = source.with_name(source.name + ".tmp")
        temporary.write_bytes(body)
        temporary.replace(source)
    else:
        if _digest_file(source) != file_sha256:
            raise FreecadReferenceError("FREECAD_FILE_INVALID", "La copia del file non è più valida.", 409)
    with tempfile.TemporaryDirectory(dir=str(source.parent)) as out_dir:
        objects = _run_freecad("list", source, Path(out_dir))
    upload_objects = objects or []
    labels = {
        str(item.get("name")): str(item.get("label") or item.get("name") or "")
        for item in upload_objects
        if isinstance(item, dict)
    }
    # The latest upload's name and labels survive a restart; the pin itself is the digest.
    manifest = source.with_name("upload.json")
    temporary_manifest = manifest.with_name(manifest.name + ".tmp")
    temporary_manifest.write_text(json.dumps({"file_name": file_name, "labels": labels}, ensure_ascii=False), encoding="utf-8")
    temporary_manifest.replace(manifest)
    return {"file_sha256": file_sha256, "file_name": file_name, "objects": upload_objects}


def _upload_names(source: Path, object_name: str) -> tuple[str, str]:
    try:
        manifest = json.loads(source.with_name("upload.json").read_text(encoding="utf-8"))
        file_name = validate_file_name(str(manifest["file_name"]))
        label = str(manifest.get("labels", {}).get(object_name) or object_name)
        return file_name, label
    except (OSError, ValueError, KeyError, TypeError, AttributeError, FreecadReferenceError):
        return "source.FCStd", object_name


def _candidate(connection: sqlite3.Connection, workspace_id: str, candidate_id: str) -> None:
    if connection.execute(
        "SELECT 1 FROM bluecad_candidates WHERE id = ? AND workspace_id = ?",
        (candidate_id, workspace_id),
    ).fetchone() is None:
        raise FreecadReferenceError("bluecad_candidate_not_found", "Candidato BLUECAD non trovato.", 404)


def _export_dir(workspace_id: str, file_sha256: str, object_name: str) -> Path:
    object_digest = _sha256(object_name.encode("utf-8"))
    return build_paths().bluecad_freecad_export_dir(workspace_id, file_sha256, object_digest)


def _present(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    result = dict(row)
    result["translation_mm"] = json.loads(result.pop("translation_mm_json"))
    result["rotation_deg"] = json.loads(result.pop("rotation_deg_json"))
    source = build_paths().bluecad_freecad_source_file(row["workspace_id"], row["fcstd_sha256"])
    out_dir = _export_dir(row["workspace_id"], row["fcstd_sha256"], row["object_name"])
    expected = {
        "source.FCStd": row["fcstd_sha256"],
        "model.step": row["step_sha256"],
        "model.stl": row["stl_sha256"],
        "model.3mf": row["three_mf_sha256"],
        "model.glb": row["glb_sha256"],
    }
    finding: str | None = None
    valid = source.is_file() and not source.is_symlink()
    if not valid:
        finding = "source.FCStd è stato modificato o manca."
    if valid:
        try:
            for name, digest in expected.items():
                path = source if name == "source.FCStd" else out_dir / name
                if path.is_symlink() or not path.is_file() or _digest_file(path) != digest:
                    valid = False
                    finding = f"{name} è stato modificato o manca."
                    break
        except OSError:
            valid = False
            finding = "Un artefatto è stato modificato o manca."
    result["valid"] = valid
    result["finding"] = None if valid else (finding or "Il riferimento FreeCAD è stato modificato o manca.")
    base = (
        f"/workspaces/{row['workspace_id']}/bluecad/candidates/{row['candidate_id']}"
        f"/freecad-references/{row['id']}"
    )
    result["glb_url"] = f"{base}/model.glb" if valid else None
    result["step_url"] = f"{base}/model.step" if valid else None
    result["stl_url"] = f"{base}/model.stl" if valid else None
    result["three_mf_url"] = f"{base}/model.3mf" if valid else None
    result["scene_id"] = f"freecad-reference:{row['id']}"
    result["visual_only"] = True
    result["frame"] = FRAME
    return result


def list_freecad_references(workspace_id: str, candidate_id: str) -> list[dict]:
    with open_sqlite_connection() as connection:
        _candidate(connection, workspace_id, candidate_id)
        rows = connection.execute(
            "SELECT * FROM bluecad_freecad_references WHERE workspace_id = ? AND candidate_id = ? ORDER BY created_at, id",
            (workspace_id, candidate_id),
        ).fetchall()
        return [_present(connection, row) for row in rows]


def _existing_export(out_dir: Path) -> dict[str, str] | None:
    """Reuse a complete earlier export of the same pinned file and object, so a second add never rewrites it."""
    digests: dict[str, str] = {}
    for name in ARTIFACT_NAMES:
        path = out_dir / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            return None
        digests[name] = _digest_file(path)
    return digests


def _export_object(source: Path, object_name: str, out_dir: Path) -> dict[str, str]:
    with tempfile.TemporaryDirectory(dir=str(source.parent)) as temp_dir:
        temp = Path(temp_dir)
        _run_freecad("export", source, temp, object_name)
        step = temp / "model.step"
        if not step.is_file() or step.stat().st_size <= 0:
            raise FreecadReferenceError("FREECAD_EXPORT_FAILED", "FreeCAD non ha creato lo STEP.", 502)
        try:
            export_imported_step(step, temp)
        except Exception as exc:
            code = getattr(exc, "code", "FREECAD_EXPORT_FAILED")
            raise FreecadReferenceError(code if isinstance(code, str) else "FREECAD_EXPORT_FAILED", "Il kernel non ha esportato la parte.", 502) from exc
        out_dir.mkdir(parents=True, exist_ok=True)
        digests: dict[str, str] = {}
        for name in ARTIFACT_NAMES:
            path = temp / name
            if not path.is_file():
                raise FreecadReferenceError("FREECAD_EXPORT_FAILED", "Manca un artefatto esportato.", 502)
            digests[name] = _digest_file(path)
            path.replace(out_dir / name)
        return digests


def add_freecad_reference(workspace_id: str, candidate_id: str, data: FreecadReferenceAdd) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", data.file_sha256):
        raise FreecadReferenceError("FREECAD_FILE_INVALID", "Digest del file non valido.", 400)
    if OBJECT_NAME_RE.fullmatch(data.object_name) is None:
        raise FreecadReferenceError("FREECAD_OBJECT_NAME_INVALID", "Nome oggetto FreeCAD non valido.", 400)
    with open_sqlite_connection() as connection:
        _candidate(connection, workspace_id, candidate_id)
    source = build_paths().bluecad_freecad_source_file(workspace_id, data.file_sha256)
    if not source.is_file() or source.is_symlink():
        raise FreecadReferenceError("FREECAD_FILE_INVALID", "Il file FCStd non è presente.", 404)
    if _digest_file(source) != data.file_sha256:
        raise FreecadReferenceError("FREECAD_FILE_INVALID", "Il file FCStd è stato modificato.", 409)
    with open_sqlite_connection() as connection:
        existing = connection.execute(
            "SELECT 1 FROM bluecad_freecad_references WHERE candidate_id = ? AND fcstd_sha256 = ? AND object_name = ?",
            (candidate_id, data.file_sha256, data.object_name),
        ).fetchone()
        if existing is not None:
            raise FreecadReferenceError("freecad_reference_duplicate", "Questa parte FreeCAD è già aggiunta alla vista.")

    out_dir = _export_dir(workspace_id, data.file_sha256, data.object_name)
    digests = _existing_export(out_dir)
    if digests is None:
        digests = _export_object(source, data.object_name, out_dir)

    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _candidate(connection, workspace_id, candidate_id)
        document_name, object_label = _upload_names(source, data.object_name)
        now = utc_now()
        ref_id = str(uuid4())
        try:
            connection.execute(
                """INSERT INTO bluecad_freecad_references
                (id, workspace_id, candidate_id, document_file_name, object_name, object_label,
                 fcstd_sha256, step_sha256, stl_sha256, three_mf_sha256, glb_sha256,
                 translation_mm_json, rotation_deg_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ref_id, workspace_id, candidate_id, document_name, data.object_name, object_label,
                    data.file_sha256, digests["model.step"], digests["model.stl"], digests["model.3mf"],
                    digests["model.glb"], json.dumps(data.translation_mm), json.dumps(data.rotation_deg),
                    now, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise FreecadReferenceError("freecad_reference_duplicate", "Questa parte FreeCAD è già aggiunta alla vista.") from exc
        row = connection.execute("SELECT * FROM bluecad_freecad_references WHERE id = ?", (ref_id,)).fetchone()
        result = _present(connection, row)
        log_event(
            connection,
            event_type="FreecadReferenceAdded",
            actor="local-user",
            target_type="FreecadReference",
            target_id=ref_id,
            workspace_id=workspace_id,
            payload={
                "outcome": "added",
                "file_name": document_name,
                "object_name": data.object_name,
                "fcstd_sha256": data.file_sha256,
                "step_sha256": digests["model.step"],
                "stl_sha256": digests["model.stl"],
                "three_mf_sha256": digests["model.3mf"],
                "glb_sha256": digests["model.glb"],
            },
        )
        connection.commit()
        return result


def move_freecad_reference(workspace_id: str, candidate_id: str, ref_id: str, data: FreecadReferenceMove) -> dict:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _candidate(connection, workspace_id, candidate_id)
        row = connection.execute(
            "SELECT * FROM bluecad_freecad_references WHERE id = ? AND workspace_id = ? AND candidate_id = ?",
            (ref_id, workspace_id, candidate_id),
        ).fetchone()
        if row is None:
            raise FreecadReferenceError("freecad_reference_not_found", "Riferimento FreeCAD non trovato.", 404)
        if row["revision"] != data.expected_revision:
            raise FreecadReferenceError("freecad_reference_stale_edit", "Il riferimento è cambiato; aggiorna la vista.")
        present = _present(connection, row)
        if not present["valid"]:
            raise FreecadReferenceError("freecad_reference_invalid", present["finding"] or "Il riferimento FreeCAD non è valido.")
        connection.execute(
            "UPDATE bluecad_freecad_references SET translation_mm_json = ?, rotation_deg_json = ?, updated_at = ?, revision = revision + 1 WHERE id = ?",
            (json.dumps(data.translation_mm), json.dumps(data.rotation_deg), utc_now(), ref_id),
        )
        result = _present(connection, connection.execute("SELECT * FROM bluecad_freecad_references WHERE id = ?", (ref_id,)).fetchone())
        connection.commit()
        return result


def remove_freecad_reference(workspace_id: str, candidate_id: str, ref_id: str, expected_revision: int) -> None:
    with open_sqlite_connection() as connection:
        _candidate(connection, workspace_id, candidate_id)
        row = connection.execute(
            "SELECT * FROM bluecad_freecad_references WHERE id = ? AND workspace_id = ? AND candidate_id = ?",
            (ref_id, workspace_id, candidate_id),
        ).fetchone()
        if row is None or row["revision"] != expected_revision:
            raise FreecadReferenceError("freecad_reference_stale_edit", "Riferimento non trovato o già modificato.")
        count = connection.execute(
            "DELETE FROM bluecad_freecad_references WHERE id = ? AND workspace_id = ? AND candidate_id = ? AND revision = ?",
            (ref_id, workspace_id, candidate_id, expected_revision),
        ).rowcount
        if count:
            log_event(
                connection,
                event_type="FreecadReferenceRemoved",
                actor="local-user",
                target_type="FreecadReference",
                target_id=ref_id,
                workspace_id=workspace_id,
                payload={
                    "outcome": "removed",
                    "file_name": row["document_file_name"],
                    "object_name": row["object_name"],
                    "fcstd_sha256": row["fcstd_sha256"],
                    "step_sha256": row["step_sha256"],
                    "stl_sha256": row["stl_sha256"],
                    "three_mf_sha256": row["three_mf_sha256"],
                    "glb_sha256": row["glb_sha256"],
                },
            )
            connection.commit()


def reference_artifact(workspace_id: str, candidate_id: str, ref_id: str, artifact_name: str) -> bytes:
    if artifact_name not in ("model.glb", "model.step", "model.stl", "model.3mf"):
        raise FreecadReferenceError("freecad_reference_not_found", "Artefatto FreeCAD non trovato.", 404)
    with open_sqlite_connection() as connection:
        _candidate(connection, workspace_id, candidate_id)
        row = connection.execute(
            "SELECT * FROM bluecad_freecad_references WHERE id = ? AND workspace_id = ? AND candidate_id = ?",
            (ref_id, workspace_id, candidate_id),
        ).fetchone()
        if row is None or not _present(connection, row)["valid"]:
            raise FreecadReferenceError("freecad_reference_invalid", "Riferimento FreeCAD non valido.", 404)
    out_dir = _export_dir(workspace_id, row["fcstd_sha256"], row["object_name"])
    path = out_dir / artifact_name
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FreecadReferenceError("freecad_reference_invalid", "Artefatto FreeCAD non disponibile.", 404) from exc
    digest_column = {
        "model.glb": "glb_sha256",
        "model.step": "step_sha256",
        "model.stl": "stl_sha256",
        "model.3mf": "three_mf_sha256",
    }[artifact_name]
    if _sha256(data) != row[digest_column]:
        raise FreecadReferenceError("freecad_reference_invalid", "Artefatto FreeCAD modificato.", 404)
    return data
