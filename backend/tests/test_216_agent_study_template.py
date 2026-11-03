"""Spec 216 contract 9: the agent proposes a CFD study template; only explicit Apply records it, via the GUI's typed write."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.database import initialize_database, open_sqlite_connection
from app.main import app
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddUnit
from app.modules.workspace_actions.models import ActionOrigin, ActionRequest, SurfaceRef
from app.modules.workspace_actions.service import apply, submit, surface_brief
from tests.test_216_process_pin_from_design import _put, _setup, water  # noqa: F401

ORIGIN = ActionOrigin(kind="local", thread_id="thread-216", interaction_id="interaction-216")
RELAY = ActionOrigin(kind="relay", thread_id="thread-216", relay_run_id="run-9")
INPUTS: dict[str, Any] = {"regime": "turbulent_rans", "inlet": {"mean_velocity_m_s": 0.2175},
                          "turbulence_model": "kOmegaSST", "wall_treatment": "low_re_resolved", "mesh_level": 1}


def _request(ctx: dict[str, Any], extra: list[dict] | None = None, **action: Any) -> ActionRequest:
    return ActionRequest.model_validate({
        "surface": "process", "draft_id": ctx["draft_id"],
        "base_revision": draft.projection(ctx["project_id"], ctx["draft_id"])["revision"],
        "actions": [{"op": "set_cfd_study_template", "unit": "pbr", **{**INPUTS, **action}}, *(extra or [])]})


def _rows(ctx: dict[str, Any]) -> list[Any]:
    with open_sqlite_connection() as connection:
        return connection.execute("SELECT * FROM design_component_study_templates WHERE component_id = ?",
                                  (ctx["component_id"],)).fetchall()


def _revision(client: TestClient, ctx: dict[str, Any]) -> int:
    return next(item for item in client.get(ctx["base"]).json() if item["id"] == ctx["component_id"])["revision"]


def _events() -> list[dict[str, Any]]:
    with open_sqlite_connection() as connection:
        return [{"actor": row["actor"], **json.loads(row["payload"])} for row in connection.execute(
            "SELECT actor, payload FROM events WHERE event_type = 'DesignComponentStudyTemplateSet'").fetchall()]


def test_proposal_writes_nothing_and_apply_records_the_template_as_the_agent() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "agent-tpl")
        revision = _revision(client, ctx)
        proposal = submit(ctx["project_id"], _request(ctx), ORIGIN)
        assert proposal.state == "proposed" and proposal.tier == "confirm", proposal.reason
        assert proposal.summary == "Set CFD turbulent_rans study template on pbr"
        line = proposal.changes[0]
        assert line.label == "PBR CFD study template (turbulent_rans)" and line.before == "none"
        assert "Re " in (line.after or "") and "mesh level 1" in (line.after or "") and "kOmegaSST" in (line.after or "")
        assert _rows(ctx) == [] and _events() == [] and _revision(client, ctx) == revision
        applied = apply(ctx["project_id"], proposal.action_id)
        assert applied.state == "applied" and applied.undo_available is False, applied.reason
        (row,) = _rows(ctx)
        assert row["updated_by"] == "agent:local:thread-216/interaction-216" and row["regime"] == "turbulent_rans"
        assert _revision(client, ctx) == revision + 1
        (event,) = _events()
        assert event["actor"] == "agent:local:thread-216/interaction-216" and event["regime"] == "turbulent_rans"
        assert event["template_digest"] == row["template_digest"]
        # the Process draft is untouched and a replacement shows the previous digest
        again = submit(ctx["project_id"], _request(ctx, mesh_level=2), RELAY)
        assert again.state == "proposed" and again.changes[0].before == row["template_digest"][:19]


def test_apply_is_stale_after_component_or_geometry_change() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "agent-tpl-stale")
        proposal = submit(ctx["project_id"], _request(ctx), ORIGIN)
        assert proposal.state == "proposed"
        client.put(f"{ctx['base']}/{ctx['component_id']}", json={
            "name": "Renamed", "expected_revision": _revision(client, ctx), "expected_active_version_id": ctx["project_id"]})
        stale = apply(ctx["project_id"], proposal.action_id)
        assert stale.state == "stale" and _rows(ctx) == [] and _events() == []
        second = submit(ctx["project_id"], _request(ctx), ORIGIN.model_copy(update={"interaction_id": "i-2"}))
        assert second.state == "proposed", second.reason
        _put(client, ctx, "geometry", candidate_id="tube-b")
        moved = apply(ctx["project_id"], second.action_id)
        assert moved.state == "stale" and _rows(ctx) == [] and _events() == []


def test_refusals_unlinked_unknown_geometry_invalid_inputs_and_combined_actions() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "agent-tpl-refuse")
        project = ctx["project_id"]
        laminar_model = submit(project, _request(ctx, regime="laminar", wall_treatment="resolved",
                                                 inlet={"mean_velocity_m_s": 0.00005}), ORIGIN)
        assert laminar_model.state == "refused" and "no turbulence model" in (laminar_model.reason or "")
        fast = submit(project, _request(ctx, regime="laminar", wall_treatment="resolved", turbulence_model=None,
                                        inlet={"mean_velocity_m_s": 1.0}), ORIGIN)
        assert fast.state == "refused" and "Re=" in (fast.reason or "")
        head = draft.projection(project, ctx["draft_id"])["revision"]
        draft.patch(project, ctx["draft_id"], head, [AddUnit(op="add_unit", id="u1", type="Pump", tag="P1", x=0, y=0)])
        unlinked = submit(project, _request(ctx, unit="P1"), ORIGIN)
        assert unlinked.state == "refused" and "no linked Design component" in (unlinked.reason or "")
        with open_sqlite_connection() as connection:
            connection.execute("DELETE FROM design_component_geometry WHERE component_id = ?", (ctx["component_id"],))
            connection.commit()
        unknown = submit(project, _request(ctx), ORIGIN)
        assert unknown.state == "refused" and "geometry is unknown" in (unknown.reason or "")
        assert _rows(ctx) == [] and _events() == []


def test_must_be_the_only_action_and_domain_is_never_agent_supplied() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "agent-tpl-schema")
        with pytest.raises(ValidationError, match="only action"):
            _request(ctx, extra=[{"op": "move", "target": "pbr", "dx": 1, "dy": 0}])
        with pytest.raises(ValidationError):
            _request(ctx, domain={"length_m": 1.0})


def test_brief_documents_the_operation_within_limits() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "agent-tpl-brief")
        brief = surface_brief(ctx["project_id"], SurfaceRef(route_id="design-process", draft_id=ctx["draft_id"]))
        assert "set_cfd_study_template" in brief.actions and "set_cfd_study_template" in brief.text
        assert len(brief.text) <= 6000
