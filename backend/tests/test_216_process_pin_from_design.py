"""Spec 216 contract 8: Design pins a current CFD reduced model on the component's Process PBR unit."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.database import initialize_database, open_sqlite_connection
from app.main import app
from app.modules.bluecad.cfd_pipe.studio_publication import studio_source
from app.modules.process_stack import draft, pbr_unit, properties
from app.modules.process_stack.draft_models import SetUnitParams
from tests.plumbing_170_support import pbr_ops, pbr_quantities, pin_of
from tests.test_170_plumbing_runtime import _card, _context, _inlet
from tests.test_216_component_reduced_models import _add_candidate, _add_published, _project, _tube
from tests.test_216_process_hydraulics_pin import DIAMETER, MU, RHO
from tests.test_216_reduced_model import c1_model


@pytest.fixture(autouse=True)
def water(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(self: Any, request: Any) -> Any:
        outputs = [SimpleNamespace(name="density", value=SimpleNamespace(value=RHO)),
                   SimpleNamespace(name="dynamic_viscosity", value=SimpleNamespace(value=MU))]
        return SimpleNamespace(status="succeeded", outputs=outputs, failure=None)

    monkeypatch.setattr(properties.CoolPropPropertyEvaluator, "evaluate", fake)


def _put(client: TestClient, ctx: dict[str, Any], kind: str, **fields: Any) -> None:
    component = next(item for item in client.get(ctx["base"]).json() if item["id"] == ctx["component_id"])
    extra = {"expected_draft_revision": draft.projection(ctx["project_id"], ctx["draft_id"])["revision"]} if kind == "geometry" else {}
    response = client.put(f"{ctx['base']}/{ctx['component_id']}/{kind}", json={
        **fields, **extra, "expected_revision": component["revision"], "expected_active_version_id": ctx["project_id"]})
    assert response.status_code == 200, response.text


def _setup(client: TestClient, slug: str) -> dict[str, Any]:
    project_id, base, plain_id = _project(client, slug)
    _add_candidate(project_id, "tube-a", _tube(2600.0))
    _add_candidate(project_id, "tube-b", _tube(2100.0))
    _add_published(project_id, "reduced-v1", studio_source(c1_model()))
    process = draft.create_draft(project_id, "Plant")
    changed = draft.patch(project_id, process["draft_id"], process["revision"], [
        *pbr_ops(), SetUnitParams(op="set_unit_params", unit="pbr", values=pbr_quantities(tube_inner_diameter=(DIAMETER, "m")))])
    entry = client.post(f"{base}/from-unit", json={
        "draft_id": process["draft_id"], "unit_id": "pbr", "expected_draft_revision": changed["revision"],
        "expected_active_version_id": project_id})
    assert entry.status_code == 200, entry.text
    component_id = entry.json()["id"]
    ctx = {"project_id": project_id, "base": base, "component_id": component_id, "draft_id": process["draft_id"],
           "plain_id": plain_id, "path": f"{base}/{component_id}/reduced-models"}
    _put(client, ctx, "geometry", candidate_id="tube-a")
    _put(client, ctx, "models", model_version_id="reduced-v1")
    return ctx


def _body(ctx: dict[str, Any], client: TestClient, **overrides: Any) -> dict[str, Any]:
    component = next(item for item in client.get(ctx["base"]).json() if item["id"] == ctx["component_id"])
    return {"expected_active_version_id": ctx["project_id"], "expected_revision": component["revision"],
            "expected_draft_revision": draft.projection(ctx["project_id"], ctx["draft_id"])["revision"], **overrides}


def _unit_hydraulics(ctx: dict[str, Any]) -> Any:
    process = draft.projection(ctx["project_id"], ctx["draft_id"])
    return next(item for item in process["objects"] if item["id"] == "pbr").get("hydraulics_model")


def _pin_url(ctx: dict[str, Any], model: str = "reduced-v1") -> str:
    return f"{ctx['path']}/{model}/process-pin"


def test_pin_success_sets_the_unit_hydraulics_logs_and_reports_pinned() -> None:
    initialize_database()
    model = c1_model()
    with TestClient(app) as client:
        ctx = _setup(client, "pin-ok")
        assert client.get(ctx["path"]).json()[0]["process_unit_pinned"] is False
        response = client.post(_pin_url(ctx), json=_body(ctx, client, out_of_range="warn"))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "current" and response.json()["process_unit_pinned"] is True
        pin = _unit_hydraulics(ctx)
        assert pin["studio_version_id"] == "reduced-v1" and pin["reduced_model_version_id"] == model.version_id
        assert pin["geometry_digest"] == model.geometry_digest and pin["out_of_range"] == "warn"
        revision = draft.projection(ctx["project_id"], ctx["draft_id"])["revision"]
    with open_sqlite_connection() as connection:
        event = connection.execute(
            "SELECT payload FROM events WHERE event_type = 'DesignComponentProcessHydraulicsPinned'").fetchone()
    payload = json.loads(event["payload"])
    assert payload["unit_id"] == "pbr" and payload["draft_id"] == ctx["draft_id"] and payload["draft_revision"] == revision
    assert payload["model_version_id"] == "reduced-v1" and payload["geometry_digest"] == model.geometry_digest


def test_obsolete_model_is_refused_and_the_draft_is_unchanged() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "pin-obsolete")
        _put(client, ctx, "geometry", candidate_id="tube-b")
        before = draft.projection(ctx["project_id"], ctx["draft_id"])["revision"]
        response = client.post(_pin_url(ctx), json=_body(ctx, client))
        assert response.status_code == 409 and "tube geometry changed" in response.text
        assert draft.projection(ctx["project_id"], ctx["draft_id"])["revision"] == before
        assert _unit_hydraulics(ctx) is None


def test_stale_draft_and_component_revisions_are_409() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "pin-stale")
        assert client.post(_pin_url(ctx), json=_body(ctx, client, expected_draft_revision="stale")).status_code == 409
        assert client.post(_pin_url(ctx), json=_body(ctx, client, expected_revision=99)).status_code == 409
        assert client.post(_pin_url(ctx), json=_body(ctx, client, expected_active_version_id="other")).status_code == 409
        assert _unit_hydraulics(ctx) is None


def test_no_linked_unit_is_422_and_unknown_model_is_404() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "pin-errors")
        assert client.post(_pin_url(ctx, "absent"), json=_body(ctx, client)).status_code == 404
        plain = next(item for item in client.get(ctx["base"]).json() if item["id"] == ctx["plain_id"])
        body = _body(ctx, client) | {"expected_revision": plain["revision"]}
        assert client.post(f"{ctx['base']}/{plain['id']}/reduced-models/reduced-v1/process-pin", json=body).status_code == 422
        assert client.get(f"{ctx['base']}/{plain['id']}/reduced-models").json() == []


def _evaluate(ctx: dict[str, Any]) -> dict[str, Any]:
    document = draft.apply_ops(draft.empty_document("pbr"), [
        *pbr_ops(), SetUnitParams(op="set_unit_params", unit="pbr", values=pbr_quantities(tube_inner_diameter=(DIAMETER, "m")))])
    unit = document["objects"]["pbr"] | {"model": pin_of(_card(ctx["project_id"], "Pin card")),
                                          "hydraulics_model": _unit_hydraulics(ctx)}
    return pbr_unit.evaluate_pbr(unit, _inlet(), _context(ctx["project_id"])).result


def test_evaluation_flags_an_obsolete_pin_but_still_uses_it() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "pin-eval")
        pinned = client.post(_pin_url(ctx), json=_body(ctx, client))
        assert pinned.status_code == 200, pinned.text
        current = _evaluate(ctx)
        assert not any(item["code"] == "PBR_HYDRAULICS_MODEL_OBSOLETE" for item in current["findings"])
        assert current["hydraulics_model"]["obsolete_reasons"] == []
        _put(client, ctx, "geometry", candidate_id="tube-b")
        stale = _evaluate(ctx)
        (finding,) = [item for item in stale["findings"] if item["code"] == "PBR_HYDRAULICS_MODEL_OBSOLETE"]
        assert finding["message"].startswith("Reduced model obsolete, rerun the study: ")
        assert "geometry changed since the study" in finding["message"]
        assert stale["reported"]["pressure_drop"]["value"] == current["reported"]["pressure_drop"]["value"] is not None
        assert any("obsolete" in caveat for caveat in stale["caveats"])
        assert client.get(ctx["path"]).json()[0]["process_unit_pinned"] is True
