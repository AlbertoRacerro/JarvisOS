"""Spec 216 contract 8/9: a Design-owned CFD study template per regime makes reduced models obsolete on input change."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from app.core.database import initialize_database, open_sqlite_connection
from app.main import app
from app.modules.bluecad.cfd_pipe.domain import derive_fluid_domain
from app.modules.bluecad.cfd_pipe.reduced_model import study_template_digest
from app.modules.workspaces import design_components
from tests.test_216_component_reduced_models import _body as _publish_body
from tests.test_216_component_reduced_models import _project, _publisher, _ready
from tests.test_216_process_pin_from_design import (  # noqa: F401  (water is an autouse fixture)
    _body,
    _evaluate,
    _pin_url,
    _put,
    _setup,
    water,
)
from tests.test_216_reduced_model import c1_model, tube, turbulent


def _set(client: TestClient, base: str, project_id: str, component_id: str, contract, regime: str | None = None,
         revision: int | None = None):
    if revision is None:
        revision = next(item for item in client.get(base).json() if item["id"] == component_id)["revision"]
    return client.put(f"{base}/{component_id}/cfd-study-templates/{regime or contract.regime}", json={
        "contract": contract.model_dump(mode="json"), "expected_revision": revision,
        "expected_active_version_id": project_id})


def test_put_and_get_record_the_template_bump_the_revision_and_log_history() -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id, base, component_id, revision = _ready(client, "tpl-ok")
        contract = turbulent(1e4)
        response = _set(client, base, project_id, component_id, contract)
        assert response.status_code == 200, response.text
        assert response.json()["regime"] == "turbulent_rans"
        assert response.json()["template_digest"] == study_template_digest(contract)
        (listed,) = client.get(f"{base}/{component_id}/cfd-study-templates").json()
        assert listed == response.json() and listed["contract"]["mesh_level"] == 1
        assert client.get(base).json()[0]["revision"] == revision + 1
        # replacing the same regime keeps one row
        assert _set(client, base, project_id, component_id, turbulent(1e4, outlet_pressure_pa=1000.0)).status_code == 200
        assert len(client.get(f"{base}/{component_id}/cfd-study-templates").json()) == 1
    with open_sqlite_connection() as connection:
        events = connection.execute(
            "SELECT payload, actor FROM events WHERE event_type = 'DesignComponentStudyTemplateSet' ORDER BY rowid").fetchall()
    assert len(events) == 2 and events[0]["actor"] == "local-user"
    payload = json.loads(events[0]["payload"])
    assert payload["regime"] == "turbulent_rans" and payload["template_digest"] == study_template_digest(contract)
    assert payload["geometry_digest"] == contract.domain.geometry_digest


def test_stale_revision_regime_mismatch_and_geometry_mismatch_are_refused() -> None:
    initialize_database()
    with TestClient(app) as client:
        project_id, base, component_id, revision = _ready(client, "tpl-refused")
        assert _set(client, base, project_id, component_id, turbulent(1e4), revision=revision - 1).status_code == 409
        wrong = _set(client, base, project_id, component_id, turbulent(1e4), regime="laminar")
        assert wrong.status_code == 422 and "does not match path regime" in wrong.text
        other = _set(client, base, project_id, component_id, turbulent(1e4, domain=derive_fluid_domain(tube(3000.0))))  # different tube geometry
        assert other.status_code == 422 and "fluid domain differs" in other.text
        contract = turbulent(1e4)
        forged = contract.model_copy(update={
            "domain": contract.domain.model_copy(update={"inner_diameter_m": contract.domain.inner_diameter_m * 1.01}),
        })
        mismatch = _set(client, base, project_id, component_id, forged)
        assert mismatch.status_code == 422 and "fluid domain differs" in mismatch.text
        _, base2, bare_id = _project(client, "tpl-nogeo")
        bare = _set(client, base2, project_id, bare_id, turbulent(1e4), revision=1)
        assert bare.status_code in (404, 409)
        assert client.get(f"{base}/{component_id}/cfd-study-templates").json() == []
        assert client.get(f"{base}/missing/cfd-study-templates").status_code == 404


def test_publish_is_refused_when_points_disagree_with_the_recorded_template(monkeypatch) -> None:
    initialize_database()
    calls: list = []
    with TestClient(app) as client:
        project_id, base, component_id, _ = _ready(client, "tpl-publish")
        _publisher(monkeypatch, calls, project_id)
        assert _set(client, base, project_id, component_id, turbulent(1e4, outlet_pressure_pa=1000.0)).status_code == 200
        revision = client.get(base).json()[0]["revision"]
        refused = client.post(f"{base}/{component_id}/reduced-models", json=_publish_body(project_id, revision, component_id))
        assert refused.status_code == 409 and "current turbulent_rans study inputs" in refused.json()["detail"]
        assert calls == [] and client.get(base).json()[0]["models"] == []
        assert _set(client, base, project_id, component_id, turbulent(1e4)).status_code == 200
        revision = client.get(base).json()[0]["revision"]
        ok = client.post(f"{base}/{component_id}/reduced-models", json=_publish_body(project_id, revision, component_id))
        assert ok.status_code == 200 and ok.json()["status"] == "current" and calls == [project_id]


def _template(client: TestClient, ctx: dict[str, Any], contract) -> None:
    response = _set(client, ctx["base"], ctx["project_id"], ctx["component_id"], contract)
    assert response.status_code == 200, response.text


def test_changed_template_makes_the_model_obsolete_everywhere_and_unchanged_keeps_it_current() -> None:
    initialize_database()
    model = c1_model()
    with TestClient(app) as client:
        ctx = _setup(client, "tpl-obsolete")
        assert model.segments[0].study_template_digest == study_template_digest(turbulent(1e4))
        _template(client, ctx, turbulent(2e4))  # same inputs, different point fields
        (status,) = client.get(ctx["path"]).json()
        assert status["status"] == "current" and status["obsolete_reasons"] == []
        pinned = client.post(_pin_url(ctx), json=_body(ctx, client))
        assert pinned.status_code == 200, pinned.text
        assert not any(i["code"] == "PBR_HYDRAULICS_MODEL_OBSOLETE" for i in _evaluate(ctx)["findings"])

        _template(client, ctx, turbulent(1e4, outlet_pressure_pa=1000.0))
        (status,) = client.get(ctx["path"]).json()
        assert status["status"] == "obsolete"
        assert status["obsolete_reasons"] == ["turbulent_rans study inputs changed since publication; rerun the study"]
        (finding,) = [i for i in _evaluate(ctx)["findings"] if i["code"] == "PBR_HYDRAULICS_MODEL_OBSOLETE"]
        assert "turbulent_rans study inputs changed since the study" in finding["message"]
        (why,) = design_components.pinned_reduced_model_obsolescence(
            ctx["project_id"], "reduced-v1", model.geometry_digest)
        assert why.endswith("turbulent_rans study inputs changed since the study (reduced model obsolete, rerun the study)")


def test_process_pin_is_refused_after_the_template_changes() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "tpl-refuse-pin")
        _template(client, ctx, turbulent(1e4, outlet_pressure_pa=1000.0))
        response = client.post(_pin_url(ctx), json=_body(ctx, client))
        assert response.status_code == 409 and "study inputs changed" in response.text


def test_regime_without_a_record_is_not_input_checked() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "tpl-other-regime")
        _put(client, ctx, "geometry", candidate_id="tube-a")  # keep geometry; no template for turbulent_rans
        (status,) = client.get(ctx["path"]).json()
        assert status["status"] == "current"
