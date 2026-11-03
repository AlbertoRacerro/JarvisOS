from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.database import initialize_database, open_sqlite_connection
from app.main import app
from app.modules.bluecad.ledger import get_candidate
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddStream, AddUnit
from app.modules.workspace_actions.models import ActionOrigin, ActionRequest
from app.modules.workspace_actions.service import _load_candidate_spec, submit


def _workspace(client: TestClient) -> str:
    response = client.post("/workspaces", json={"name": "Actions", "slug": "workspace-actions"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _draft(workspace_id: str) -> dict:
    created = draft.create_draft(workspace_id, "Action test")
    return draft.patch(
        workspace_id,
        created["draft_id"],
        created["revision"],
        [
            AddStream(op="add_stream", id="s1", tag="S1", x=0, y=0),
            AddUnit(op="add_unit", id="u1", type="Pump", tag="P1", x=100, y=0),
        ],
    )


def test_process_propose_apply_undo_and_refuse() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        state = _draft(workspace_id)
        origin = ActionOrigin(kind="local", thread_id="thread-1", interaction_id="interaction-1")
        request = ActionRequest.model_validate(
            {
                "surface": "process",
                "base_revision": state["revision"],
                "draft_id": state["draft_id"],
                "actions": [
                    {"op": "set_value", "target": "S1", "property": "pressure", "value": {"value": 2, "unit": "bar"}},
                    {"op": "set_value", "target": "S1", "property": "pressure", "value": {"value": 2, "unit": "bar"}},
                ],
            }
        )
        proposal = submit(workspace_id, request, origin)
        assert proposal.state == "proposed", proposal.reason
        assert proposal.summary == "Set S1 pressure to 2 bar"
        assert len(proposal.changes) == 1
        assert len(proposal.request["actions"]) == 1
        from app.modules.workspace_actions.service import apply, undo

        applied = apply(workspace_id, proposal.action_id)
        assert applied.state == "applied" and applied.undo_available
        projection = draft.projection(workspace_id, state["draft_id"])
        assert projection["objects"][0]["spec"]["pressure"]["si"] == 200000
        undone = undo(workspace_id, applied.action_id)
        assert undone.state == "undone"
        assert draft.projection(workspace_id, state["draft_id"])["revision"] != state["revision"]

        current = draft.projection(workspace_id, state["draft_id"])
        invalid = ActionRequest.model_validate(
            {
                "surface": "process",
                "base_revision": current["revision"],
                "draft_id": state["draft_id"],
                "actions": [
                    {
                        "op": "set_value",
                        "target": "P1",
                        "property": "kinetics",
                        "value": {"value": 1, "unit": "dimensionless"},
                    }
                ],
            }
        )
        refused = submit(workspace_id, invalid, origin)
        assert refused.state == "refused"
        assert "kinetics is unsupported" in (refused.reason or "")


def test_identical_local_request_in_one_turn_is_idempotent() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        state = _draft(workspace_id)
        request = ActionRequest.model_validate(
            {
                "surface": "process",
                "base_revision": state["revision"],
                "draft_id": state["draft_id"],
                "actions": [{"op": "set_value", "target": "S1", "property": "pressure", "value": {"value": 3, "unit": "bar"}}],
            }
        )
        turn = ActionOrigin(kind="local", thread_id="thread-dup", interaction_id="interaction-dup")
        first = submit(workspace_id, request, turn)
        repeated = submit(workspace_id, request, turn)
        assert first.state == "proposed" and repeated.action_id == first.action_id
        from app.modules.workspace_actions.service import apply, find_local_duplicate, list_for

        assert [item.action_id for item in list_for(workspace_id, thread_id="thread-dup")] == [first.action_id]
        applied = apply(workspace_id, first.action_id)
        again = find_local_duplicate(workspace_id, "interaction-dup", request)
        assert again is not None and again.state == applied.state == "applied"
        next_turn = ActionOrigin(kind="local", thread_id="thread-dup", interaction_id="interaction-dup-2")
        later = submit(workspace_id, request, next_turn)
        assert later.action_id != first.action_id


def test_brief_and_http_actions_routes() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        state = _draft(workspace_id)
        brief = client.post(
            f"/workspaces/{workspace_id}/actions/brief",
            json={
                "route_id": "design-process",
                "draft_id": state["draft_id"],
                "process_selection": [{"kind": "stream", "tag": "S1"}],
            },
        )
        assert brief.status_code == 200, brief.text
        payload = brief.json()
        assert payload["surface"] == "process"
        assert payload["selected"][0]["id"] == "s1"
        assert payload["summary"].startswith("Process: Action test · revision ")
        assert "selected: Stream S1" in payload["summary"]
        assert state["draft_id"] not in payload["summary"]
        assert "set_reaction" in payload["text"] and "one typed reaction per PFR/CSTR" in payload["text"]
        assert '"target":"S1"' in payload["text"]
        assert '"target":"P1"' in payload["text"]
        assert '"after":"P1"' in payload["text"]
        assert '"target":"tube"' not in payload["text"]
        off_surface = client.post(f"/workspaces/{workspace_id}/actions/brief", json={"route_id": "home"})
        assert off_surface.json()["surface"] == "none"
        request = ActionRequest.model_validate(
            {
                "surface": "process",
                "base_revision": state["revision"],
                "draft_id": state["draft_id"],
                "actions": [
                    {"op": "set_value", "target": "S1", "property": "pressure", "value": {"value": 2, "unit": "bar"}}
                ],
            }
        )
        proposal = submit(
            workspace_id,
            request,
            ActionOrigin(kind="local", thread_id="thread-http", interaction_id="interaction-http"),
        )
        applied = client.post(f"/workspaces/{workspace_id}/actions/{proposal.action_id}/apply")
        assert applied.status_code == 200, applied.text
        assert applied.json()["state"] == "applied"
        fetched = client.get(f"/workspaces/{workspace_id}/actions/{proposal.action_id}")
        assert fetched.status_code == 200 and fetched.json()["state"] == "applied"
        listed = client.get(f"/workspaces/{workspace_id}/actions", params={"thread_id": "thread-http"})
        assert listed.status_code == 200 and len(listed.json()["actions"]) == 1

        current = draft.projection(workspace_id, state["draft_id"])
        dismiss_request = ActionRequest.model_validate(
            {"surface": "process", "base_revision": current["revision"], "draft_id": state["draft_id"],
             "actions": [{"op": "set_value", "target": "S1", "property": "pressure", "value": {"value": 3, "unit": "bar"}}]}
        )
        pending = submit(
            workspace_id, dismiss_request,
            ActionOrigin(kind="local", thread_id="thread-dismiss", interaction_id="interaction-dismiss"),
        )
        dismissed = client.post(f"/workspaces/{workspace_id}/actions/{pending.action_id}/dismiss")
        assert dismissed.status_code == 200 and dismissed.json()["state"] == "dismissed"

        current = draft.projection(workspace_id, state["draft_id"])
        move_request = ActionRequest.model_validate(
            {"surface": "process", "base_revision": current["revision"], "draft_id": state["draft_id"],
             "actions": [{"op": "move", "target": "P1", "dx": 5, "dy": 0}]}
        )
        moved = submit(
            workspace_id, move_request,
            ActionOrigin(kind="local", thread_id="thread-undo", interaction_id="interaction-undo"),
        )
        undone = client.post(f"/workspaces/{workspace_id}/actions/{moved.action_id}/undo")
        assert undone.status_code == 200 and undone.json()["state"] == "undone"


def test_layout_tier_and_stale_apply() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        state = _draft(workspace_id)
        local = ActionOrigin(kind="local", thread_id="thread-layout", interaction_id="interaction-layout")
        move = ActionRequest.model_validate(
            {
                "surface": "process",
                "base_revision": state["revision"],
                "draft_id": state["draft_id"],
                "actions": [{"op": "move", "target": "P1", "dx": 10, "dy": 0}],
            }
        )
        immediate = submit(workspace_id, move, local)
        assert immediate.state == "applied" and immediate.tier == "immediate"

        current = draft.projection(workspace_id, state["draft_id"])
        proposal_request = ActionRequest.model_validate(
            {
                "surface": "process",
                "base_revision": current["revision"],
                "draft_id": state["draft_id"],
                "actions": [
                    {"op": "set_value", "target": "S1", "property": "pressure", "value": {"value": 3, "unit": "bar"}}
                ],
            }
        )
        relay = ActionOrigin(kind="relay", thread_id="thread-relay", relay_run_id="relay-1")
        proposal = submit(workspace_id, proposal_request, relay)
        assert proposal.state == "proposed" and proposal.tier == "confirm"
        draft.patch(workspace_id, state["draft_id"], current["revision"], [draft.Move(op="move", id="u1", x=120, y=0)])
        from app.modules.workspace_actions.service import apply

        stale = apply(workspace_id, proposal.action_id)
        assert stale.state == "stale"


def test_identical_draft_revisions_require_draft_binding() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        first = draft.create_draft(workspace_id, "Same name")
        second = draft.create_draft(workspace_id, "Same name")
        assert first["revision"] == second["revision"]
        origin = ActionOrigin(kind="local", thread_id="thread-collision", interaction_id="interaction-collision")
        action = [{"op": "add_unit", "type": "Pump", "tag": "P1"}]
        ambiguous = ActionRequest.model_validate(
            {"surface": "process", "base_revision": first["revision"], "actions": action}
        )
        assert submit(workspace_id, ambiguous, origin).state == "stale"
        bound = ActionRequest.model_validate(
            {
                "surface": "process",
                "base_revision": second["revision"],
                "draft_id": second["draft_id"],
                "actions": action,
            }
        )
        proposal = submit(workspace_id, bound, origin)
        assert proposal.state == "proposed" and proposal.draft_id == second["draft_id"]


def test_process_insert_connect_and_delete_validation() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        created = draft.create_draft(workspace_id, "Topology actions")
        state = draft.patch(
            workspace_id,
            created["draft_id"],
            created["revision"],
            [
                AddUnit(op="add_unit", id="u1", type="Pump", tag="P1", x=0, y=0),
                AddUnit(op="add_unit", id="u2", type="Pump", tag="P2", x=300, y=0),
                AddStream(op="add_stream", id="s1", tag="S1", x=150, y=0),
                draft.Connect(op="connect", stream="s1", end="source", unit="u1", port=0),
                draft.Connect(op="connect", stream="s1", end="target", unit="u2", port=0),
            ],
        )
        origin = ActionOrigin(kind="local", thread_id="thread-topology", interaction_id="interaction-topology")
        insert = ActionRequest.model_validate(
            {
                "surface": "process",
                "base_revision": state["revision"],
                "draft_id": state["draft_id"],
                "actions": [{"op": "insert_unit_after", "type": "Valve", "after": "P1"}],
            }
        )
        proposal = submit(workspace_id, insert, origin)
        assert proposal.state == "proposed", proposal.reason
        assert proposal.summary == "Add Valve Valve_1 after P1"
        from app.modules.workspace_actions.service import apply

        applied = apply(workspace_id, proposal.action_id)
        assert applied.state == "applied", applied.reason
        objects = draft.projection(workspace_id, state["draft_id"])["objects"]
        valve = next(item for item in objects if item["tag"] == "Valve_1")
        assert valve["type"] == "Valve"

        current = draft.projection(workspace_id, state["draft_id"])
        connect = ActionRequest.model_validate(
            {
                "surface": "process",
                "base_revision": current["revision"],
                "draft_id": state["draft_id"],
                "actions": [{"op": "connect", "from": "P1", "to": "P2"}],
            }
        )
        refused = submit(workspace_id, connect, origin)
        assert refused.state == "refused"
        assert "already connected" in (refused.reason or "")
        delete = ActionRequest.model_validate(
            {
                "surface": "process",
                "base_revision": current["revision"],
                "draft_id": state["draft_id"],
                "actions": [{"op": "delete", "target": "P1"}],
            }
        )
        orphaned = submit(workspace_id, delete, origin)
        assert orphaned.state == "refused"
        assert "Delete would leave required Process data invalid" in (orphaned.reason or "")


def test_mixed_surface_action_request_is_a_typed_validation_error() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="All actions must match the bluecad surface"):
        ActionRequest.model_validate({
            "surface": "bluecad", "base_revision": "candidate-1",
            "actions": [{"op": "set_value", "target": "S1", "property": "pressure",
                         "value": {"value": 2, "unit": "bar"}}],
        })


def test_auto_tags_are_unique_within_a_multi_action_request() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        created = draft.create_draft(workspace_id, "Multiple action tags")
        state = draft.patch(workspace_id, created["draft_id"], created["revision"], [
            AddUnit(op="add_unit", id="u1", type="Pump", tag="P1", x=0, y=0),
            AddUnit(op="add_unit", id="u2", type="Pump", tag="P2", x=200, y=0),
            AddUnit(op="add_unit", id="u3", type="Pump", tag="P3", x=400, y=0),
            AddUnit(op="add_unit", id="u4", type="Pump", tag="P4", x=600, y=0),
        ])
        origin = ActionOrigin(kind="relay", thread_id="multi-tag", relay_run_id="multi-tag-run")
        add_units = ActionRequest.model_validate({
            "surface": "process", "base_revision": state["revision"], "draft_id": state["draft_id"],
            "actions": [{"op": "add_unit", "type": "Valve"}, {"op": "add_unit", "type": "Valve"}],
        })
        proposal = submit(workspace_id, add_units, origin)
        assert proposal.state == "proposed", proposal.reason
        assert [item.label.split(" — ", 1)[0] for item in proposal.changes] == ["Valve_1", "Valve_2"]
        assert "Add Valve Valve_1" in proposal.summary and "Add Valve Valve_2" in proposal.summary
        explicit_conflict = ActionRequest.model_validate({
            "surface": "process", "base_revision": state["revision"], "draft_id": state["draft_id"],
            "actions": [{"op": "add_unit", "type": "Valve", "tag": "P1"}],
        })
        assert submit(workspace_id, explicit_conflict, origin).state == "refused"
        connections = ActionRequest.model_validate({
            "surface": "process", "base_revision": state["revision"], "draft_id": state["draft_id"],
            "actions": [{"op": "connect", "from": "P1", "to": "P2"},
                        {"op": "connect", "from": "P3", "to": "P4"}],
        })
        connected = submit(workspace_id, connections, origin)
        assert connected.state == "proposed", connected.reason
        assert [item.label.split(" — ", 1)[0] for item in connected.changes] == ["S_auto1", "S_auto2"]


def test_concurrent_bluecad_apply_claims_once(monkeypatch) -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        template = client.post(
            f"/workspaces/{workspace_id}/bluecad/candidates/from-template",
            json={"template": "tube", "params": {"outer_d_mm": 50, "wall_t_mm": 3, "length_mm": 1000}},
        )
        assert template.status_code == 201, template.text
        base_id = template.json()["id"]
        request = ActionRequest.model_validate({
            "surface": "bluecad", "base_revision": base_id,
            "actions": [{"op": "duplicate_part", "part": "template_tube"}],
        })
        proposal = submit(workspace_id, request, ActionOrigin(kind="relay", thread_id="race", relay_run_id="race-run"))
        assert proposal.state == "proposed"
        from app.modules.workspace_actions import bluecad_actions
        from app.modules.workspace_actions.service import ActionError, apply

        original = bluecad_actions.execute_bluecad
        calls = 0
        calls_lock = threading.Lock()

        def counted(*args, **kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.15)
            return original(*args, **kwargs)

        monkeypatch.setattr(bluecad_actions, "execute_bluecad", counted)
        start = threading.Barrier(2)

        def attempt():
            start.wait()
            try:
                return apply(workspace_id, proposal.action_id)
            except ActionError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: attempt(), range(2)))
        assert calls == 1
        successes = [item for item in results if not isinstance(item, ActionError)]
        failures = [item for item in results if isinstance(item, ActionError)]
        assert len(successes) == len(failures) == 1
        assert successes[0].state == "applied" and successes[0].child_candidate_id
        assert failures[0].code == "action_not_proposed"


def test_bluecad_apply_failure_is_persisted_as_refused(monkeypatch) -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        template = client.post(
            f"/workspaces/{workspace_id}/bluecad/candidates/from-template",
            json={"template": "tube", "params": {"outer_d_mm": 50, "wall_t_mm": 3, "length_mm": 1000}},
        )
        assert template.status_code == 201, template.text
        proposal = submit(
            workspace_id,
            ActionRequest.model_validate({
                "surface": "bluecad", "base_revision": template.json()["id"],
                "actions": [{"op": "duplicate_part", "part": "template_tube"}],
            }),
            ActionOrigin(kind="relay", thread_id="apply-failure", relay_run_id="apply-failure-run"),
        )
        from app.modules.workspace_actions import bluecad_actions
        from app.modules.workspace_actions.service import apply, get

        def fail(*_args, **_kwargs):
            raise ValueError("candidate build failed")

        monkeypatch.setattr(bluecad_actions, "execute_bluecad", fail)
        outcome = apply(workspace_id, proposal.action_id)
        assert outcome.state == "refused" and outcome.reason_code == "build_failed"
        assert outcome.reason == "candidate build failed"
        assert get(workspace_id, proposal.action_id).state == "refused"


def test_bluecad_duplicate_builds_valid_child_candidate() -> None:
    initialize_database()
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        template = client.post(
            f"/workspaces/{workspace_id}/bluecad/candidates/from-template",
            json={"template": "tube", "params": {"outer_d_mm": 50, "wall_t_mm": 3, "length_mm": 1000}},
        )
        assert template.status_code == 201, template.text
        base = template.json()
        from app.modules.workspace_actions.models import SurfaceRef
        from app.modules.workspace_actions.service import surface_brief

        brief = surface_brief(
            workspace_id,
            SurfaceRef(route_id="design-bluecad", candidate_id=base["id"], bluecad_part_ids=["template_tube"]),
        )
        assert '"part":"template_tube"' in brief.text
        assert '"part":"tube"' not in brief.text
        assert brief.summary.startswith("BLUECAD: Template tube:")
        assert "selected: template_tube (tube_run)" in brief.summary
        assert base["id"] not in brief.summary
        request = ActionRequest.model_validate(
            {
                "surface": "bluecad",
                "base_revision": base["id"],
                "actions": [{"op": "duplicate_part", "part": "template_tube", "placement": "beside",
                             "gap_mm": 25}],
            }
        )
        result = submit(
            workspace_id,
            request,
            ActionOrigin(kind="local", thread_id="thread-bluecad", interaction_id="interaction-bluecad"),
        )
        assert result.state == "applied", result.reason
        assert result.summary == "Duplicate tube_run beside itself (new tube_run part, 25 mm gap)"
        assert len(result.changes) == 1
        assert len(result.request["actions"]) == 1
        assert result.child_candidate_id
        child = get_candidate(workspace_id, result.child_candidate_id)
        assert child is not None and child.status == "valid" and child.origin == "agent_action"
        assert child.parent_candidate_id == base["id"]
        spec = _load_candidate_spec(child.spec_artifact_id, workspace_id)
        assert [part["kind"] for part in spec["parts"]] == ["tube_run", "tube_run"]
        with open_sqlite_connection() as connection:
            manifest = connection.execute(
                "SELECT stored_path FROM artifacts WHERE workspace_id=? AND source_ref=? AND artifact_type='bluecad_manifest'",
                (workspace_id, f"bluecad_candidate:{child.id}:attempt:1"),
            ).fetchone()
        assert manifest is not None
        manifest_data = json.loads(Path(manifest["stored_path"]).read_text())
        first_box = manifest_data["parts"]["template_tube"]["bbox_mm"]
        second_box = manifest_data["parts"]["template_tube_2"]["bbox_mm"]
        assert second_box["min"][1] > first_box["max"][1]
        from app.modules.workspace_actions.service import undo

        undone = undo(workspace_id, result.action_id)
        assert undone.state == "undone"
        assert get_candidate(workspace_id, child.id).status == "archived"

        length_request = ActionRequest.model_validate(
            {
                "surface": "bluecad",
                "base_revision": base["id"],
                "actions": [
                    {"op": "set_part_param", "part": "template_tube", "param": "length", "value": 2, "unit": "m"}
                ],
            }
        )
        length_result = submit(
            workspace_id,
            length_request,
            ActionOrigin(kind="local", thread_id="thread-length", interaction_id="interaction-length"),
        )
        assert length_result.state == "applied", length_result.reason
        assert length_result.summary == "Set tube_run length to 2 m"
        length_child = get_candidate(workspace_id, length_result.child_candidate_id)
        length_spec = _load_candidate_spec(length_child.spec_artifact_id, workspace_id)
        assert length_spec["parts"][0]["params"]["length"] == 2000

        delete_last = ActionRequest.model_validate(
            {
                "surface": "bluecad",
                "base_revision": base["id"],
                "actions": [{"op": "delete_part", "part": "template_tube"}],
            }
        )
        refused = submit(
            workspace_id,
            delete_last,
            ActionOrigin(kind="local", thread_id="thread-delete", interaction_id="interaction-delete"),
        )
        assert refused.state == "refused"
        assert "last part" in (refused.reason or "")
