"""Spec 374: grants live for the whole turn, refusals say why, the format is described, patent fields stay visible."""
from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError
from uuid import uuid4

import pytest

from app.core.database import initialize_database
from app.modules.agents.hermes import supervisor as supervisor_module
from app.modules.agents.hermes.message_budget import compact_local_messages
from app.modules.agents.hermes.patent_read_tool import GOOGLE_PATENTS_BASE_URL, execute_patent_read
from app.modules.agents.hermes.supervisor import (
    CONTINUATION_REQUEST,
    RELAY_TOOL_PROTOCOL,
    UPSTREAM_REVISION,
    dispatch_tool,
    missing_final_answer,
    refusal_message,
    strip_empty_template,
    strip_template_from_reply,
    turn_deadline,
)
from app.modules.agents.hermes.worker_shim import (
    COMPACT_METHODOLOGY,
    STEP_CAP_MESSAGE,
    compact_tool_payload,
)
from app.modules.ai.agent_contracts import AgentSessionRef, StructuredToolCall
from app.modules.ai.thread_service import _install_hermes_retrieval_grant, _install_surface_grants
from app.modules.studio_runtime import runtime
from app.modules.workspace_actions.models import SurfaceBrief, SurfaceRef
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace

TEMPLATE = "Answer: 1-3 sentences\nNumbers:\nSources:\nCaveats:"


@pytest.fixture
def workspace_id() -> str:
    initialize_database()
    return create_workspace(WorkspaceCreate(name=f"T374 {uuid4().hex[:6]}", slug=f"t374-{uuid4().hex[:8]}")).id


def _worker(step_seconds: int | None = 240, turn_cap: int | None = 10) -> Any:
    worker = SimpleNamespace(live_grants={})
    if step_seconds is not None:
        worker.step_deadline_seconds = step_seconds
    if turn_cap is not None:
        worker.turn_cap = turn_cap
    return worker


def _brief(surface: str, workspace: str) -> SurfaceBrief:
    return SurfaceBrief(surface=surface, route_id=f"design-{surface}" if surface != "none" else "none",
                        workspace_id=workspace, base_revision="rev-1", draft_id="d-1", summary="s",
                        selected=[], text="t", digest="sha256:" + "0" * 64)


def _call(workspace: str, grant_id: str, now: datetime, arguments: dict[str, Any]) -> StructuredToolCall:
    session = AgentSessionRef(jarvis_thread_id="thread-374", hermes_session_id="hermes-374", profile_id="default",
                              workspace_id=workspace, generation=1, upstream_revision=UPSTREAM_REVISION)
    return StructuredToolCall(call_id=f"call-{uuid4().hex[:8]}", capability_id="jarvis.calc_run", grant_id=grant_id,
                              correlation_id="c", session_ref=session, arguments=arguments,
                              requested_at=now, deadline_at=now + timedelta(seconds=30))


def _fake_clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return now

    monkeypatch.setattr(supervisor_module, "datetime", Clock)


# Criterion 1
def test_python_call_after_fifteen_minutes_succeeds_and_is_refused_with_a_reason_after_the_deadline(
        monkeypatch: pytest.MonkeyPatch, workspace_id: str) -> None:
    worker = _worker()
    _install_surface_grants(worker, workspace_id, "thread-374", SurfaceRef(route_id="design-none"),
                            _brief("none", workspace_id))
    (grant_id, grant), = worker.live_grants.items()
    deadline = turn_deadline(240, 10)
    assert deadline > 15 * 60
    record = runtime.RunRecord(
        source_sha256="a" * 64,
        limits=runtime.AppliedLimits(**{n: runtime.PROFILES["calculation"][n] for n in runtime.AppliedLimits.model_fields}),
        mechanism=runtime.MECHANISM, duration_seconds=0.1, classification="ok", stdout="3\n")
    monkeypatch.setattr(runtime, "run", lambda *a, **k: record)

    later = grant.issued_at + timedelta(minutes=15)
    _fake_clock(monkeypatch, later)
    ok = dispatch_tool(_call(workspace_id, grant_id, later, {"code": "print(3)"}), live_grants=worker.live_grants)
    assert ok.status == "succeeded"

    after = grant.issued_at + timedelta(seconds=deadline + 61)
    _fake_clock(monkeypatch, after)
    call = _call(workspace_id, grant_id, after, {"code": "print(3)"})
    refused = dispatch_tool(call, live_grants=worker.live_grants)
    assert (refused.status, refused.error_code) == ("refused", "grant_expired")
    assert "answer with what you have" in (refusal_message(call, "grant_expired") or "")


# Criterion 2
@pytest.mark.parametrize(("step", "cap", "expected"), [(240, 10, 2520), (240, 20, 3600), (30, 3, 600), (None, None, 1320)])
@pytest.mark.parametrize("surface", ["none", "bluecad"])
def test_surface_grants_live_for_the_turn_deadline_plus_a_minute(workspace_id: str, surface: str, step: int | None,
                                                                 cap: int | None, expected: int) -> None:
    worker = _worker(step, cap)
    _install_surface_grants(worker, workspace_id, "thread-374", SurfaceRef(route_id=f"design-{surface}"),
                            _brief(surface, workspace_id))
    assert worker.live_grants
    for grant in worker.live_grants.values():
        assert grant.expires_at - grant.issued_at == timedelta(seconds=expected + 60)


def test_retrieval_grants_live_for_the_turn_deadline_plus_a_minute(workspace_id: str) -> None:
    worker = _worker(200, 12)
    payload = SimpleNamespace(jarvis_context=None, role_id="isaac")
    worker.session = None
    _install_hermes_retrieval_grant(worker, payload, workspace_id, "thread-374")  # type: ignore[arg-type]
    assert len(worker.live_grants) >= 2
    for grant in worker.live_grants.values():
        assert grant.expires_at - grant.issued_at == timedelta(seconds=turn_deadline(200, 12) + 60)


# Criterion 3
def test_unknown_grant_is_denied_with_a_message_and_python_without_code_names_it(workspace_id: str) -> None:
    now = datetime.now(UTC)
    call = _call(workspace_id, "no-such-grant", now, {"code": "print(1)"})
    result = dispatch_tool(call, live_grants={})
    assert (result.status, result.error_code) == ("refused", "capability_denied")
    message = refusal_message(call, "capability_denied")
    assert message and "grant" in message and "no-such-grant" not in message

    worker = _worker()
    _install_surface_grants(worker, workspace_id, "thread-374", SurfaceRef(route_id="design-none"),
                            _brief("none", workspace_id))
    (grant_id, _), = worker.live_grants.items()
    bad = _call(workspace_id, grant_id, now, {})
    refused = dispatch_tool(bad, live_grants=worker.live_grants)
    assert (refused.status, refused.error_code) == ("refused", "invalid_arguments")
    assert "`code` is missing" in (refusal_message(bad, "invalid_arguments") or "")
    for arguments, text in [({"code": 5}, "must be a string"), ({"code": "x" * 20_000}, "limit")]:
        assert text in (refusal_message(_call(workspace_id, grant_id, now, arguments), "invalid_arguments") or "")
        assert "xxxx" not in (refusal_message(_call(workspace_id, grant_id, now, arguments), "invalid_arguments") or "")


def test_refusal_message_reaches_the_worker_frame(monkeypatch: pytest.MonkeyPatch, workspace_id: str) -> None:
    sent: list[dict[str, Any]] = []
    session = AgentSessionRef(jarvis_thread_id="thread-374", hermes_session_id="hermes-374", profile_id="default",
                              workspace_id=workspace_id, generation=1, upstream_revision=UPSTREAM_REVISION)
    worker = supervisor_module.HermesSupervisor("unused")
    worker.session = session
    monkeypatch.setattr(worker, "_send", sent.append)
    worker._handle_tool({"id": "c1", "session_ref": session.model_dump(mode="json"),
                         "arguments": {"tool_name": "jarvis_python", "grant_id": "gone", "code": "print(1)"}})
    frame = sent[-1]["tool_result"]
    assert frame["error_code"] == "capability_denied" and "grant" in frame["message"] and "gone" not in frame["message"]


# Criterion 4
def test_format_is_described_in_words_and_methodology_has_the_two_rules() -> None:
    for text in (COMPACT_METHODOLOGY, STEP_CAP_MESSAGE, CONTINUATION_REQUEST):
        lines = [line.strip() for line in text.splitlines()]
        assert not {"Numbers:", "Sources:", "Caveats:"} & set(lines)
        assert "Answer: 1-3 sentences" not in text
        assert "`Answer:`" in text and "`Numbers:`" in text and "`Sources:`" in text and "`Caveats:`" in text
    assert "Use every given input; if you leave one out, say why." in COMPACT_METHODOLOGY
    assert "If your own check shows an inconsistency, fix it before answering." in COMPACT_METHODOLOGY


# Criterion 5
def test_strip_removes_only_the_empty_template_block() -> None:
    echoed = TEMPLATE + "Answer: I could not finish.\nNumbers: 3"
    assert strip_empty_template(echoed) == "Answer: I could not finish.\nNumbers: 3"
    assert strip_empty_template(TEMPLATE + "\n\nAnswer: x") == "Answer: x"
    for normal in ("Answer: 4 m.\nNumbers: 4\nSources: none\nCaveats: none", "Answer: 1-3 sentences\nNumbers: 5",
                   "Numbers:\nSources:", ""):
        assert strip_empty_template(normal) == normal
    with_content = TEMPLATE + " none reported"
    assert strip_empty_template(with_content) == with_content


def test_strip_applies_to_plain_and_json_replies() -> None:
    assert strip_template_from_reply(TEMPLATE + "Answer: ok") == "Answer: ok"
    wrapped = strip_template_from_reply(json.dumps({"answer": TEMPLATE + "Answer: ok"}))
    assert json.loads(wrapped) == {"answer": "Answer: ok"}
    untouched = json.dumps({"answer": "Answer: ok"})
    assert strip_template_from_reply(untouched) == untouched


def test_missing_final_answer_ignores_the_empty_template() -> None:
    assert missing_final_answer(TEMPLATE) == TEMPLATE
    assert missing_final_answer(json.dumps({"answer": TEMPLATE})) == TEMPLATE
    assert missing_final_answer(TEMPLATE + "Answer: done") is None
    assert missing_final_answer("Answer: done") is None


def test_local_final_answer_is_stripped_before_it_is_returned() -> None:
    import inspect

    source = inspect.getsource(supervisor_module)
    assert "final_text = strip_template_from_reply(final_text)" in source


def test_turn_extends_grants_to_the_measured_turn_deadline(monkeypatch: pytest.MonkeyPatch,
                                                            workspace_id: str) -> None:
    supervisor = supervisor_module.HermesSupervisor("unused")
    supervisor.session = AgentSessionRef(jarvis_thread_id="thread-374", hermes_session_id="hermes-374",
                                         profile_id="default", workspace_id=workspace_id, generation=1,
                                         upstream_revision=UPSTREAM_REVISION)
    _install_surface_grants(supervisor, workspace_id, "thread-374", None, _brief("none", workspace_id))
    monkeypatch.setattr("app.modules.agents.hermes.roles.get_role_instructions", lambda *_a, **_k: "role")
    monkeypatch.setattr(supervisor_module, "step_deadline_from_rows", lambda *_a: 900)

    class Alive:
        def poll(self) -> None:
            return None

    supervisor.process = Alive()  # type: ignore[assignment]
    monkeypatch.setattr(supervisor, "_send", lambda _frame: None)

    def answer(_id: str, **_kwargs: Any) -> dict[str, Any]:
        supervisor.last_flow[supervisor.session.hermes_session_id] = "flow-1"  # type: ignore[union-attr]
        return {"type": "turn_result", "status": "success", "completed": True, "final_response": "Answer: ok"}

    monkeypatch.setattr(supervisor, "_await", answer)
    before = datetime.now(UTC)
    assert supervisor.turn("question")["status"] == "success"
    (grant,) = supervisor.live_grants.values()
    assert grant.expires_at >= before + timedelta(seconds=3600 + 60)
    assert grant.expires_at <= datetime.now(UTC) + timedelta(seconds=3600 + 60)


# Criterion 6
class _Response:
    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data)), "Content-Type": "text/html; charset=utf-8"}
        self.status = 200

    def read(self, amt: int = -1) -> bytes:
        return self._stream.read(amt)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _Opener:
    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages

    def open(self, req: Any, timeout: float = 10.0) -> Any:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for pattern, data in self.pages.items():
            if pattern in url:
                return _Response(data)
        if "register.epo.org" in url:
            from urllib.error import HTTPError

            raise HTTPError(url, 403, "Forbidden", {}, None)  # type: ignore[arg-type]
        raise URLError(url)


EP_HTML = (b'<html><head><meta name="DC.title" content="Synthetic Acoustic Transducer Assembly">'
           b'<meta name="DC.contributor" scheme="assignee" content="Synthetic Aero SA">'
           b'<meta name="DC.date" scheme="priority" content="2017-05-15"></head><body>'
           b'<span itemprop="legalStatus">Pending</span><div class="claim" num="1"><div class="claim-text">'
           b'1. An acoustic transducer comprising a housing and a synthetic diaphragm, '
           + b"the diaphragm having a coating of layered material in several steps, " * 20 +
           b'</div></div><a href="https://register.epo.org/application?number=EP18123456">Register</a></body></html>')


def test_patent_result_leads_with_bibliography_and_states_the_register_status() -> None:
    opener = _Opener({f"{GOOGLE_PATENTS_BASE_URL}/EP18123456A1/en": EP_HTML})
    result = execute_patent_read("EP18123456A1", opener=opener)
    keys = list(result)
    assert keys.index("title") < keys.index("claim_1") < keys.index("legal_status_note") < keys.index("google_legal_status")
    assert result["legal_status_note"].startswith("official legal status not verified: the EPO Register refused "
                                                  "automated access (HTTP 403)")


def test_patent_claim_and_register_sentence_survive_four_later_tool_results() -> None:
    opener = _Opener({f"{GOOGLE_PATENTS_BASE_URL}/EP18123456A1/en": EP_HTML})
    result = compact_tool_payload(execute_patent_read("EP18123456A1", opener=opener))
    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    contents = [json.dumps(result)] + [f"{i}" + "r" * 1500 for i in range(5)]
    for i, content in enumerate(contents):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": content})
    messages.append({"role": "assistant", "content": "next"})
    sent, _ = compact_local_messages(messages, [], RELAY_TOOL_PROTOCOL, overhead=0, budget=100_000)
    patent = next(m["content"] for m in sent if m["role"] == "tool" and "claim_1" in m["content"])
    assert len(patent) > 700 or "claim_1" in patent
    assert result["claim_1"][:300] in patent
    assert "official legal status not verified: the EPO Register refused automated access (HTTP 403)" in patent
    assert "Synthetic Acoustic Transducer Assembly" in patent
