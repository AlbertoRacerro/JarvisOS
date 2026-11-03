"""Spec 216 contract 8: read-only CFD reduced-model status of a Design component."""

from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.database import initialize_database, open_sqlite_connection
from app.main import app
from app.modules.bluecad.cfd_pipe.reduced_model import PublicationRefused
from app.modules.bluecad.cfd_pipe.studio_publication import reduced_model_of_source, studio_source
from app.modules.bluecad.spec import canonical_json
from app.modules.bluecad.template import TubeTemplateCreate, template_geometry_spec
from app.modules.workspaces import design_components
from tests.cfd_run_fixtures import turbulent_study, write_run
from tests.test_216_reduced_model import TOL, c1_model, turbulent

CALC = "def main(inputs):\n    return {'y': inputs['x']}\n"


def _tube(length_mm: float) -> dict:
    return template_geometry_spec(TubeTemplateCreate.model_validate(
        {"template": "tube", "params": {"outer_d_mm": 50.0, "wall_t_mm": 2.0, "length_mm": length_mm}}))


def _add_candidate(workspace_id: str, candidate_id: str, spec: dict) -> None:
    path = get_settings().data_root / f"{candidate_id}.json"
    path.write_text(canonical_json(spec) + "\n", encoding="utf-8")
    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT INTO artifacts (id, workspace_id, filename, stored_path, artifact_type, status, created_at) "
            "VALUES (?, ?, ?, ?, 'bluecad_spec', 'registered', '2026-01-01')",
            (f"art-{candidate_id}", workspace_id, path.name, str(path)))
        connection.execute(
            "INSERT INTO bluecad_candidates (id, workspace_id, brief_text, brief_digest, status, origin, "
            "loop_config_json, spec_artifact_id, created_at, updated_at) VALUES (?, ?, 'tube', 'digest', 'valid', "
            "'template', '{}', ?, '2026-01-01', '2026-01-01')", (candidate_id, workspace_id, f"art-{candidate_id}"))
        connection.commit()


def _add_published(workspace_id: str, version_id: str, source: str) -> None:
    path = get_settings().data_root / f"{version_id}.py"
    path.write_text(source, encoding="utf-8")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT INTO artifacts (id, workspace_id, filename, stored_path, artifact_type, status, created_at) "
            "VALUES (?, ?, 'studio_calc.py', ?, 'python_script', 'registered', '2026-01-01')",
            (f"art-{version_id}", workspace_id, str(path)))
        connection.execute(
            "INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at) "
            "VALUES (?, ?, 'Calc', 'q', '2026-01-01', '2026-01-01')", (f"spec-{version_id}", workspace_id))
        connection.execute(
            "INSERT INTO model_versions (id, workspace_id, model_spec_id, version_label, implementation_artifact_id, "
            "implementation_kind, created_at) VALUES (?, ?, ?, 'v1', ?, 'studio_calc_v1', '2026-01-01')",
            (version_id, workspace_id, f"spec-{version_id}", f"art-{version_id}"))
        connection.execute(
            "INSERT INTO studio_published_versions (version_id, model_spec_id, contract_json, source_sha256, "
            "contract_sha256, created_at) VALUES (?, ?, '{}', ?, 'contract', '2026-01-01')",
            (version_id, f"spec-{version_id}", digest))
        connection.commit()


def _project(client: TestClient, slug: str) -> tuple[str, str, str]:
    project_id = client.post("/workspaces", json={"name": slug, "slug": slug}).json()["id"]
    base = f"/workspaces/{project_id}/design-versions/{project_id}/components"
    component = client.post(base, json={"name": "Tube", "expected_active_version_id": project_id}).json()
    return project_id, base, component["id"]


def _attach(client: TestClient, base: str, project_id: str, component_id: str, kind: str, **fields) -> None:
    revision = client.get(base).json()[0]["revision"]
    response = client.put(f"{base}/{component_id}/{kind}", json={
        **fields, "expected_revision": revision, "expected_active_version_id": project_id})
    assert response.status_code == 200, response.text


def test_reduced_model_is_current_while_geometry_matches_and_obsolete_after_change() -> None:
    initialize_database()
    model = c1_model()
    with TestClient(app) as client:
        project_id, base, component_id = _project(client, "reduced-current")
        _add_candidate(project_id, "tube-a", _tube(2600.0))
        _add_candidate(project_id, "tube-b", _tube(2100.0))
        _add_published(project_id, "reduced-v1", studio_source(model))
        _attach(client, base, project_id, component_id, "geometry", candidate_id="tube-a")
        _attach(client, base, project_id, component_id, "models", model_version_id="reduced-v1")
        path = f"{base}/{component_id}/reduced-models"
        (current,) = client.get(path).json()
        assert current["status"] == "current" and current["obsolete_reasons"] == []
        assert current["fidelity"] == "CFD reduced model (scientifically unqualified)"
        assert current["model_version_id"] == "reduced-v1" and current["node"] == "Physics"
        assert current["reduced_model_id"] == model.model_id and current["reduced_model_version_id"] == model.version_id
        assert current["geometry_digest"] == current["current_geometry_digest"] == model.geometry_digest
        (segment,) = current["segments"]
        assert segment["regime"] == "turbulent_rans" and segment["reference"] == "colebrook_smooth"
        assert segment["combined_uncertainty"] == model.segments[0].uncertainty.combined
        assert segment["diameter_m"] == model.segments[0].validity.diameter_m

        _attach(client, base, project_id, component_id, "geometry", candidate_id="tube-b")
        (obsolete,) = client.get(path).json()
        assert obsolete["status"] == "obsolete" and obsolete["model_version_id"] == "reduced-v1"
        assert any("tube geometry changed" in reason for reason in obsolete["obsolete_reasons"])
        assert obsolete["geometry_digest"] == model.geometry_digest != obsolete["current_geometry_digest"]
        assert client.get(base).json()[0]["models"][0]["model_version_id"] == "reduced-v1"


def test_missing_geometry_is_unknown_with_an_honest_reason() -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id, base, component_id = _project(client, "reduced-unknown")
        _add_published(project_id, "reduced-v1", studio_source(c1_model()))
        _attach(client, base, project_id, component_id, "models", model_version_id="reduced-v1")
        (item,) = client.get(f"{base}/{component_id}/reduced-models").json()
        assert item["status"] == "unknown" and item["current_geometry_digest"] is None
        assert "component geometry unavailable; obsolescence cannot be checked" in item["obsolete_reasons"]


def test_ordinary_calculation_is_skipped_and_tampered_reduced_model_is_invalid() -> None:
    initialize_database()
    source = studio_source(c1_model())
    with TestClient(app) as client:
        project_id, base, component_id = _project(client, "reduced-skip")
        _add_published(project_id, "plain-v1", CALC)
        _add_published(project_id, "tampered-v1", source.replace("DIAMETER_M = 0.046", "DIAMETER_M = 0.047"))
        _attach(client, base, project_id, component_id, "models", model_version_id="plain-v1")
        path = f"{base}/{component_id}/reduced-models"
        assert client.get(path).json() == []
        _attach(client, base, project_id, component_id, "models", model_version_id="tampered-v1")
        (item,) = client.get(path).json()
        assert item["model_version_id"] == "tampered-v1" and item["status"] == "invalid"
        assert item["obsolete_reasons"] and item["reduced_model_id"] is None


def test_unknown_component_is_404() -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id, base, _ = _project(client, "reduced-404")
        assert client.get(f"{base}/missing/reduced-models").status_code == 404


def _runs(project_id: str, component_id: str, errors=((1e4, -0.02), (2e4, -0.01), (2.8e4, -0.005))) -> dict:
    """Server run records (no OpenFOAM) and the run-id body that names them."""
    run_ids = [write_run(project_id, component_id, turbulent(re), error=err) for re, err in errors]
    return {"run_ids": run_ids, "mesh_study_run_ids": {"turbulent_rans": turbulent_study(project_id, component_id)}}


def _body(project_id: str, revision: int, component_id: str, **overrides) -> dict:
    return {"model_id": "c1_tube.darcy", **_runs(project_id, component_id), "tolerances": TOL.model_dump(),
            "expected_revision": revision, "expected_active_version_id": project_id, **overrides}


def _publisher(monkeypatch, calls: list, project_id: str, hook=None):
    def fake(workspace_id, model):
        calls.append(workspace_id)
        if hook:
            hook()
        _add_published(project_id, "pub-v1", studio_source(model))
        return {"id": "pub-v1"}
    monkeypatch.setattr(design_components, "publish_to_studio", fake)


def _ready(client: TestClient, slug: str, length_mm: float = 2600.0) -> tuple[str, str, str, int]:
    project_id, base, component_id = _project(client, slug)
    _add_candidate(project_id, "tube-a", _tube(length_mm))
    _attach(client, base, project_id, component_id, "geometry", candidate_id="tube-a")
    return project_id, base, component_id, client.get(base).json()[0]["revision"]


def _pinned(base: str, client: TestClient) -> list:
    return client.get(base).json()[0]["models"]


def test_publish_pins_the_fitted_model_and_returns_current_status(monkeypatch) -> None:
    initialize_database()
    calls: list = []
    with TestClient(app) as client:
        project_id, base, component_id, revision = _ready(client, "publish-ok")
        _publisher(monkeypatch, calls, project_id)
        body = _body(project_id, revision, component_id)
        response = client.post(f"{base}/{component_id}/reduced-models", json=body)
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["status"] == "current" and status["model_version_id"] == "pub-v1" and calls == [project_id]
        assert [m["model_version_id"] for m in _pinned(base, client)] == ["pub-v1"]
        assert client.get(base).json()[0]["revision"] == revision + 1
    carried = reduced_model_of_source((get_settings().data_root / "pub-v1.py").read_text())
    assert carried.provenance["mesh_study_run_ids"] == tuple(
        f"turbulent_rans:{run_id}" for run_id in reversed(body["mesh_study_run_ids"]["turbulent_rans"]))
    assert len(carried.provenance["mesh_study_case_manifest_digests"]) == 3
    with open_sqlite_connection() as connection:
        event = connection.execute(
            "SELECT payload FROM events WHERE event_type = 'DesignComponentReducedModelPublished'").fetchone()
    assert event is not None
    payload = json.loads(event["payload"])
    assert payload["model_version_id"] == "pub-v1" and payload["reduced_model_version_id"] == status["reduced_model_version_id"]
    assert payload["geometry_digest"] == c1_model().geometry_digest and payload["run_ids"] == body["run_ids"]


def test_publish_refused_fit_is_422_with_every_reason_and_pins_nothing(monkeypatch) -> None:
    initialize_database()
    calls: list = []
    with TestClient(app) as client:
        project_id, base, component_id, revision = _ready(client, "publish-refused")
        _publisher(monkeypatch, calls, project_id)
        runs = [write_run(project_id, component_id, turbulent(1e4), status="unmeasured"),
                write_run(project_id, component_id, turbulent(2.8e4), converged=False)]
        body = _body(project_id, revision, component_id) | {"run_ids": runs}
        response = client.post(f"{base}/{component_id}/reduced-models", json=body)
        assert response.status_code == 422
        reasons = response.json()["detail"]["reasons"]
        assert response.json()["detail"]["message"] == "Reduced model publication refused; nothing was created."
        assert any("unmeasured" in r for r in reasons) or any("residual target" in r for r in reasons)
        assert calls == [] and _pinned(base, client) == []


def test_publish_refuses_caller_supplied_numbers_and_unserved_runs(monkeypatch) -> None:
    initialize_database()
    calls: list = []
    with TestClient(app) as client:
        project_id, base, component_id, revision = _ready(client, "publish-forged")
        _, other_base, other_component = _project(client, "publish-other")
        _publisher(monkeypatch, calls, project_id)
        url = f"{base}/{component_id}/reduced-models"
        body = _body(project_id, revision, component_id)
        forged = body | {"points": [], "discretization": []}
        assert client.post(url, json=forged).status_code == 422  # extra fields are forbidden
        assert client.post(url, json=body | {"points": []}).status_code == 422

        def refused(**overrides) -> list[str]:
            response = client.post(url, json=body | overrides)
            assert response.status_code == 422, response.text
            return response.json()["detail"]["reasons"]

        assert any("not a run of this component" in r for r in refused(run_ids=["a" * 32, *body["run_ids"][1:]]))
        foreign = write_run(project_id, other_component, turbulent(1e4))
        assert any("not a run of this component" in r for r in refused(run_ids=[foreign, *body["run_ids"][1:]]))
        assert any("listed more than once" in r for r in refused(run_ids=[body["run_ids"][0]] * 2))
        interrupted = write_run(project_id, component_id, turbulent(1e4), state="interrupted")
        assert any("not finished" in r for r in refused(run_ids=[interrupted, *body["run_ids"][1:]]))
        unmeasured = write_run(project_id, component_id, turbulent(1e4), status="unmeasured")
        assert any("unmeasured: probes unreadable" in r for r in refused(run_ids=[unmeasured, *body["run_ids"][1:]]))
        unconverged = write_run(project_id, component_id, turbulent(1e4), converged=False)
        assert any("residual target" in r for r in refused(run_ids=[unconverged, *body["run_ids"][1:]]))
        undigested = write_run(project_id, component_id, turbulent(1e4), contract_digest_recorded=False)
        assert any("no contract or geometry digest" in r for r in refused(run_ids=[undigested, *body["run_ids"][1:]]))
        study = body["mesh_study_run_ids"]["turbulent_rans"]
        assert any("exactly 3" in r for r in refused(mesh_study_run_ids={"turbulent_rans": study[:2]}))
        assert any("exactly 3" in r for r in refused(mesh_study_run_ids={"turbulent_rans": [study[0]] * 3}))
        assert any("exactly 3" in r for r in refused(mesh_study_run_ids={}))
        same_level = write_run(project_id, component_id, turbulent(2e4, mesh_level=2), cells=300000)
        assert any("three distinct mesh levels" in r for r in refused(
            mesh_study_run_ids={"turbulent_rans": [study[0], study[2], same_level]}))
        no_cells = write_run(project_id, component_id, turbulent(2e4, mesh_level=2), cells=None)
        assert any("no mesh cell count" in r for r in refused(
            mesh_study_run_ids={"turbulent_rans": [*study[:2], no_cells]}))
        other_re = write_run(project_id, component_id, turbulent(1e4, mesh_level=2), cells=230000)
        assert any("one Reynolds number" in r for r in refused(
            mesh_study_run_ids={"turbulent_rans": [*study[:2], other_re]}))
        detached_study = turbulent_study(project_id, component_id, re=2.5e4)
        assert any("must match a measured study point" in r for r in refused(
            mesh_study_run_ids={"turbulent_rans": detached_study}))
        assert any("which has no study runs" in r for r in refused(
            mesh_study_run_ids={"turbulent_rans": study, "laminar": study}))
        unconverged_mesh = write_run(project_id, component_id, turbulent(2e4, mesh_level=2), cells=230000,
                                     converged=False)
        assert any("mesh study contains a run that did not meet" in r for r in refused(
            mesh_study_run_ids={"turbulent_rans": [*study[:2], unconverged_mesh]}))
        mixed_tool_mesh = write_run(project_id, component_id, turbulent(2e4, mesh_level=2), cells=230000,
                                    tool_versions={"openfoam_simplefoam": "2413"})
        assert any("one recorded toolchain" in r for r in refused(
            mesh_study_run_ids={"turbulent_rans": [*study[:2], mixed_tool_mesh]}))
        level0 = write_run(project_id, component_id, turbulent(1e4, mesh_level=0))
        assert any("must share one level" in r for r in refused(run_ids=[level0, *body["run_ids"][1:]]))
        assert calls == [] and _pinned(base, client) == []


def test_publish_geometry_mismatch_is_409_without_calling_studio(monkeypatch) -> None:
    initialize_database()
    calls: list = []
    with TestClient(app) as client:
        project_id, base, component_id, revision = _ready(client, "publish-mismatch", length_mm=2100.0)
        _publisher(monkeypatch, calls, project_id)
        response = client.post(f"{base}/{component_id}/reduced-models", json=_body(project_id, revision, component_id))
        assert response.status_code == 409 and "different tube geometry" in response.json()["detail"]
        assert calls == [] and _pinned(base, client) == []


def test_publish_without_attached_geometry_is_409(monkeypatch) -> None:
    initialize_database()
    calls: list = []
    with TestClient(app) as client:
        project_id, base, component_id = _project(client, "publish-nogeo")
        _publisher(monkeypatch, calls, project_id)
        response = client.post(f"{base}/{component_id}/reduced-models", json=_body(project_id, 1, component_id))
        assert response.status_code == 409 and "no attached geometry" in response.json()["detail"] and calls == []


def test_publish_stale_revision_is_409(monkeypatch) -> None:
    initialize_database()
    calls: list = []
    with TestClient(app) as client:
        project_id, base, component_id, revision = _ready(client, "publish-stale")
        _publisher(monkeypatch, calls, project_id)
        response = client.post(f"{base}/{component_id}/reduced-models", json=_body(project_id, revision - 1, component_id))
        assert response.status_code == 409 and "Component changed" in response.json()["detail"] and calls == []


def test_publish_names_the_studio_version_when_the_component_changed_before_the_pin(monkeypatch) -> None:
    initialize_database()
    calls: list = []
    with TestClient(app) as client:
        project_id, base, component_id, revision = _ready(client, "publish-race")

        def concurrent_edit() -> None:
            with open_sqlite_connection() as connection:
                connection.execute("UPDATE design_components SET revision = revision + 1 WHERE id = ?", (component_id,))
                connection.commit()

        _publisher(monkeypatch, calls, project_id, hook=concurrent_edit)
        response = client.post(f"{base}/{component_id}/reduced-models", json=_body(project_id, revision, component_id))
        assert response.status_code == 409
        assert "pub-v1" in response.json()["detail"] and "published but not pinned" in response.json()["detail"]
        assert _pinned(base, client) == []


def test_publish_studio_refusal_is_422_and_pins_nothing(monkeypatch) -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id, base, component_id, revision = _ready(client, "publish-studio-refused")

        def refuse(workspace_id, model):
            raise PublicationRefused(["Studio runtime unavailable (test); nothing was created"])

        monkeypatch.setattr(design_components, "publish_to_studio", refuse)
        response = client.post(f"{base}/{component_id}/reduced-models", json=_body(project_id, revision, component_id))
        assert response.status_code == 422
        assert "Studio runtime unavailable" in response.json()["detail"]["reasons"][0]
        assert _pinned(base, client) == []
