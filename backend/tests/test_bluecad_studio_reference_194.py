"""Pinned Studio generation references remain visual and fail closed on changed artifacts."""

import hashlib
import inspect
import json
import math
import re
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.modules.bluecad.export import build_artifacts
from app.modules.bluecad.studio_references import (
    ReferenceCreate,
    ReferenceError,
    ReferenceMove,
    add_reference,
    list_references,
    move_reference,
)
from app.modules.bluecad.template import TubeTemplateCreate, template_geometry_spec
from app.modules.bluecad.validate import validate_artifacts
from app.modules.runner.safety import model_implementation_root, run_root


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seed(workspace: str = "main") -> tuple[str, str, dict[str, Path]]:
    initialize_storage(seed_default=True)
    candidate_id, model_id, version_id, run_id, source_id = [str(uuid4()) for _ in range(5)]
    contract = {"category": "geometry", "inputs": [{"name": "radius", "unit": "mm"}]}
    contract_json = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    source = b"def main(inputs): return None\n"
    source_path = model_implementation_root(workspace) / version_id / "studio_geometry.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source)
    paths = {}
    now = "2026-10-15T00:00:00Z"
    with open_sqlite_connection() as connection:
        connection.execute("""INSERT INTO bluecad_candidates
            (id,workspace_id,brief_text,brief_digest,status,origin,loop_config_json,created_at,updated_at)
            VALUES (?,?, 'Test typed assembly', 'digest', 'valid', 'template', '{}', ?, ?)""",
            (candidate_id, workspace, now, now))
        connection.execute("""INSERT INTO model_specs
            (id,workspace_id,title,engineering_question,created_at,updated_at)
            VALUES (?,?,'Baffle','How does it fit?',?,?)""", (model_id, workspace, now, now))
        connection.execute("""INSERT INTO artifacts
            (id,workspace_id,filename,stored_path,artifact_type,sha256,created_at)
            VALUES (?,?,?,?,?,?,?)""", (source_id, workspace, source_path.name, str(source_path),
                                        "studio_source", _sha(source), now))
        connection.execute("""INSERT INTO model_versions
            (id,workspace_id,model_spec_id,version_label,implementation_artifact_id,implementation_kind,status,created_at)
            VALUES (?,?,?,?,?,'studio_geometry_v1','published',?)""",
            (version_id, workspace, model_id, "v1", source_id, now))
        connection.execute("""INSERT INTO studio_published_versions
            (version_id,model_spec_id,contract_json,source_sha256,contract_sha256,created_at)
            VALUES (?,?,?,?,?,?)""", (version_id, model_id, contract_json, _sha(source),
                                       _sha(contract_json.encode()), now))
        connection.execute("""INSERT INTO simulation_runs
            (id,workspace_id,model_version_id,status,input_payload,created_at)
            VALUES (?,?,?,'succeeded',?,?)""", (run_id, workspace, version_id,
                                                '{"radius":{"value":18,"unit":"mm"}}', now))
        for name in ("model.glb", "model.step", "model.stl"):
            path = run_root(workspace, run_id) / "geometry" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            paths[name] = path
            artifact_id = str(uuid4())
            connection.execute("""INSERT INTO artifacts
                (id,workspace_id,filename,stored_path,artifact_type,sha256,source_ref,created_at)
                VALUES (?,?,?,?,'studio_geometry',?,?,?)""",
                (artifact_id, workspace, name, str(path), _sha(path.read_bytes()), f"simulation_run:{run_id}", now))
            connection.execute("""INSERT INTO run_artifacts
                (id,workspace_id,simulation_run_id,artifact_id,role,created_at)
                VALUES (?,?,?,?,'studio_geometry',?)""", (str(uuid4()), workspace, run_id, artifact_id, now))
        connection.commit()
    return candidate_id, run_id, paths


def test_pinned_geometry_reference_and_integrity(tmp_path: Path) -> None:
    candidate_id, run_id, paths = _seed()
    created = add_reference("main", candidate_id, ReferenceCreate(simulation_run_id=run_id))
    assert created["valid"] and created["glb_url"].endswith(f"/studio-references/{created['id']}/model.glb")
    assert created["inputs"] == {"radius": {"value": 18, "unit": "mm"}}
    assert created["translation_mm"] == [0, 0, 0]
    with pytest.raises(ReferenceError, match="already referenced"):
        add_reference("main", candidate_id, ReferenceCreate(simulation_run_id=run_id))
    with pytest.raises(ReferenceError):
        add_reference("foreign-workspace", candidate_id, ReferenceCreate(simulation_run_id=run_id))
    moved = move_reference("main", candidate_id, created["id"], ReferenceMove(
        expected_revision=1, translation_mm=(5, -2, 10), rotation_deg=(0, 0, 45)))
    assert moved["translation_mm"] == [5, -2, 10] and moved["revision"] == 2
    with pytest.raises(ReferenceError, match="refresh"):
        move_reference("main", candidate_id, created["id"], ReferenceMove(expected_revision=1))
    paths["model.glb"].write_bytes(b"tampered")
    found = list_references("main", candidate_id)[0]
    assert not found["valid"] and found["glb_url"] is None and "changed" in found["finding"]
    with pytest.raises(ReferenceError, match="Invalid reference"):
        move_reference("main", candidate_id, created["id"], ReferenceMove(expected_revision=2))


def test_reference_endpoint_rejects_foreign_and_tampered_generation() -> None:
    candidate_id, run_id, paths = _seed()
    from app.main import create_app

    with TestClient(create_app()) as client:
        base = f"/workspaces/main/bluecad/candidates/{candidate_id}/studio-references"
        assert client.post(base, json={"simulation_run_id": run_id, "translation_mm": [1, 2, 3]}).status_code == 201
        assert client.post(base, json={"simulation_run_id": run_id, "translation_mm": [1, 2]}).status_code == 422
        paths["model.step"].write_bytes(b"changed")
        reference = client.get(base).json()[0]
        assert reference["valid"] is False and reference["glb_url"] is None


def test_served_reference_glb_reverifies_the_whole_pin() -> None:
    candidate_id, run_id, paths = _seed()
    from app.main import create_app

    with TestClient(create_app()) as client:
        base = f"/workspaces/main/bluecad/candidates/{candidate_id}/studio-references"
        url = client.post(base, json={"simulation_run_id": run_id}).json()["glb_url"]
        served = client.get(url)
        assert served.status_code == 200 and served.content == b"model.glb"
        assert served.headers["content-type"] == "model/gltf-binary"
        # Changed generation inputs after the URL was issued: the pin no longer holds.
        with open_sqlite_connection() as connection:
            connection.execute("UPDATE simulation_runs SET input_payload=? WHERE id=?",
                               ('{"radius":{"value":19,"unit":"mm"}}', run_id))
            connection.commit()
        assert client.get(url).status_code == 404
        with open_sqlite_connection() as connection:
            connection.execute("UPDATE simulation_runs SET input_payload=? WHERE id=?",
                               ('{"radius":{"value":18,"unit":"mm"}}', run_id))
            connection.commit()
        assert client.get(url).status_code == 200
        # Replaced bytes re-registered under the artifact row: still refused under the pin.
        paths["model.glb"].write_bytes(b"replaced")
        with open_sqlite_connection() as connection:
            connection.execute("UPDATE artifacts SET sha256=? WHERE stored_path=?",
                               (hashlib.sha256(b"replaced").hexdigest(), str(paths["model.glb"])))
            connection.commit()
        assert client.get(url).status_code == 404
        assert client.get(f"/workspaces/other-ws/bluecad/candidates/{candidate_id}/studio-references/"
                          f"{url.split('/')[-2]}/model.glb").status_code == 404


def _second_workspace() -> str:
    workspace_id = "other-ws"
    with open_sqlite_connection() as connection:
        connection.execute("""INSERT OR IGNORE INTO workspaces (id,name,slug,status,created_at,updated_at)
            VALUES (?, 'Other', 'other-ws', 'active', '2026-10-15T00:00:00Z', '2026-10-15T00:00:00Z')""",
                           (workspace_id,))
        connection.commit()
    return workspace_id


def test_foreign_workspace_generation_is_refused() -> None:
    _, run_id, _ = _seed()
    other = _second_workspace()
    foreign_candidate = str(uuid4())
    with open_sqlite_connection() as connection:
        connection.execute("""INSERT INTO bluecad_candidates
            (id,workspace_id,brief_text,brief_digest,status,origin,loop_config_json,created_at,updated_at)
            VALUES (?,?, 'Foreign', 'digest', 'valid', 'template', '{}', 'x', 'x')""", (foreign_candidate, other))
        connection.commit()
    with pytest.raises(ReferenceError) as caught:
        add_reference(other, foreign_candidate, ReferenceCreate(simulation_run_id=run_id))
    assert caught.value.code == "studio_generation_unavailable"
    with pytest.raises(ReferenceError) as caught:
        add_reference("main", foreign_candidate, ReferenceCreate(simulation_run_id=run_id))
    assert caught.value.code == "bluecad_candidate_not_found"


def test_unknown_unsucceeded_and_missing_artifacts_are_refused() -> None:
    candidate_id, run_id, paths = _seed()
    with pytest.raises(ReferenceError) as caught:
        add_reference("main", candidate_id, ReferenceCreate(simulation_run_id=str(uuid4())))
    assert caught.value.code == "studio_generation_unavailable"
    paths["model.stl"].unlink()
    with pytest.raises(ReferenceError) as caught:
        add_reference("main", candidate_id, ReferenceCreate(simulation_run_id=run_id))
    assert caught.value.code == "studio_artifact_invalid"
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE simulation_runs SET status = 'failed' WHERE id = ?", (run_id,))
        connection.commit()
    with pytest.raises(ReferenceError) as caught:
        add_reference("main", candidate_id, ReferenceCreate(simulation_run_id=run_id))
    assert caught.value.code == "studio_generation_unavailable"


def test_tampered_source_and_changed_inputs_fail_closed() -> None:
    candidate_id, run_id, _ = _seed()
    created = add_reference("main", candidate_id, ReferenceCreate(simulation_run_id=run_id))
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE simulation_runs SET input_payload = ? WHERE id = ?",
                           ('{"radius":{"value":19,"unit":"mm"}}', run_id))
        connection.commit()
    found = list_references("main", candidate_id)[0]
    assert found["id"] == created["id"] and not found["valid"] and found["glb_url"] is None
    assert "changed" in found["finding"]
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE simulation_runs SET input_payload = ? WHERE id = ?",
                           ('{"radius":{"value":18,"unit":"cm"}}', run_id))
        connection.commit()
    assert not list_references("main", candidate_id)[0]["valid"]


def test_newer_version_marks_superseded_without_changing_pin() -> None:
    candidate_id, run_id, _ = _seed()
    created = add_reference("main", candidate_id, ReferenceCreate(simulation_run_id=run_id))
    assert created["superseded"] is False
    with open_sqlite_connection() as connection:
        spec_id, contract_json = connection.execute(
            "SELECT model_spec_id, contract_json FROM studio_published_versions").fetchone()
        newer_id = str(uuid4())
        connection.execute("""INSERT INTO model_versions
            (id,workspace_id,model_spec_id,version_label,implementation_kind,status,created_at)
            VALUES (?,?,?,'v2','studio_geometry_v1','published','2026-10-16T00:00:00Z')""",
                           (newer_id, "main", spec_id))
        connection.execute("""INSERT INTO studio_published_versions
            (version_id,model_spec_id,contract_json,source_sha256,contract_sha256,created_at)
            VALUES (?,?,?,?,?,'2026-10-16T00:00:00Z')""", (newer_id, spec_id, contract_json, "n" * 64, "m" * 64))
        connection.commit()
    found = list_references("main", candidate_id)[0]
    assert found["superseded"] and found["stale"] and found["valid"]
    for key in ("model_version_id", "version_label", "source_sha256", "contract_sha256", "glb_sha256",
                "simulation_run_id", "inputs", "placement_sha256", "scene_id"):
        assert found[key] == created[key]


def test_transform_units_and_repeatable_scene_identity() -> None:
    candidate_id, run_id, _ = _seed()
    created = add_reference("main", candidate_id, ReferenceCreate(
        simulation_run_id=run_id, translation_mm=(12.5, 0, -3), rotation_deg=(0, 90, 0)))
    first, second = list_references("main", candidate_id)[0], list_references("main", candidate_id)[0]
    assert first == second and first["scene_id"] == f"studio-reference:{created['id']}"
    assert first["provenance"]["frame"].startswith("mm_") and first["visual_only"] is True
    assert first["provenance"]["translation_mm"] == [12.5, 0, -3]
    assert first["provenance"]["rotation_deg"] == [0, 90, 0]
    moved = move_reference("main", candidate_id, created["id"], ReferenceMove(
        expected_revision=1, translation_mm=(12.5, 0, -3), rotation_deg=(0, 90, 0)))
    assert moved["placement_sha256"] == first["placement_sha256"]
    other = move_reference("main", candidate_id, created["id"], ReferenceMove(
        expected_revision=2, translation_mm=(13, 0, -3), rotation_deg=(0, 90, 0)))
    assert other["placement_sha256"] != first["placement_sha256"]
    for bad in (math.nan, math.inf):
        with pytest.raises(ValidationError):
            ReferenceMove(expected_revision=1, translation_mm=(bad, 0, 0))
        with pytest.raises(ValidationError):
            ReferenceCreate(simulation_run_id=run_id, rotation_deg=(0, bad, 0))
    with pytest.raises(ValidationError):
        ReferenceCreate(simulation_run_id=run_id, translation_mm=(0, 0, 0), unit="m")  # type: ignore[call-arg]


def test_visual_reference_leaves_typed_export_and_findings_unchanged(tmp_path: Path) -> None:
    spec = template_geometry_spec(TubeTemplateCreate(template="tube", params={
        "outer_d_mm": 20, "wall_t_mm": 2, "length_mm": 80}))
    before = tmp_path / "before"
    manifest_before = build_artifacts(spec, before)
    report_before = validate_artifacts(spec, before)
    candidate_id, run_id, _ = _seed()
    add_reference("main", candidate_id, ReferenceCreate(simulation_run_id=run_id, translation_mm=(5, 5, 5)))
    after = tmp_path / "after"
    manifest_after = build_artifacts(spec, after)
    report_after = validate_artifacts(spec, after)
    # STEP embeds a process-global occurrence counter, so it (and the digests derived from it) is
    # compared after normalizing that counter; STL and GLB must match byte for byte.
    for name in ("model.stl", "model.glb"):
        assert (before / name).read_bytes() == (after / name).read_bytes(), name
    counter = re.compile(rb"(NEXT_ASSEMBLY_USAGE_OCCURRENCE\(')\d+'")
    assert (counter.sub(rb"\1#'", (before / "model.step").read_bytes())
            == counter.sub(rb"\1#'", (after / "model.step").read_bytes()))

    def stable(manifest: dict) -> dict:
        trimmed = json.loads(json.dumps(manifest))
        trimmed["artifacts"]["model.step"].pop("sha256")
        trimmed.pop("manifest_digest")
        return trimmed

    assert stable(manifest_before) == stable(manifest_after)
    report_before.pop("manifest_sha256"), report_after.pop("manifest_sha256")  # derived from the STEP counter
    assert report_before == report_after
    import app.modules.bluecad.export as export_module
    import app.modules.bluecad.spec as spec_module
    import app.modules.bluecad.validate as validate_module
    for module in (export_module, spec_module, validate_module):
        assert "studio_references" not in inspect.getsource(module)
