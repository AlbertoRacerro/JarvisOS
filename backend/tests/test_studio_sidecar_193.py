"""Spec 193: inert Studio proposals through the governed workspace-action owner."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.ai.thread_service import _turn_tools
from app.modules.workspace_actions import service
from app.modules.workspace_actions.models import ActionOrigin, ActionRequest, SurfaceRef


@pytest.fixture
def client():
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client


INPUT = {"name": "flow", "label": "Flow", "unit": "m**3/s", "description": "Volume flow"}
OUTPUT = {"name": "loss", "label": "Loss", "unit": "Pa", "description": "Pressure loss"}
SOURCE = "def main(inputs: dict) -> dict:\n    return {'loss': inputs['flow'] * 3}\n"


def action(category="calculation", source=SOURCE, **changes):
    value = {"op": "studio_draft", "mode": "create", "category": category,
             "name": "Pressure model", "engineering_question": "What is the pressure loss?",
             "contract": {"inputs": [INPUT], "outputs": [OUTPUT]}, "source": source,
             "assumptions": "Steady flow"}
    value.update(changes)
    return value


def submit(action_value, *, revision="new", draft_id=None, workspace="main", interaction="turn-1",
           kind="local"):
    request = ActionRequest(surface="studio", base_revision=revision, draft_id=draft_id,
                            actions=[action_value])
    origin = ActionOrigin(kind=kind, thread_id="thread-193", interaction_id=interaction if kind == "local" else None,
                          relay_run_id=interaction if kind == "relay" else None)
    return service.submit(workspace, request, origin)


def test_create_is_inert_and_requires_explicit_apply(client, tmp_path):
    canary = tmp_path / "executed"
    source = (f"from pathlib import Path\nPath({str(canary)!r}).write_text('bad')\n"
              "def evaluate(x):\n    return x * 3\n" + SOURCE)
    brief = service.surface_brief("main", SurfaceRef(route_id="design-studio"))
    assert brief.surface == "studio" and brief.base_revision == "new"
    proposed = submit(action(source=source))
    assert proposed.state == "proposed" and proposed.tier == "confirm"
    assert not canary.exists()
    assert next(line for line in proposed.changes if line.label == "Source").after == source
    assert client.get("/workspaces/main/studio/models").json() == []
    applied = client.post(f"/workspaces/main/actions/{proposed.action_id}/apply")
    assert applied.status_code == 200, applied.text
    model_id = applied.json()["draft_id"]
    model = client.get(f"/workspaces/main/studio/models/{model_id}").json()
    assert model["source"] == source and model["status"] == "draft"
    assert model["last_test_report"] is None and model["versions"] == []
    assert not canary.exists()
    assert client.post(f"/workspaces/main/actions/{proposed.action_id}/apply").status_code == 409
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM studio_drafts WHERE model_spec_id=?", (model_id,)).fetchone()[0] == 1


def test_update_cas_and_test_evidence_invalidation(client):
    created = submit(action(), interaction="create")
    applied = service.apply("main", created.action_id)
    model_id = applied.draft_id
    assert model_id
    model = client.get(f"/workspaces/main/studio/models/{model_id}").json()
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE studio_drafts SET test_report_json=? WHERE model_spec_id=?",
                           ('{"passed":true,"source_sha256":"old","contract_sha256":"old","cases":[{}]}', model_id))
        connection.commit()
    selected = service.surface_brief("main", SurfaceRef(route_id="design-studio", studio_model_id=model_id))
    assert selected.base_revision and selected.draft_id == model_id
    # The model sees the exact revision to echo, never a content hash it could mistake for one.
    assert f"base_revision={selected.base_revision}" in selected.text
    assert model["source_sha256"] not in selected.text and model["contract_sha256"] not in selected.text
    changed_source = SOURCE.replace("* 3", "* 4")
    update = action(source=changed_source, mode="update")
    proposed = submit(update, revision=selected.base_revision, draft_id=model_id, interaction="update")
    assert proposed.state == "proposed"
    source_diff = next(line for line in proposed.changes if line.label == "Source")
    assert source_diff.before == SOURCE and source_diff.after == changed_source
    assert service.apply("main", proposed.action_id).state == "applied"
    changed = client.get(f"/workspaces/main/studio/models/{model_id}").json()
    assert changed["source"] == changed_source and changed["last_test_report"] is None
    assert len(client.get("/workspaces/main/studio/models").json()) == 1
    assert model["source_sha256"] != changed["source_sha256"]


def test_stale_foreign_and_relay_proposals_cannot_mutate(client):
    created = service.apply("main", submit(action(), interaction="create").action_id)
    model_id = created.draft_id
    assert model_id
    selected = service.surface_brief("main", SurfaceRef(route_id="design-studio", studio_model_id=model_id))
    update = action(mode="update", source=SOURCE.replace("* 3", "* 5"))
    relay = submit(update, revision=selected.base_revision, draft_id=model_id, interaction="relay-1", kind="relay")
    assert relay.state == "proposed"
    other = client.post("/workspaces", json={"name": "Other studio", "slug": "other-studio-193"})
    assert other.status_code == 201
    foreign = submit(update, revision=selected.base_revision, draft_id=model_id,
                     workspace=other.json()["id"], interaction="foreign")
    assert foreign.state in {"refused", "stale"}
    client.put(f"/workspaces/main/studio/models/{model_id}", json={"source": SOURCE + "# operator edit\n"})
    assert client.post(f"/workspaces/main/actions/{relay.action_id}/apply").status_code == 409
    assert client.get(f"/workspaces/main/studio/models/{model_id}").json()["source"].endswith("# operator edit\n")


def test_invalid_contract_source_and_duplicate_local_turn(client):
    invalid = submit(action(source="def main(: pass"), interaction="syntax")
    assert invalid.state == "refused" and invalid.reason_code == "studio_source_invalid"
    invalid_unit = submit(action(**{"contract": {"inputs": [{**INPUT, "unit": "not_a_unit_xyz"}],
                                                  "outputs": [OUTPUT]}}), interaction="unit")
    assert invalid_unit.state == "refused"
    with pytest.raises(ValidationError):
        ActionRequest(surface="studio", base_revision="new", actions=[action(source="x" * 65537)])
    first = submit(action(), interaction="same-turn")
    duplicate = submit(action(), interaction="same-turn")
    assert duplicate.action_id == first.action_id
    assert client.get("/workspaces/main/studio/models").json() == []


def test_documentation_preview_parses_without_execution(client, tmp_path):
    canary = Path(tmp_path / "preview-executed")
    source = (f"from pathlib import Path\nPath({str(canary)!r}).write_text('bad')\n"
              "def evaluate(x):\n    return x * 3\n" + SOURCE)
    result = client.post("/studio/documentation/preview", json={"source": source})
    assert result.status_code == 200 and result.json()["status"] == "ok"
    assert not canary.exists()
    assert client.post("/studio/documentation/preview", json={"source": "x" * 65537}).status_code == 422
    assert "mcp__jarvis__jarvis_studio_act" in _turn_tools("studio")
    assert "mcp__jarvis__jarvis_studio_act" not in _turn_tools("process")


def _studio_rows(workspace_id="main"):
    with open_sqlite_connection() as connection:
        drafts = connection.execute("SELECT COUNT(*) FROM studio_drafts WHERE workspace_id=?",
                                    (workspace_id,)).fetchone()[0]
        specs = connection.execute("SELECT COUNT(*) FROM model_specs WHERE workspace_id=?",
                                   (workspace_id,)).fetchone()[0]
    return drafts, specs


GEOMETRY = {"inputs": [{"name": "pitch", "label": "Baffle pitch", "unit": "mm", "description": "Helix pitch",
                        "min": 5, "max": 500}]}
GEOMETRY_SOURCE = ("import build123d as bd\n\ndef build(pitch: float) -> bd.Part:\n"
                   "    return bd.Box(10, 10, pitch)\n\ndef main(inputs: dict) -> bd.Part:\n"
                   "    return build(inputs['pitch'])\n")
UNIT = {"inlets": [{"name": "feed", "label": "Feed"}], "outlets": [{"name": "product", "label": "Product"}],
        "parameters": [{"name": "dose", "label": "Nutrient dose", "unit": "kg/s", "description": "Dose rate",
                        "min": 0}],
        "culture_generation": []}
UNIT_SOURCE = "def main(inputs: dict) -> dict:\n    return {'outlets': {'product': inputs['inlets']['feed']}}\n"


def test_geometry_and_process_unit_proposals_validate_with_category_models(client):
    geometry = submit(action("geometry", source=GEOMETRY_SOURCE, contract=GEOMETRY), interaction="geo")
    unit = submit(action("process_unit", source=UNIT_SOURCE, contract=UNIT), interaction="unit")
    assert geometry.state == "proposed", geometry.reason
    assert unit.state == "proposed", unit.reason
    # A geometry contract is not a calculation contract: outputs are refused by the owner model.
    wrong = submit(action("geometry", source=GEOMETRY_SOURCE), interaction="geo-wrong")
    assert wrong.state == "refused"
    for model_id in (service.apply("main", geometry.action_id).draft_id,
                     service.apply("main", unit.action_id).draft_id):
        model = client.get(f"/workspaces/main/studio/models/{model_id}").json()
        assert model["status"] == "draft" and model["versions"] == [] and model["last_test_report"] is None
    categories = {model["category"] for model in client.get("/workspaces/main/studio/models").json()}
    assert categories == {"geometry", "process_unit"}


@pytest.mark.parametrize("bad", [
    {"category": "procedure"},
    {"category": "notebook"},
    {"op": "publish"},
    {"runtime_profile": "gpu"},
    {"status": "published"},
])
def test_malformed_category_and_model_chosen_authority_fields_are_rejected(bad):
    with pytest.raises(ValidationError):
        ActionRequest(surface="studio", base_revision="new", actions=[action(**bad)])


@pytest.mark.parametrize("contract", [
    {"inputs": [INPUT], "outputs": [OUTPUT], "category": "geometry"},
    {"inputs": [INPUT], "outputs": [OUTPUT], "name": "Renamed"},
    {"inputs": [INPUT], "outputs": [OUTPUT], "provider": "external:scaleway"},
    {"inputs": [INPUT], "outputs": [OUTPUT], "status": "published"},
    {"inputs": [{**INPUT, "unit": "m'; import os"}], "outputs": [OUTPUT]},
    {"inputs": [{**INPUT, "min": 5, "max": 1}], "outputs": [OUTPUT]},
    {"inputs": [{**INPUT, "name": "import"}], "outputs": [OUTPUT]},
])
def test_contract_cannot_smuggle_identity_authority_or_invalid_units(client, contract):
    outcome = submit(action(contract=contract), interaction=f"bad-{abs(hash(str(contract)))}")
    assert outcome.state == "refused" and outcome.tier == "none"
    assert _studio_rows()[0] == 0


@pytest.mark.parametrize("source", ["def helper():\n    return 1\n", "def main(:\n", "x = 1\x00"])
def test_source_without_main_or_invalid_is_refused(client, source):
    outcome = submit(action(source=source), interaction=f"src-{abs(hash(source))}")
    assert outcome.state == "refused" and outcome.reason_code == "studio_source_invalid"


def test_oversized_source_is_refused_before_any_storage(client):
    from app.modules.studio import service as studio
    from app.modules.studio.models import StudioError

    with pytest.raises(ValidationError):
        ActionRequest(surface="studio", base_revision="new", actions=[action(source="#" * 65537)])
    with pytest.raises(StudioError):
        studio.validate_proposed_source("calculation", "x", "q", {"inputs": [INPUT], "outputs": [OUTPUT]},
                                        SOURCE + "#" * 65536)
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM workspace_actions WHERE surface='studio'").fetchone()[0] == 0


def test_foreign_workspace_cannot_apply_or_read_draft(client):
    created = service.apply("main", submit(action(), interaction="create").action_id)
    other = client.post("/workspaces", json={"name": "Foreign studio", "slug": "foreign-studio-193"}).json()["id"]
    foreign_brief = service.surface_brief(other, SurfaceRef(route_id="design-studio", studio_model_id=created.draft_id))
    assert foreign_brief.surface == "none" and "Pressure model" not in foreign_brief.text
    revision = service.surface_brief("main", SurfaceRef(route_id="design-studio",
                                                            studio_model_id=created.draft_id)).base_revision
    foreign = submit(action(mode="update"), revision=revision, draft_id=created.draft_id, workspace=other,
                     interaction="foreign")
    assert foreign.state == "refused" and foreign.reason_code == "studio_model_not_found"
    assert client.post(f"/workspaces/{other}/actions/{created.action_id}/apply").status_code == 404
    assert _studio_rows(other)[0] == 0


def test_one_apply_is_exactly_one_draft_mutation_and_stale_apply_writes_nothing(client):
    before = _studio_rows()
    proposed = submit(action(), interaction="one")
    assert _studio_rows() == before
    applied = client.post(f"/workspaces/main/actions/{proposed.action_id}/apply")
    assert applied.status_code == 200
    model_id = applied.json()["draft_id"]
    assert _studio_rows() == (before[0] + 1, before[1] + 1)
    with open_sqlite_connection() as connection:
        events = connection.execute(
            "SELECT event_type, payload FROM events WHERE target_id=? ORDER BY rowid", (model_id,)).fetchall()
    assert [row[0] for row in events] == ["ModelSpecCreated"]
    assert json.loads(events[0][1])["action_request_digest"] == proposed.request_digest
    # Repeated Apply and a stale update leave both tables and the source untouched.
    assert client.post(f"/workspaces/main/actions/{proposed.action_id}/apply").status_code == 409
    revision = service.surface_brief("main", SurfaceRef(route_id="design-studio",
                                                            studio_model_id=model_id)).base_revision
    first = submit(action(mode="update", source=SOURCE + "# a\n", name="Renamed A"), revision=revision,
                   draft_id=model_id, interaction="a")
    second = submit(action(mode="update", source=SOURCE + "# b\n", name="Renamed B"), revision=revision,
                    draft_id=model_id, interaction="b")
    assert service.apply("main", first.action_id).state == "applied"
    snapshot = client.get(f"/workspaces/main/studio/models/{model_id}").json()
    assert client.post(f"/workspaces/main/actions/{second.action_id}/apply").status_code == 409
    after = client.get(f"/workspaces/main/studio/models/{model_id}").json()
    assert after["source"] == snapshot["source"] == SOURCE + "# a\n"
    assert after["name"] == "Renamed A" and after["updated_at"] == snapshot["updated_at"]
    assert _studio_rows() == (before[0] + 1, before[1] + 1)


def test_published_draft_cannot_be_updated_by_proposal(client):
    model_id = service.apply("main", submit(action(), interaction="pub").action_id).draft_id
    revision = service.surface_brief("main", SurfaceRef(route_id="design-studio",
                                                            studio_model_id=model_id)).base_revision
    proposed = submit(action(mode="update", source=SOURCE + "# x\n"), revision=revision, draft_id=model_id,
                      interaction="pub-update")
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE model_specs SET status='published' WHERE id=?", (model_id,))
        connection.commit()
    assert client.post(f"/workspaces/main/actions/{proposed.action_id}/apply").status_code == 409
    assert client.get(f"/workspaces/main/studio/models/{model_id}").json()["source"] == SOURCE


def test_category_change_through_update_is_refused(client):
    model_id = service.apply("main", submit(action(), interaction="cat").action_id).draft_id
    revision = service.surface_brief("main", SurfaceRef(route_id="design-studio",
                                                            studio_model_id=model_id)).base_revision
    outcome = submit(action("geometry", mode="update", source=GEOMETRY_SOURCE, contract=GEOMETRY),
                     revision=revision, draft_id=model_id, interaction="cat-update")
    assert outcome.state == "stale"


def test_relay_origin_proposes_only_and_never_executes(client, tmp_path):
    from app.modules.relay_gateway import service as relay

    canary = tmp_path / "relay-executed"
    source = f"from pathlib import Path\nPath({str(canary)!r}).write_text('bad')\n" + SOURCE
    block = {"surface": "studio", "base_revision": "new", "actions": [action(source=source)]}
    import json as _json
    text = "Here is a model.\n```jarvis-actions\n" + _json.dumps(block) + "\n```\n"
    prose, _ = relay._ingest_relay_actions(text, workspace_id="main", thread_id="thread-193", run_id="run-193",
                                           model="relay-model",
                                           action_context={"surface": "studio", "base_revision": "new"})
    assert "could not validate" not in prose
    assert _studio_rows()[0] == 0 and not canary.exists()
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT id, state, origin_json FROM workspace_actions WHERE surface='studio'"
                                 ).fetchone()
    assert row["state"] == "proposed" and '"relay"' in row["origin_json"]
    # A Relay block bound to a different target revision is refused before any proposal is stored.
    prose, _ = relay._ingest_relay_actions(text.replace('"new"', '"sha256:other"', 1), workspace_id="main",
                                           thread_id="thread-193", run_id="run-194", model="relay-model",
                                           action_context={"surface": "studio", "base_revision": "new"})
    assert "could not validate" in prose
    assert service.apply("main", row["id"]).state == "applied"
    assert _studio_rows()[0] == 1 and not canary.exists()


def test_secret_like_proposal_is_refused_and_not_stored(client):
    outcome = submit(action(assumptions="api_key = sk-ant-api03-" + "A" * 80), interaction="secret")
    assert outcome.state == "refused" and outcome.reason_code == "secret_detected"
    with open_sqlite_connection() as connection:
        stored = connection.execute("SELECT request_json, outcome_json FROM workspace_actions WHERE id=?",
                                    (outcome.action_id,)).fetchone()
    assert stored is not None and "sk-ant-api03" not in stored["outcome_json"] + stored["request_json"]


def test_hermes_studio_grant_owns_target_draft_and_revision(client, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.modules.agents.hermes.supervisor import UPSTREAM_REVISION, dispatch_tool
    from app.modules.ai.agent_contracts import (
        AgentSessionRef,
        CapabilityGrantRef,
        CapabilityScope,
        StructuredToolCall,
    )

    model_id = service.apply("main", submit(action(), interaction="grant").action_id).draft_id
    revision = service.surface_brief("main", SurfaceRef(route_id="design-studio",
                                                            studio_model_id=model_id)).base_revision
    session = AgentSessionRef(jarvis_thread_id="thread-193", hermes_session_id="hermes-193", profile_id="default",
                              workspace_id="main", generation=1,
                              upstream_revision=UPSTREAM_REVISION)
    now = datetime.now(UTC)
    grant = CapabilityGrantRef(
        grant_id="studio-act", capability_id="jarvis.studio_act", issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id="main", jarvis_thread_id="thread-193"),
        constraints={"surface": "studio", "route_id": "design-studio", "base_revision": revision,
                     "draft_id": model_id},
        issued_at=now, expires_at=now + timedelta(minutes=2))

    def call(arguments, call_id):
        return dispatch_tool(StructuredToolCall(
            call_id=call_id, capability_id="jarvis.studio_act", grant_id="studio-act", correlation_id=call_id,
            session_ref=session, arguments=arguments, requested_at=now, deadline_at=now + timedelta(minutes=1)),
            live_grants={"studio-act": grant}, interaction_id=f"turn-{call_id}")

    update = action(mode="update", source=SOURCE + "# hermes\n")
    ok = call({"base_revision": revision, "actions": [update]}, "ok")
    assert ok.status == "succeeded", ok
    assert ok.result["state"] == "proposed" and ok.result["applied"] is False
    wrong_revision = call({"base_revision": "sha256:other", "actions": [update]}, "rev")
    assert wrong_revision.status != "succeeded" or wrong_revision.result["state"] != "proposed"
    # The model cannot re-target another draft: the grant, not the arguments, names the draft.
    call({"base_revision": revision, "draft_id": "other-draft", "actions": [update]}, "retarget")
    with open_sqlite_connection() as connection:
        targets = {row[0] for row in connection.execute(
            "SELECT json_extract(request_json, '$.draft_id') FROM workspace_actions WHERE surface='studio' "
            "AND interaction_id LIKE 'turn-%'")}
    assert targets == {model_id}
    assert client.get(f"/workspaces/main/studio/models/{model_id}").json()["source"] == SOURCE


def test_auto_route_on_studio_surface_never_calls_external_provider(client, monkeypatch):
    from app.modules.ai import execution

    def forbid_external(**_kwargs):
        raise AssertionError("auto must not execute an external provider")

    monkeypatch.setattr(execution, "_run_external_network_task", forbid_external)
    brief = service.surface_brief("main", SurfaceRef(route_id="design-studio"))
    with open_sqlite_connection() as connection:
        jobs_before = connection.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0]
    outcome = execution.run_ai_task(user_prompt="Write a pressure-drop calculation for my pipe.",
                                    route_class="auto", workspace_id="main",
                                    context_blocks=[{"source": "jarvis:surface-brief", "content": brief.text}])
    assert outcome.selected_route_class in {None, "auto"}
    with open_sqlite_connection() as connection:
        job = connection.execute("SELECT * FROM ai_jobs ORDER BY rowid DESC LIMIT 1").fetchone()
        assert connection.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == jobs_before + 1
    assert job["requested_route_class"] == "auto" and job["provider_id"] is None
    assert job["adapter_invoked"] == 0 and job["execution_class"] == "none"
    assert job["external_dispatch_state"] == "not_applicable"
    assert _studio_rows()[0] == 0


def test_studio_tool_schema_spells_out_every_contract_field(client):
    # Constrained local decoding turns an untyped object into "{}", so every field must be declared.
    from app.modules.agents.hermes.broker_mcp import _STUDIO_ACT_TOOL
    from app.modules.studio.geometry import GeometryCreate
    from app.modules.studio.models import StudioCreate, Variable
    from app.modules.studio.unit_checks import CULTURE_FIELDS
    from app.modules.studio.unit_models import Port, StudioUnitCreate

    item = _STUDIO_ACT_TOOL["inputSchema"]["properties"]["actions"]["items"]["properties"]
    contract = item["contract"]["properties"]
    identity = {"name", "engineering_question", "category", "symbol"}
    for model in (StudioCreate, GeometryCreate, StudioUnitCreate):
        assert set(model.model_fields) - identity <= set(contract)
    assert set(contract["inputs"]["items"]["properties"]) == set(Variable.model_fields)
    assert set(contract["inlets"]["items"]["properties"]) == set(Port.model_fields)
    assert tuple(contract["culture_generation"]["items"]["enum"]) == CULTURE_FIELDS
    assert submit(action(contract={"inputs": [INPUT], "outputs": [OUTPUT]})).state == "proposed"


SECRET = "api_key = sk-ant-api03-" + "A" * 80


def _created_draft(interaction: str) -> str:
    applied = service.apply("main", submit(action(), interaction=interaction).action_id)
    assert applied.state == "applied" and applied.draft_id
    return applied.draft_id


def test_canonical_title_edit_makes_pending_update_stale(client):
    from app.modules.modeling.models import ModelSpecProjectUpdate
    from app.modules.modeling.project_knowledge_owner import update_model_spec_in_transaction

    model_id = _created_draft("meta-create")
    brief = service.surface_brief("main", SurfaceRef(route_id="design-studio", studio_model_id=model_id))
    proposed = submit(action(mode="update", name="Proposal title"), revision=brief.base_revision,
                      draft_id=model_id, interaction="meta-update")
    assert proposed.state == "proposed"
    # Another owner edits only the canonical model spec, not the Studio draft row.
    with open_sqlite_connection() as connection:
        spec = connection.execute("SELECT updated_at FROM model_specs WHERE id=?", (model_id,)).fetchone()
        update_model_spec_in_transaction(connection, model_id, ModelSpecProjectUpdate(
            workspace_id="main", expected_updated_at=spec["updated_at"], title="Operator title"))
        connection.commit()
    response = client.post(f"/workspaces/main/actions/{proposed.action_id}/apply")
    assert response.status_code == 409, response.text
    assert client.get(f"/workspaces/main/studio/models/{model_id}").json()["name"] == "Operator title"


def test_apply_refuses_cross_site_origin(client):
    proposed = submit(action(), interaction="origin")
    refused = client.post(f"/workspaces/main/actions/{proposed.action_id}/apply",
                          headers={"Origin": "https://attacker.example"})
    assert refused.status_code == 403
    assert client.get("/workspaces/main/studio/models").json() == []
    assert service.get("main", proposed.action_id).state == "proposed"


def test_secret_like_selected_draft_never_reaches_brief_or_card(client):
    model_id = _created_draft("secret-create")
    with open_sqlite_connection() as connection:
        contract = json.loads(connection.execute("SELECT contract_json FROM studio_drafts WHERE model_spec_id=?",
                                                 (model_id,)).fetchone()[0])
        contract["inputs"][0]["description"] = SECRET
        connection.execute("UPDATE studio_drafts SET contract_json=?, assumption_notes=? WHERE model_spec_id=?",
                           (json.dumps(contract), SECRET, model_id))
        connection.commit()
    brief = service.surface_brief("main", SurfaceRef(route_id="design-studio", studio_model_id=model_id))
    assert "sk-ant-api03" not in brief.text
    outcome = submit(action(mode="update"), revision=brief.base_revision, draft_id=model_id, interaction="secret-upd")
    assert outcome.state == "refused" and outcome.reason_code == "secret_detected"
    assert "sk-ant-api03" not in outcome.model_dump_json()


def test_secret_like_rationale_is_refused_and_not_stored(client):
    request = ActionRequest(surface="studio", base_revision="new", actions=[action()], rationale=SECRET)
    outcome = service.submit("main", request, ActionOrigin(kind="local", thread_id="thread-193",
                                                               interaction_id="secret-rationale"))
    assert outcome.state == "refused" and outcome.reason_code == "secret_detected"
    with open_sqlite_connection() as connection:
        stored = connection.execute("SELECT request_json, outcome_json FROM workspace_actions WHERE id=?",
                                    (outcome.action_id,)).fetchone()
    assert "sk-ant-api03" not in stored["outcome_json"] + stored["request_json"]


def test_secret_like_selected_draft_title_never_reaches_brief(client):
    model_id = _created_draft("secret-title")
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE model_specs SET title=? WHERE id=?", (SECRET, model_id))
        connection.commit()
    brief = service.surface_brief("main", SurfaceRef(route_id="design-studio", studio_model_id=model_id))
    assert brief.surface == "none" and "sk-ant-api03" not in brief.model_dump_json()
