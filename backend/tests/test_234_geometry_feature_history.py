"""Spec 234 feature compilation, attachment, regeneration and fork evidence."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.core.database import initialize_database, open_sqlite_connection
from app.main import app
from app.modules.bluecad.cfd_pipe.domain import derive_fluid_domain
from app.modules.bluecad.cfd_pipe.reduced_model import publish
from app.modules.bluecad.feature_history import (
    FeatureHistoryError,
    compile_feature_sequence,
    synthesize_feature_sequence_from_spec,
)
from app.modules.bluecad.template import SerpentineTemplateCreate, template_geometry_spec
from tests.test_216_component_reduced_models import (
    _add_candidate,
    _add_published,
    _attach,
    _project,
    _tube,
    studio_source,
)
from tests.test_216_reduced_model import TOL, point, turbulent, turbulent_mesh


def _sequence(length: float = 2600.0) -> dict:
    return {
        "parameters": [
            {"name": "outer_d_mm", "value": 50.0},
            {"name": "wall_t_mm", "value": 2.0},
            {"name": "length_mm", "value": length},
        ],
        "features": [{"feature_id": "tube", "kind": "tube_run", "params": {
            name: {"$param": name} for name in ("outer_d_mm", "wall_t_mm", "length_mm")}}],
    }


def _put(client: TestClient, project: str, base: str, component: str, sequence: dict, revision: int):
    return client.put(f"{base}/{component}/geometry/feature-history", json={
        "sequence": sequence, "expected_revision": revision, "expected_active_version_id": project,
    })


def test_compiler_catalog_determinism_and_refusals() -> None:
    seq = _sequence()
    seq["features"].append({"feature_id": "unused", "kind": "tube_run", "enabled": False, "params": {}})
    seq["features"].append({"feature_id": "curve", "kind": "bend", "params": {
        "outer_d_mm": 50, "wall_t_mm": 2, "bend_radius_mm": 60, "angle_deg": 90},
        "connect_to": {"from_port": "port_a", "to_feature_id": "tube", "to_port": "port_b"}})
    canonical, spec, digest = compile_feature_sequence(seq)
    assert compile_feature_sequence(copy.deepcopy(seq)) == (canonical, spec, digest)
    assert len(spec["parts"]) == 2
    assert spec["connections"] == [{"from": "tube.port_b", "to": "curve.port_a"}]
    assert spec["parts"][0]["params"]["length"] == 2600
    framed = _sequence()
    framed["features"][0]["frame"] = {"origin": [0, 0, 0], "direction": [1, 0, 0]}
    assert compile_feature_sequence(framed)[1]["parts"][0]["frame"]["direction"] == [1.0, 0.0, 0.0]
    manifold = {"features": [{"feature_id": "header", "kind": "manifold", "params": {
        "outer_d_mm": 100, "wall_t_mm": 3, "length_mm": 1000,
        "branch_count": 2, "branch_outer_d_mm": 20}}]}
    assert compile_feature_sequence(manifold)[1]["parts"][0]["params"]["n_out"] == 2
    serpentine = {"features": [{"feature_id": "loop", "kind": "serpentine", "params": {
        "outer_d_mm": 50, "wall_t_mm": 2, "segment_length_mm": 1000,
        "segment_count": 3, "bend_radius_mm": 60}}]}
    loop = compile_feature_sequence(serpentine)[1]
    assert len(loop["parts"]) == 5 and len(loop["connections"]) == 4
    assert loop["parts"][0]["part_id"] == "loop_run_1"
    template_loop = template_geometry_spec(SerpentineTemplateCreate.model_validate({
        "template": "serpentine", "params": {"outer_d_mm": 50, "wall_t_mm": 2,
                                               "segment_length_mm": 1000, "segment_count": 3,
                                               "bend_radius_mm": 60}}))
    assert synthesize_feature_sequence_from_spec(template_loop).features[0].kind == "serpentine"
    bad = copy.deepcopy(seq)
    bad["features"][0]["params"]["length_mm"] = {"$param": "missing"}
    with pytest.raises(FeatureHistoryError, match="Unknown parameter") as error:
        compile_feature_sequence(bad)
    assert error.value.code == "unknown_parameter" and error.value.status_code == 422
    bad = _sequence()
    bad["parameters"][1]["value"] = 25
    with pytest.raises(FeatureHistoryError) as error:
        compile_feature_sequence(bad)
    assert error.value.code == "feature_geometry_invalid"
    bad = _sequence()
    bad["features"][0]["enabled"] = False
    with pytest.raises(FeatureHistoryError) as error:
        compile_feature_sequence(bad)
    assert error.value.code == "no_enabled_features"
    bad = copy.deepcopy(seq)
    bad["features"][2]["connect_to"]["to_feature_id"] = "unused"
    with pytest.raises(FeatureHistoryError) as error:
        compile_feature_sequence(bad)
    assert error.value.code == "invalid_connection_target"
    duplicate = _sequence()
    duplicate["parameters"].append(copy.deepcopy(duplicate["parameters"][0]))
    with pytest.raises(FeatureHistoryError) as error:
        compile_feature_sequence(duplicate)
    assert error.value.code == "duplicate_parameter"


def test_lifecycle_regeneration_failure_atomicity_and_fork(monkeypatch) -> None:
    initialize_database()
    with TestClient(app) as client:
        project, base, component = _project(client, "feature-history")
        path = f"{base}/{component}/geometry/feature-history"
        assert client.get(path).json()["sequence"] is None
        _add_candidate(project, "old-template", _tube(1000))
        _attach(client, base, project, component, "geometry", candidate_id="old-template")
        synthetic = client.get(path).json()
        assert synthetic["persisted"] is False and synthetic["sequence"]["features"][0]["kind"] == "tube_run"
        revision = client.get(base).json()[0]["revision"]
        first = _put(client, project, base, component, _sequence(), revision)
        assert first.status_code == 200, first.text
        first_data = first.json()
        first_id = first_data["candidate_id"]
        assert first_data["persisted"] and first_data["revision"] == 1
        assert client.get(path).json() == first_data
        assert client.get(base).json()[0]["revision"] == revision + 1
        with open_sqlite_connection() as connection:
            candidate = connection.execute("SELECT origin, status FROM bluecad_candidates WHERE id = ?", (first_id,)).fetchone()
            attached = connection.execute("SELECT candidate_id FROM design_component_geometry WHERE component_id = ?", (component,)).fetchone()
            assert candidate["origin"] == "feature_history" and candidate["status"] == "valid"
            assert attached["candidate_id"] == first_id
        old_digest = client.get(f"{base}/{component}/fluid-domain").json()["geometry_digest"]
        domain = derive_fluid_domain(compile_feature_sequence(_sequence())[1])
        model = publish("c1_tube.darcy", [point(turbulent(re, domain=domain), err)
                                          for re, err in ((1e4, -0.02), (2e4, -0.01), (2.8e4, -0.005))],
                        [turbulent_mesh()], TOL)
        _add_published(project, "history-reduced-v1", studio_source(model))
        _attach(client, base, project, component, "models", model_version_id="history-reduced-v1")
        assert client.get(f"{base}/{component}/reduced-models").json()[0]["status"] == "current"
        revision = client.get(base).json()[0]["revision"]
        second = _put(client, project, base, component, _sequence(3000), revision)
        assert second.status_code == 200, second.text
        second_data = second.json()
        assert second_data["candidate_id"] != first_id and second_data["revision"] == 2
        with open_sqlite_connection() as connection:
            row = connection.execute("SELECT parent_candidate_id FROM bluecad_candidates WHERE id = ?",
                                     (second_data["candidate_id"],)).fetchone()
            assert row["parent_candidate_id"] == first_id
        assert client.get(f"{base}/{component}/fluid-domain").json()["geometry_digest"] != old_digest
        (status,) = client.get(f"{base}/{component}/reduced-models").json()
        assert status["status"] == "obsolete"
        # Spec 216's existing digest check owns the reason text; there is no separate machine code.
        assert any(reason.startswith("tube geometry changed since the reduced model was published")
                   for reason in status["obsolete_reasons"])
        revision = client.get(base).json()[0]["revision"]
        bad = _sequence()
        bad["features"][0]["params"]["length_mm"] = {"$param": "unknown"}
        assert _put(client, project, base, component, bad, revision).status_code == 422
        assert client.get(path).json() == second_data
        assert client.get(base).json()[0]["revision"] == revision
        from app.modules.bluecad import candidate_build
        from app.modules.bluecad.models import BluecadError, BuildResult
        from app.modules.bluecad.validate import write_validation_report

        def failed_build(spec, out_dir, *, timeout_s):
            report = write_validation_report(spec, out_dir, error=BluecadError("KERNEL_ERROR", {"message": "test failure"}))
            return BuildResult(spec["spec_id"], out_dir, None, out_dir / "validation_report.json", None,
                               report, "error", report["errors"])

        monkeypatch.setattr(candidate_build, "build_geometry_spec", failed_build)
        failed = _put(client, project, base, component, _sequence(3500), revision)
        assert failed.status_code == 409, failed.text
        with open_sqlite_connection() as connection:
            parked = connection.execute("SELECT status FROM bluecad_candidates WHERE origin = 'feature_history' "
                                        "ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
            assert parked["status"] == "parked"
        assert client.get(path).json() == second_data
        assert client.get(base).json()[0]["revision"] == revision
        monkeypatch.undo()
        fork = client.post(f"/workspaces/{project}/design-versions", json={
            "label": "Fork", "expected_active_version_id": project, "mode": "fork",
        })
        assert fork.status_code == 201, fork.text
        version = next(v for v in fork.json()["versions"] if v["id"] != project)
        fork_base = f"/workspaces/{project}/design-versions/{version['id']}/components"
        fork_component = client.get(fork_base).json()[0]["id"]
        inherited = client.get(f"{fork_base}/{fork_component}/geometry/feature-history").json()
        assert inherited["sequence"] == second_data["sequence"]
        assert inherited["sequence_digest"] == second_data["sequence_digest"]
        assert inherited["candidate_id"] == second_data["candidate_id"]
        assert inherited["persisted"] and not inherited["stale_against_attached_candidate"]
        activated = client.post(f"/workspaces/{project}/design-versions/activate", json={
            "version_id": version["id"], "expected_active_version_id": project})
        assert activated.status_code == 200, activated.text
        fork_revision = client.get(fork_base).json()[0]["revision"]
        regenerated = client.put(f"{fork_base}/{fork_component}/geometry/feature-history", json={
            "sequence": _sequence(3200), "expected_revision": fork_revision,
            "expected_active_version_id": version["id"],
        })
        assert regenerated.status_code == 200, regenerated.text
        fork_candidate = regenerated.json()["candidate_id"]
        assert fork_candidate != second_data["candidate_id"] and regenerated.json()["revision"] == inherited["revision"] + 1
        with open_sqlite_connection() as connection:
            # The inherited candidate lives in the parent version's data workspace; lineage edges stay inside one
            # workspace, so a fork's first regeneration starts a new chain instead of a dangling cross-workspace parent.
            row = connection.execute("SELECT parent_candidate_id FROM bluecad_candidates WHERE id = ?",
                                     (fork_candidate,)).fetchone()
            assert row["parent_candidate_id"] is None
            parent_geometry = connection.execute(
                "SELECT candidate_id FROM design_component_geometry WHERE component_id = ?", (component,)).fetchone()
            assert parent_geometry["candidate_id"] == second_data["candidate_id"]
