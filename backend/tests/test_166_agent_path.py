from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx

from app.modules.agents.hermes.supervisor import dispatch_tool
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)
from app.modules.ai.contracts import AIRequest
from app.modules.ai.providers import local_llamacpp_adapter
from app.modules.ai.providers.local_llamacpp_adapter import LocalLlamaCppAdapter
from app.modules.ai.thread_service import (
    _derive_surface_brief,
    _direct_workspace_mode_instruction,
    _finalize_direct_workspace_answer,
    _guard_tool_shaped_output,
    _install_surface_grants,
    _turn_tools,
)
from app.modules.workspace_actions.models import SurfaceBrief, SurfaceRef

SESSION = AgentSessionRef(jarvis_thread_id="thread-166", hermes_session_id="hermes-166",
                          profile_id="default", workspace_id="workspace-166", generation=1,
                          upstream_revision="d337b736aa1e8ebecfab043842d13e4a2d2f48a3")


def test_turn_tools_are_scoped_to_owner_derived_surface() -> None:
    process = _turn_tools("process")
    bluecad = _turn_tools("bluecad")
    none = _turn_tools("none")
    assert "mcp__jarvis__jarvis_process_act" in process
    assert "mcp__jarvis__jarvis_bluecad_act" not in process
    assert "mcp__jarvis__jarvis_bluecad_act" in bluecad
    assert "mcp__jarvis__jarvis_process_act" not in bluecad
    assert not any("workspace" in name for name in none)
    assert all("jarvis_process" not in name and "jarvis_bluecad" not in name for name in none)


def test_process_brief_uses_biological_kinetics_explanation(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.core.database import initialize_database
    from app.main import app
    from app.modules.bio_models.forms import kinetics_explanation
    from app.modules.process_stack import draft
    from app.modules.workspace_actions.service import surface_brief

    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "jarvis"))
    get_settings.cache_clear()
    initialize_database()
    with TestClient(app) as client:
        response = client.post("/workspaces", json={"name": "Process brief", "slug": "process-brief-169"})
        assert response.status_code == 201
        workspace_id = response.json()["id"]
    created = draft.create_draft(workspace_id, "Process brief draft")
    brief = surface_brief(workspace_id, SurfaceRef(route_id="design-process", draft_id=created["draft_id"]))
    assert kinetics_explanation() in brief.text
    assert any(kinetics_explanation() in item for item in brief.limits)
    assert "Monod and custom rate laws unsupported" not in brief.text


def test_surface_brief_falls_back_to_none_with_reason(monkeypatch) -> None:
    from app.modules.workspace_actions import service

    def unavailable(_workspace_id, _ref):
        raise NotImplementedError("executor lane is not merged")

    monkeypatch.setattr(service, "surface_brief", unavailable)
    brief = _derive_surface_brief("workspace-166", SurfaceRef(route_id="design-process"))
    assert brief.surface == "none"
    assert "executor lane is not merged" in brief.text


def test_surface_grants_and_instructions_are_scoped_to_brief() -> None:
    worker = SimpleNamespace(live_grants={})
    brief = SurfaceBrief(surface="bluecad", route_id="design-bluecad", workspace_id="workspace-166",
                         base_revision="candidate-1", candidate_id="candidate-1", summary="BLUECAD · tube",
                         selected=[{"part_id": "template_tube", "kind": "tube_run"}],
                         text="candidate candidate-1 selected tube", digest="sha256:" + "b" * 64)
    text = _install_surface_grants(worker, "workspace-166", "thread-166",
                                   SurfaceRef(route_id="design-bluecad", candidate_id="candidate-1"), brief)
    assert {grant.capability_id for grant in worker.live_grants.values()} == {
        "jarvis.bluecad_read", "jarvis.bluecad_act"}
    assert "mcp__jarvis__jarvis_bluecad_act" in text
    assert "mcp__jarvis__jarvis_process_act" not in text
    assert "Monod" not in text
    assert all(grant.constraints["base_revision"] == "candidate-1" for grant in worker.live_grants.values())
    read_grant = next(grant for grant in worker.live_grants.values()
                      if grant.capability_id == "jarvis.bluecad_read")
    assert read_grant.constraints["candidate_id"] == "candidate-1"
    assert '"name":"mcp__jarvis__jarvis_bluecad_act"' in text
    assert '"op":"duplicate_part","part":"template_tube","placement":"beside"' in text
    assert '"surface_ref"' not in text


def test_process_turn_instructions_show_prefixed_read_and_typed_value_example() -> None:
    worker = SimpleNamespace(live_grants={})
    brief = SurfaceBrief(surface="process", route_id="design-process", workspace_id="workspace-166",
                         base_revision="rev-7", draft_id="draft-1", summary="Process · PFR-1",
                         selected=[{"kind": "stream", "tag": "S1"}],
                         text="draft draft-1 revision rev-7 selected stream S1", digest="sha256:" + "d" * 64)
    text = _install_surface_grants(worker, "workspace-166", "thread-166",
                                   SurfaceRef(route_id="design-process", draft_id="draft-1"), brief)
    assert 'mcp__jarvis__jarvis_process_read using only {"grant_id":"' in text
    assert '"name":"mcp__jarvis__jarvis_process_act"' in text
    assert '"op":"set_value","target":"S1","property":"pressure","value":{"value":2,"unit":"bar"}' in text
    assert "For state proposed" in text and "NOT been applied" in text
    assert "biological Monod card is a nutrient-limitation factor of a bioreactor growth model" in text
    assert "a Monod or Haldane rate law is a typed reactor reaction" in text
    assert "select the reactor and open Kinetics" in text
    # Spec 170: the PBR now exists, so the roadmap wording is gone and the unit is named.
    assert "arrive with 170" not in text and "Photobioreactor (T1)" in text
    assert "An explanation alone proposes no change" in text
    assert "only when state is applied and applied is true" in text
    assert '"surface_ref"' not in text


def test_guard_hides_tool_protocol_and_bounds_raw_details() -> None:
    visible, details = _guard_tool_shaped_output(
        '{"tool_calls":[{"name":"jarvis_process_act","arguments":{"grant_id":"private-grant"}}]}'
    )
    assert visible == "I couldn't complete that — Jarvis produced an invalid action request, so nothing was changed."
    assert details is not None and len(details) < 8_001 and "private-grant" not in details
    assert _guard_tool_shaped_output("No Monod kinetics are supported.") == ("No Monod kinetics are supported.", None)


def test_guard_suppresses_fenced_and_embedded_tool_json_and_redacts_grants() -> None:
    tool_json = '{"name":"mcp__jarvis__jarvis_process_act","arguments":{"grant_id":"private-grant","actions":[]}}'
    for text in (f"```json\n{tool_json}\n```", f"Here is the result: {tool_json} done.", f"~~~\n{tool_json}\n~~~"):
        visible, details = _guard_tool_shaped_output(text)
        assert visible == "I couldn't complete that — Jarvis produced an invalid action request, so nothing was changed."
        assert details is not None and "private-grant" not in details
    visible, details = _guard_tool_shaped_output('The `grant_id` field is used by the protocol, but no call was made.')
    assert details is None and "grant_id` field" in visible
    redacted, details = _guard_tool_shaped_output('Debug value: {"grant_id":"private-grant"}')
    assert details is None and "private-grant" not in redacted and "[redacted]" in redacted


def test_guard_hides_workspace_action_json_but_preserves_ordinary_json_prose() -> None:
    action = '{"op":"set_value","target":"Feed","property":"pressure","value":{"value":3,"unit":"bar"}}'
    for text in (action, f"Suggested change: {action}", f"[{action}]", f"```jarvis-actions\n{action}\n```"):
        visible, details = _guard_tool_shaped_output(text)
        assert visible.startswith("I couldn't complete that")
        assert details is not None and '"op"' in details
    assert _guard_tool_shaped_output('The JSON contains an "operation" field.') == (
        'The JSON contains an "operation" field.', None
    )
    assert _guard_tool_shaped_output('The word op appears in ordinary prose.') == (
        'The word op appears in ordinary prose.', None
    )


def test_direct_workspace_mode_is_truthful_for_change_requests() -> None:
    instruction = _direct_workspace_mode_instruction("process")
    assert "has no workspace-action tools" in instruction
    assert "do not emit action JSON" in instruction
    visible, details = _finalize_direct_workspace_answer(
        "change the Feed stream pressure to 3 bar", "local:llamacpp", "process",
        "I have prepared a proposal. Action JSON: {\"op\":\"set_value\"}",
    )
    assert visible == "This responder can't change the workspace. Switch to Jarvis agent, or use Escalate."
    assert details is not None and '"op"' in details
    prose, prose_details = _finalize_direct_workspace_answer(
        "What is JSON?", "local:llamacpp", "process", "JSON is a data format."
    )
    assert prose == "JSON is a data format." and prose_details is None
    unchanged, _ = _finalize_direct_workspace_answer(
        "change the Feed stream pressure", "hermes:agent", "process", "A proposal is ready."
    )
    assert unchanged == "A proposal is ready."


def test_dispatch_submits_typed_action_to_workspace_executor(monkeypatch) -> None:
    from app.modules.workspace_actions import service

    captured = {}

    outcome = SimpleNamespace(state="proposed", summary="Move PFR", reason=None, changes=[],
                              result_revision=None, child_candidate_id=None)

    def submit(workspace_id, request, origin):
        captured.update(workspace_id=workspace_id, request=request, origin=origin)
        return outcome

    monkeypatch.setattr(service, "submit", submit)
    duplicates: dict[str, object] = {}
    monkeypatch.setattr(service, "find_local_duplicate",
                        lambda workspace_id, interaction_id, request: duplicates.get(interaction_id))
    now = datetime.now(UTC)
    grant = CapabilityGrantRef(
        grant_id="process-act-grant", capability_id="jarvis.process_act", issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=SESSION.workspace_id, jarvis_thread_id=SESSION.jarvis_thread_id),
        constraints={"surface": "process", "route_id": "design-process", "base_revision": "rev-1"},
        issued_at=now, expires_at=now + timedelta(minutes=2),
    )
    call = StructuredToolCall(
        call_id="call-166", capability_id="jarvis.process_act", grant_id=grant.grant_id,
        correlation_id="call-166", session_ref=SESSION,
        arguments={"base_revision": "rev-1", "actions": [{"op": "move", "target": "PFR-1", "dx": 1, "dy": 0}]},
        requested_at=now, deadline_at=now + timedelta(minutes=1),
    )
    result = dispatch_tool(call, live_grants={grant.grant_id: grant}, interaction_id="interaction-166")
    assert result.status == "succeeded"
    assert result.result == {"state": "proposed", "applied": False,
                             "summary": "Proposed (NOT applied yet): Move PFR. The operator must click Apply on the card.",
                             "reason": None,
                             "changes": [], "result_revision": None, "child_candidate_id": None}
    assert captured["workspace_id"] == SESSION.workspace_id
    assert captured["request"].surface == "process"
    assert captured["origin"].interaction_id == "interaction-166"

    outcome.state = "applied"
    applied = dispatch_tool(call, live_grants={grant.grant_id: grant}, interaction_id="interaction-166")
    assert applied.result["applied"] is True
    assert applied.result["summary"] == "Applied: Move PFR."

    outcome.state, outcome.reason = "stale", "The draft revision changed."
    stale = dispatch_tool(call, live_grants={grant.grant_id: grant}, interaction_id="interaction-166")
    assert stale.result["applied"] is False
    assert stale.result["reason"] == "The draft revision changed."
    assert stale.result["summary"].startswith("Not applied:")

    duplicates["interaction-166"] = SimpleNamespace(state="proposed", summary="Move PFR", reason=None,
                                                    result_revision=None, child_candidate_id=None)
    captured.clear()
    repeated = dispatch_tool(call, live_grants={grant.grant_id: grant}, interaction_id="interaction-166")
    assert captured == {}
    assert repeated.result["duplicate"] is True and repeated.result["changes"] == []
    assert "Do not submit it again" in repeated.result["summary"]


def test_read_tool_derives_surface_ref_from_live_grant(monkeypatch) -> None:
    from app.modules.workspace_actions import service

    captured = {}
    brief = SurfaceBrief(surface="bluecad", route_id="design-bluecad", workspace_id=SESSION.workspace_id,
                         base_revision="candidate-1", candidate_id="candidate-1", summary="BLUECAD · tube",
                         text="candidate candidate-1 selected tube", digest="sha256:" + "c" * 64)

    def surface_brief(workspace_id, ref):
        captured.update(workspace_id=workspace_id, ref=ref)
        return brief

    monkeypatch.setattr(service, "surface_brief", surface_brief)
    now = datetime.now(UTC)
    grant = CapabilityGrantRef(
        grant_id="bluecad-read-grant", capability_id="jarvis.bluecad_read", issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=SESSION.workspace_id, jarvis_thread_id=SESSION.jarvis_thread_id),
        constraints={"surface": "bluecad", "route_id": "design-bluecad", "candidate_id": "candidate-1",
                     "base_revision": "candidate-1", "bluecad_part_count": 1, "bluecad_part_0": "part-1"},
        issued_at=now, expires_at=now + timedelta(minutes=2),
    )
    call = StructuredToolCall(
        call_id="read-166", capability_id="jarvis.bluecad_read", grant_id=grant.grant_id,
        correlation_id="read-166", session_ref=SESSION, arguments={}, requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )
    result = dispatch_tool(call, live_grants={grant.grant_id: grant})
    assert result.status == "succeeded"
    assert captured["workspace_id"] == SESSION.workspace_id
    assert captured["ref"] == SurfaceRef(route_id="design-bluecad", candidate_id="candidate-1",
                                         bluecad_part_ids=["part-1"])


def test_off_turn_or_wrong_capability_grants_cannot_dispatch(monkeypatch) -> None:
    now = datetime.now(UTC)
    process_grant = CapabilityGrantRef(
        grant_id="process-read", capability_id="jarvis.process_read", issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=SESSION.workspace_id, jarvis_thread_id=SESSION.jarvis_thread_id),
        issued_at=now, expires_at=now + timedelta(minutes=1),
    )
    call = StructuredToolCall(
        call_id="bluecad-call", capability_id="jarvis.bluecad_act", grant_id=process_grant.grant_id,
        correlation_id="bluecad-call", session_ref=SESSION,
        arguments={"base_revision": "candidate-1", "actions": [{"op": "delete_part", "part": "tube"}]},
        requested_at=now, deadline_at=now + timedelta(seconds=30),
    )
    result = dispatch_tool(call, live_grants={process_grant.grant_id: process_grant})
    assert result.status == "refused"


def test_llama_schema_defaults_on_and_switch_can_disable(monkeypatch) -> None:
    captured = []

    class Response:
        is_success = True

        def raise_for_status(self):
            return None

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}]}

    class Client:
        def post(self, _url, *, json, **_kwargs):
            captured.append(json)
            return Response()

        @staticmethod
        def close():
            return None

    schema = {"type": "object", "oneOf": [{"required": ["answer"]}]}
    request = AIRequest(task_type="synthesis", structured_output_schema=schema)
    monkeypatch.setattr(local_llamacpp_adapter, "llama_cpp_runtime_config",
                        lambda: SimpleNamespace(base_url="http://127.0.0.1:18081/v1", model_id="local", thinking="off",
                                                request_timeout_s=5))
    monkeypatch.setattr(local_llamacpp_adapter, "get_llama_cpp_runtime_owner",
                        lambda: SimpleNamespace(auth_headers=lambda: {}))
    monkeypatch.delenv("JARVIS_LLAMA_JSON_SCHEMA", raising=False)
    LocalLlamaCppAdapter(client_factory=Client).complete(request)
    assert captured[-1]["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "jarvis_agent_response", "strict": True, "schema": schema}}
    monkeypatch.setenv("JARVIS_LLAMA_JSON_SCHEMA", "false")
    LocalLlamaCppAdapter(client_factory=Client).complete(request)
    assert "response_format" not in captured[-1]


def test_llama_schema_rejection_retries_once_without_schema_and_records_fallback(monkeypatch, caplog) -> None:
    requests = []

    class Response:
        def __init__(self, reject=False):
            self.reject = reject

        def raise_for_status(self):
            if self.reject:
                response = httpx.Response(400, text="unsupported json schema grammar",
                                          request=httpx.Request("POST", "http://localhost"))
                raise httpx.HTTPStatusError("bad request", request=response.request, response=response)

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}]}

    class Client:
        def post(self, _url, *, json, **_kwargs):
            requests.append(dict(json))
            return Response(reject=len(requests) == 1)

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(local_llamacpp_adapter, "llama_cpp_runtime_config",
                        lambda: SimpleNamespace(base_url="http://127.0.0.1:18081/v1", model_id="local", thinking="off",
                                                request_timeout_s=5))
    monkeypatch.setattr(local_llamacpp_adapter, "get_llama_cpp_runtime_owner",
                        lambda: SimpleNamespace(auth_headers=lambda: {}))
    monkeypatch.delenv("JARVIS_LLAMA_JSON_SCHEMA", raising=False)
    response = LocalLlamaCppAdapter(client_factory=Client).complete(
        AIRequest(task_type="synthesis", structured_output_schema={"type": "object"}))
    assert len(requests) == 2
    assert "response_format" in requests[0] and "response_format" not in requests[1]
    assert response.raw_provider_metadata["json_schema_fallback"] is True
    assert "retrying once without response_format" in caplog.text


def test_llama_schema_fallback_does_not_retry_non_schema_http_error(monkeypatch) -> None:
    requests = []

    class Client:
        def post(self, _url, *, json, **_kwargs):
            requests.append(dict(json))
            response = httpx.Response(400, text="invalid model id",
                                      request=httpx.Request("POST", "http://localhost"))
            raise httpx.HTTPStatusError("bad request", request=response.request, response=response)

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(local_llamacpp_adapter, "llama_cpp_runtime_config",
                        lambda: SimpleNamespace(base_url="http://127.0.0.1:18081/v1", model_id="local", thinking="off",
                                                request_timeout_s=5))
    monkeypatch.setattr(local_llamacpp_adapter, "get_llama_cpp_runtime_owner",
                        lambda: SimpleNamespace(auth_headers=lambda: {}))
    monkeypatch.delenv("JARVIS_LLAMA_JSON_SCHEMA", raising=False)
    LocalLlamaCppAdapter(client_factory=Client).complete(
        AIRequest(task_type="synthesis", structured_output_schema={"type": "object"}))
    assert len(requests) == 1
