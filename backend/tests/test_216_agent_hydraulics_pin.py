"""Spec 216 contract 9: the agent pins a CFD reduced model through the same typed owner as the GUI."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from app.core.database import initialize_database, open_sqlite_connection
from app.main import app
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddUnit
from app.modules.workspace_actions.models import ActionOrigin, ActionRequest, SurfaceRef
from app.modules.workspace_actions.service import apply, submit, surface_brief
from tests.test_216_process_pin_from_design import _put, _setup, _unit_hydraulics, water  # noqa: F401

ORIGIN = ActionOrigin(kind="local", thread_id="thread-216", interaction_id="interaction-216")


def _request(ctx: dict[str, Any], **action: Any) -> ActionRequest:
    return ActionRequest.model_validate({
        "surface": "process", "draft_id": ctx["draft_id"],
        "base_revision": draft.projection(ctx["project_id"], ctx["draft_id"])["revision"],
        "actions": [{"op": "set_unit_hydraulics", "unit": "pbr", **action}]})


def _events(kind: str = "DesignComponentProcessHydraulicsPinned") -> list[dict[str, Any]]:
    with open_sqlite_connection() as connection:
        return [{"actor": row["actor"], **json.loads(row["payload"])} for row in connection.execute(
            "SELECT actor, payload FROM events WHERE event_type = ?", (kind,)).fetchall()]


def test_agent_proposal_is_confirm_tier_and_apply_pins_with_agent_history() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "agent-pin")
        proposal = submit(ctx["project_id"], _request(ctx, reduced_model="reduced-v1"), ORIGIN)
        assert proposal.state == "proposed" and proposal.tier == "confirm", proposal.reason
        assert proposal.summary == "Pin CFD reduced model reduced-v1 on pbr"
        assert any(line.label.endswith("hydraulics model") and line.before == "literature correlation"
                   for line in proposal.changes)
        assert _unit_hydraulics(ctx) is None and _events() == []
        applied = apply(ctx["project_id"], proposal.action_id)
        assert applied.state == "applied", applied.reason
        pin = _unit_hydraulics(ctx)
        assert pin["studio_version_id"] == "reduced-v1" and pin["out_of_range"] == "refuse"
        (event,) = _events()
        assert event["actor"].startswith("agent:local:thread-216") and event["unit_id"] == "pbr"
        assert event["model_version_id"] == "reduced-v1" and event["draft_id"] == ctx["draft_id"]


def test_obsolete_model_is_refused_at_proposal_and_at_apply() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "agent-obsolete")
        proposal = submit(ctx["project_id"], _request(ctx, reduced_model="reduced-v1"), ORIGIN)
        assert proposal.state == "proposed"
        _put(client, ctx, "geometry", candidate_id="tube-b")
        revision = draft.projection(ctx["project_id"], ctx["draft_id"])["revision"]
        applied = apply(ctx["project_id"], proposal.action_id)
        assert applied.state in {"refused", "stale"} and "not current" in (applied.reason or "")
        assert _unit_hydraulics(ctx) is None and _events() == []
        assert draft.projection(ctx["project_id"], ctx["draft_id"])["revision"] == revision
        again = submit(ctx["project_id"], _request(ctx, reduced_model="reduced-v1"), ORIGIN)
        assert again.state == "refused" and "not current" in (again.reason or "")


def test_unattached_model_and_non_pbr_target_are_refused() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "agent-refuse")
        missing = submit(ctx["project_id"], _request(ctx, reduced_model="absent"), ORIGIN)
        assert missing.state == "refused" and "not pinned on this component" in (missing.reason or "")
        head = draft.projection(ctx["project_id"], ctx["draft_id"])["revision"]
        draft.patch(ctx["project_id"], ctx["draft_id"], head, [AddUnit(op="add_unit", id="u1", type="Pump", tag="P1", x=0, y=0)])
        wrong = submit(ctx["project_id"], _request(ctx, unit="P1", reduced_model="reduced-v1"), ORIGIN)
        assert wrong.state == "refused" and "only a Photobioreactor" in (wrong.reason or "")
        assert _unit_hydraulics(ctx) is None


def test_reduced_model_none_returns_the_unit_to_the_literature_correlation() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "agent-revert")
        pinned = apply(ctx["project_id"], submit(ctx["project_id"], _request(ctx, reduced_model="reduced-v1"), ORIGIN).action_id)
        assert pinned.state == "applied" and _unit_hydraulics(ctx) is not None
        proposal = submit(ctx["project_id"], _request(ctx, reduced_model=None), ORIGIN)
        assert proposal.state == "proposed" and proposal.tier == "confirm"
        assert proposal.summary == "Return pbr to the literature correlation"
        assert apply(ctx["project_id"], proposal.action_id).state == "applied"
        assert _unit_hydraulics(ctx) is None
        assert len(_events()) == 1  # reverting records no pin event


def test_brief_lists_the_action_and_the_hydraulics_fidelity() -> None:
    initialize_database()
    with TestClient(app) as client:
        ctx = _setup(client, "agent-brief")
        ref = SurfaceRef(route_id="design-process", draft_id=ctx["draft_id"])
        brief = surface_brief(ctx["project_id"], ref)
        assert "set_unit_hydraulics" in brief.actions and "hydraulics literature correlation" in brief.text
        assert len(brief.text) <= 6000
        apply(ctx["project_id"], submit(ctx["project_id"], _request(ctx, reduced_model="reduced-v1"), ORIGIN).action_id)
        brief = surface_brief(ctx["project_id"], ref)
        assert "CFD reduced model" in brief.text and "(studio version reduced-v1)" in brief.text
        assert len(brief.text) <= 6000
