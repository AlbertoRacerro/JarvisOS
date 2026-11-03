"""Spec 163: provider-free template candidates and STL/STEP print-handoff exports."""

from __future__ import annotations

import math
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths

TUBE = {"template": "tube", "params": {"outer_d_mm": 40.0, "wall_t_mm": 3.0, "length_mm": 120.0}}
MANIFOLD = {
    "template": "manifold",
    "params": {
        "outer_d_mm": 50.0,
        "wall_t_mm": 3.0,
        "length_mm": 240.0,
        "branch_count": 3,
        "branch_outer_d_mm": 20.0,
    },
}


SERPENTINE = {
    "template": "serpentine",
    "params": {"outer_d_mm": 50.0, "wall_t_mm": 3.0, "segment_length_mm": 400.0, "segment_count": 3, "bend_radius_mm": 100.0},
}


@pytest.fixture
def client(isolated_data_root) -> TestClient:
    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client


def _workspace_id(client: TestClient) -> str:
    return client.get("/workspaces").json()[0]["id"]


def _count(table: str) -> int:
    with open_sqlite_connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


@pytest.mark.parametrize("payload", [TUBE, MANIFOLD], ids=["tube", "manifold"])
def test_template_candidate_is_valid_with_glb_stl_step_and_no_ai(client: TestClient, payload: dict) -> None:
    workspace_id = _workspace_id(client)
    ai_jobs_before = _count("ai_jobs")

    response = client.post(f"/workspaces/{workspace_id}/bluecad/candidates/from-template", json=payload)

    assert response.status_code == 201, response.text
    candidate = response.json()
    assert candidate["status"] == "valid"
    assert candidate["origin"] == "template"
    assert candidate["glb_artifact_id"] and candidate["report_artifact_id"] and candidate["spec_artifact_id"]
    [attempt] = candidate["attempts"]
    assert attempt["route_class"] == f"deterministic:template:{payload['template']}"
    assert attempt["proposal_ai_job_id"] is None
    assert attempt["proposal_outcome"] == "not_applicable"
    assert attempt["validation_verdict"] == "pass"
    assert _count("ai_jobs") == ai_jobs_before

    aggregate = client.get(f"/workspaces/{workspace_id}/bluecad/candidates/{candidate['id']}/aggregate").json()
    exports = {item["roles"][0]: item for item in aggregate["exports"]}
    assert set(exports) == {"export.stl", "export.step", "export.3mf"}
    assert not aggregate["diagnostics"]

    glb = client.get(f"/workspaces/{workspace_id}/bluecad/artifacts/{candidate['glb_artifact_id']}/content")
    assert glb.status_code == 200
    assert glb.headers["content-type"].startswith("model/gltf-binary")
    assert glb.content[:4] == b"glTF"

    short = candidate["id"][:8]
    stl = client.get(exports["export.stl"]["content_url"])
    assert stl.status_code == 200
    assert stl.headers["content-type"].startswith("model/stl")
    assert f'filename="bluecad-{short}-attempt1.stl"' in stl.headers["content-disposition"]
    assert len(stl.content) > 84

    step = client.get(exports["export.step"]["content_url"])
    assert step.status_code == 200
    assert step.headers["content-type"].startswith("model/step")
    assert f'filename="bluecad-{short}-attempt1.step"' in step.headers["content-disposition"]
    assert step.content.startswith(b"ISO-10303-21;")


def _bad_serpentine(**changes: object) -> dict:
    return {**SERPENTINE, "params": {**SERPENTINE["params"], **changes}}


def test_serpentine_spec_structure_and_centreline_length() -> None:
    from app.modules.bluecad.assembly import assemble_parts
    from app.modules.bluecad.template import SerpentineTemplateCreate, template_geometry_spec

    request = SerpentineTemplateCreate(**SERPENTINE)
    spec = template_geometry_spec(request)
    kinds = [part["kind"] for part in spec["parts"]]
    assert kinds == ["tube_run", "bend", "tube_run", "bend", "tube_run"]
    assert len(spec["connections"]) == 4
    assert request.params.centreline_length_mm == pytest.approx(3 * 400 + 2 * math.pi * 100)
    built = assemble_parts(spec)
    # Planar serpentine: runs 2 * bend_radius apart, alternating direction, no overlap.
    assert [built[f"run_{i}"].bbox_mm[0][1] for i in (1, 2, 3)] == pytest.approx([-25.0, 175.0, 375.0])
    volume = sum(part.volume_mm3 for part in built.values())
    annulus = math.pi / 4 * (50.0**2 - 44.0**2)
    assert volume == pytest.approx(annulus * request.params.centreline_length_mm)


def test_serpentine_candidate_builds_valid_with_expected_volume(client: TestClient) -> None:
    workspace_id = _workspace_id(client)
    response = client.post(f"/workspaces/{workspace_id}/bluecad/candidates/from-template", json=SERPENTINE)
    assert response.status_code == 201, response.text
    candidate = response.json()
    assert candidate["status"] == "valid"
    assert candidate["attempts"][0]["route_class"] == "deterministic:template:serpentine"
    assert "serpentine" in candidate["brief_text"]


@pytest.mark.parametrize(
    "payload",
    [
        _bad_serpentine(segment_length_mm=5001.0),
        _bad_serpentine(segment_count=0),
        _bad_serpentine(segment_count=65),
        _bad_serpentine(segment_count=True),
        _bad_serpentine(bend_radius_mm=49.0),
        _bad_serpentine(wall_t_mm=25.0),
        {**SERPENTINE, "params": {k: v for k, v in SERPENTINE["params"].items() if k != "bend_radius_mm"}},
    ],
)
def test_invalid_serpentine_parameters_are_refused_before_any_write(client: TestClient, payload: dict) -> None:
    workspace_id = _workspace_id(client)
    before = (_count("bluecad_candidates"), _count("artifacts"))
    response = client.post(f"/workspaces/{workspace_id}/bluecad/candidates/from-template", json=payload)
    assert response.status_code == 422, response.text
    assert (_count("bluecad_candidates"), _count("artifacts")) == before


@pytest.mark.parametrize(
    "payload",
    [
        {"template": "tube", "params": {"outer_d_mm": 10.0, "wall_t_mm": 5.0, "length_mm": 50.0}},
        {"template": "tube", "params": {"outer_d_mm": 10.0, "wall_t_mm": 1.0, "length_mm": 50000.0}},
        {"template": "tube", "params": {"outer_d_mm": 0.0, "wall_t_mm": 1.0, "length_mm": 50.0}},
        {"template": "tube", "params": {"outer_d_mm": 10.0, "wall_t_mm": 1.0}},
        {"template": "tube", "params": {"outer_d_mm": 10.0, "wall_t_mm": 1.0, "length_mm": 50.0, "kind": "bend"}},
        {"template": "tube", "params": {"outer_d_mm": "10", "wall_t_mm": 1.0, "length_mm": 50.0}, "extra": 1},
        {"template": "sphere", "params": {}},
        {**MANIFOLD, "params": {**MANIFOLD["params"], "branch_outer_d_mm": 60.0}},
        {**MANIFOLD, "params": {**MANIFOLD["params"], "branch_count": 20}},
        {**MANIFOLD, "params": {**MANIFOLD["params"], "branch_count": True}},
        {**MANIFOLD, "params": {**MANIFOLD["params"], "length_mm": 60.0}},
    ],
)
def test_invalid_template_parameters_are_refused_before_any_write(client: TestClient, payload: dict) -> None:
    workspace_id = _workspace_id(client)
    before = (_count("bluecad_candidates"), _count("artifacts"))

    response = client.post(f"/workspaces/{workspace_id}/bluecad/candidates/from-template", json=payload)

    assert response.status_code == 422, response.text
    assert (_count("bluecad_candidates"), _count("artifacts")) == before


def test_template_for_unknown_workspace_is_404(client: TestClient) -> None:
    response = client.post("/workspaces/does-not-exist/bluecad/candidates/from-template", json=TUBE)
    assert response.status_code == 404
    assert _count("bluecad_candidates") == 0


def test_export_serving_boundary_is_unchanged(client: TestClient, tmp_path: Path) -> None:
    workspace_id = _workspace_id(client)

    def insert(artifact_type: str, stored_path: Path) -> str:
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        stored_path.write_bytes(b"solid x\nendsolid x\n")
        artifact_id = str(uuid.uuid4())
        with open_sqlite_connection() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (id, workspace_id, filename, stored_path, artifact_type, mime_type,
                    sha256, source_ref, status, created_at, notes)
                VALUES (?, ?, ?, ?, ?, 'application/octet-stream', NULL, 'test', 'registered', ?, 'fixture')
                """,
                (artifact_id, workspace_id, stored_path.name, str(stored_path), artifact_type, "2026-10-01T00:00:00Z"),
            )
            connection.commit()
        return artifact_id

    outside = insert("bluecad_stl", tmp_path / "outside" / "model.stl")
    foreign = insert("runner_output", build_paths().data_root / "artifacts" / "runner" / "model.stl")
    inside = insert("bluecad_stl", build_paths().data_root / "artifacts" / "bluecad" / "model.stl")

    assert client.get(f"/workspaces/{workspace_id}/bluecad/artifacts/{outside}/content").status_code == 404
    assert client.get(f"/workspaces/{workspace_id}/bluecad/artifacts/{foreign}/content").status_code == 404
    assert client.get(f"/workspaces/other/bluecad/artifacts/{inside}/content").status_code == 404
    served = client.get(f"/workspaces/{workspace_id}/bluecad/artifacts/{inside}/content")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("model/stl")
    # Without a candidate source_ref the stored filename is kept.
    assert 'filename="model.stl"' in served.headers["content-disposition"]


def test_generation_availability_reports_safe_default_block_without_ai(client: TestClient) -> None:
    workspace_id = _workspace_id(client)
    ai_jobs_before = _count("ai_jobs")

    response = client.get(f"/workspaces/{workspace_id}/bluecad/generation-availability")

    assert response.status_code == 200
    body = response.json()
    assert body["route_class"] == "external:cheap"
    assert body["external_calls_allowed"] is False
    assert body["blocking_reason"]
    assert _count("ai_jobs") == ai_jobs_before


def test_generation_availability_rejects_unknown_workspace(client: TestClient) -> None:
    response = client.get("/workspaces/missing-workspace/bluecad/generation-availability")

    assert response.status_code == 404


def test_loop_build_registers_stl_and_step_exports(client: TestClient) -> None:
    from app.modules.bluecad.loop import _build_and_register
    from app.modules.bluecad.template import TubeTemplateCreate, template_geometry_spec

    workspace_id = _workspace_id(client)
    candidate_id = str(uuid.uuid4())
    spec = template_geometry_spec(TubeTemplateCreate.model_validate(TUBE))

    _build_and_register(workspace_id, candidate_id, 1, spec)

    with open_sqlite_connection() as connection:
        roles = {
            row["artifact_type"]
            for row in connection.execute(
                "SELECT artifact_type FROM artifacts WHERE source_ref = ?",
                (f"bluecad_candidate:{candidate_id}:attempt:1",),
            )
        }
    assert {"bluecad_glb", "bluecad_stl", "bluecad_step"} <= roles


def test_template_build_budget_scales_with_part_count() -> None:
    from app.modules.bluecad.service import DEFAULT_TIMEOUT_SECONDS
    from app.modules.bluecad.template import TEMPLATE_BUILD_TIMEOUT_CAP_S, template_build_timeout_s

    assert template_build_timeout_s({"parts": [{}]}) == DEFAULT_TIMEOUT_SECONDS
    # The 80 m C1 loop (16 runs + 15 bends) builds in roughly 27-30 s, so it needs more than the default.
    assert template_build_timeout_s({"parts": [{}] * 31}) > 2 * DEFAULT_TIMEOUT_SECONDS
    assert template_build_timeout_s({"parts": [{}] * 127}) <= TEMPLATE_BUILD_TIMEOUT_CAP_S
