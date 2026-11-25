"""Spec 375: benchmarks yield the local model to the maintainer; navigation turns run light. Offline only."""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import app.modules.agents.hermes.supervisor as supervisor_module
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.agents.hermes.message_budget import estimate_tokens
from app.modules.agents.hermes.supervisor import (
    LIGHT_ROLE_LINE,
    UPSTREAM_REVISION,
    HermesSupervisor,
    _assemble_turn_prompt,
)
from app.modules.ai.agent_contracts import AgentSessionRef
from app.modules.ai.contracts import AIRequest, AITaskType
from app.modules.ai.providers.local_llamacpp_adapter import LocalLlamaCppAdapter
from app.modules.ai.settings import ensure_ai_settings
from app.modules.ai.thread_models import AIThreadCreate, AIThreadSubmit
from app.modules.ai.thread_service import create_thread, submit_interaction, turn_weight
from app.modules.ai.thread_ui_context import VISIBLE_PAGE_SOURCE
from app.modules.events.service import utc_now
from app.modules.local_ai.runtime import model_registry
from app.modules.local_ai.runtime.model_registry import ModelEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmarks.agent_eval.runner import (  # noqa: E402
    build_parser,
    run_eval,
    set_claude_runner,
    wait_for_model_idle,
)

SESSION = AgentSessionRef(jarvis_thread_id="thread-375", hermes_session_id="hermes-375", profile_id="isaac",
                          workspace_id="workspace-375", generation=1, upstream_revision=UPSTREAM_REVISION)


# --- criterion 1: the benchmark yields to the model ------------------------------------------------


class FakeSlots:
    """A fake `/slots` opener on a fake clock: busy for the first `busy_polls` polls, then idle."""

    def __init__(self, busy_polls: int, fail: bool = False) -> None:
        self.now = 1000.0
        self.polls = 0
        self.busy_polls = busy_polls
        self.fail = fail
        self.urls: list[str] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _slots_opener(slots: FakeSlots) -> Any:
    def opener(url: str, timeout: float = 0) -> Any:
        if slots.fail:
            slots.urls.append(url)
            raise urllib.error.URLError("refused")
        slots.urls.append(url)
        slots.polls += 1
        return _Response(json.dumps([{"id": 0, "is_processing": slots.polls <= slots.busy_polls}]).encode())
    return opener


def test_wait_for_model_idle_waits_for_a_continuous_quiet_window() -> None:
    slots = FakeSlots(busy_polls=5)
    result = wait_for_model_idle("http://127.0.0.1:8090", quiet_seconds=120, opener=_slots_opener(slots),
                                 clock=slots.clock, sleep=slots.sleep, poll_seconds=2.0)
    assert result is not None
    waited, saw_busy = result
    assert saw_busy is True
    # Five busy polls (10 s), then the first idle poll starts the window, which must last 120 s.
    assert waited >= 120 + 8
    assert slots.urls[0] == "http://127.0.0.1:8090/slots"


def test_wait_for_model_idle_restarts_the_window_when_a_slot_turns_busy_again() -> None:
    states = [False, False, True, False, False, False]
    clock = SimpleNamespace(now=0.0)
    polls: list[float] = []

    def opener(_url: str, timeout: float = 0) -> Any:
        polls.append(clock.now)
        busy = states.pop(0) if states else False
        return _Response(json.dumps([{"id": 0, "is_processing": busy}]).encode())

    def sleep(seconds: float) -> None:
        clock.now += seconds

    result = wait_for_model_idle("http://127.0.0.1:8090", quiet_seconds=10, opener=opener,
                                 clock=lambda: clock.now, sleep=sleep, poll_seconds=4.0)
    assert result is not None and result[1] is True
    # busy at t=8 restarts the window: idle again from t=12, so the wait ends at t=24, not t=10.
    assert result[0] == 24.0


@pytest.mark.parametrize("body", [None, b"not json", b'{"id": 0}', b"[]", b"[{}]", b"[[]]", b"[1]",
                                  b'[{"id": 0, "is_processing": "no"}]', b'[{"id": 0}, "x"]'])
def test_wait_for_model_idle_does_not_block_on_unreachable_or_malformed_slots(body: bytes | None) -> None:
    def opener(_url: str, timeout: float = 0) -> Any:
        if body is None:
            raise urllib.error.URLError("refused")
        return _Response(body)

    assert wait_for_model_idle("http://127.0.0.1:8090", quiet_seconds=120, opener=opener,
                               clock=lambda: 0.0, sleep=lambda _s: None) is None


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_yield_quiet_seconds_must_be_finite_and_non_negative(tmp_path: Path, bad: float) -> None:
    with pytest.raises(ValueError):
        run_eval(tasks_path=_task_file(tmp_path), label="y", out_dir=tmp_path / "out", reference="haiku",
                 yield_to_model="http://127.0.0.1:8090", yield_quiet_seconds=bad)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--tasks", "t", "--label", "l", "--out", "o",
                                   "--yield-quiet-seconds", str(bad)])


def _task_file(tmp_path: Path) -> Path:
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"set_id": "t", "cloud_ok": True, "tasks": [
        {"id": "a", "prompt": "p", "checks": [{"type": "contains_any", "values": ["ok"]}]}]}), encoding="utf-8")
    return path


def test_run_eval_records_yield_wait_and_totals(tmp_path: Path) -> None:
    slots = FakeSlots(busy_polls=3)
    set_claude_runner(lambda _prompt, _model: ("ok", "completed", None))
    try:
        result = run_eval(tasks_path=_task_file(tmp_path), label="y", reps=2, out_dir=tmp_path / "out",
                          reference="haiku", yield_to_model="http://127.0.0.1:8090", yield_quiet_seconds=30,
                          slots_opener=_slots_opener(slots), clock=slots.clock, sleep=slots.sleep)
    finally:
        set_claude_runner(None)
    reps = result["tasks"]["a"]["repetitions"]
    assert reps[0]["yield_wait_s"] >= 30 and reps[1]["yield_wait_s"] >= 30
    # Only the first repetition saw a busy slot, so only it counts as a pause; the mandatory quiet-window
    # wait of the initially idle second repetition is still waiting time and is reported in the total.
    assert result["yield_pauses"] == 1
    assert result["yield_paused_s"] == round(reps[0]["yield_wait_s"] + reps[1]["yield_wait_s"], 3)
    # Waiting is not benchmark wall time: the fake clock only moves while waiting, so no wait leaks in.
    assert all(r["wall_time_s"] < 1 for r in reps) and result["total_wall_time_s"] < 1
    assert json.loads((tmp_path / "out" / "results.json").read_text())["yield_pauses"] == 1


def test_run_eval_continues_when_slots_is_unreachable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    slots = FakeSlots(busy_polls=0, fail=True)
    set_claude_runner(lambda _prompt, _model: ("ok", "completed", None))
    try:
        result = run_eval(tasks_path=_task_file(tmp_path), label="y", reps=2, out_dir=tmp_path / "out",
                          reference="haiku", yield_to_model="http://127.0.0.1:8090",
                          slots_opener=_slots_opener(slots), clock=slots.clock, sleep=slots.sleep)
    finally:
        set_claude_runner(None)
    assert result["yield_pauses"] == 0 and result["yield_paused_s"] == 0
    assert [r["yield_wait_s"] for r in result["tasks"]["a"]["repetitions"]] == [0.0, 0.0]
    assert capsys.readouterr().err.count("unreachable or malformed") == 1  # logged once


def test_run_eval_refuses_a_yield_url_on_port_8000(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_eval(tasks_path=_task_file(tmp_path), label="y", out_dir=tmp_path / "out", reference="haiku",
                 yield_to_model="http://127.0.0.1:8000")


# --- criterion 2: the label ------------------------------------------------------------------------


@pytest.mark.parametrize("question", [
    "mostrami la schermata CFD", "dove trovo il flowsheet?", "apri la libreria", "vai alla pagina Studio",
    "Where is the CFD screen?", "show me the plant", "open the library", "go to Studio", "find the Studio tab",
    "go to Design", "Go to the Design page", "vai a Design", "apri la schermata Design", "dove trovo Design?",
])
def test_label_navigation_with_ui_context_is_light(question: str) -> None:
    assert turn_weight(question, has_ui_context=True) == "light"


@pytest.mark.parametrize("question", ["ciao", "Grazie mille!", "buongiorno Bubu", "hello", "thanks", "hi there"])
def test_label_short_greetings_are_light_in_both_languages(question: str) -> None:
    assert turn_weight(question, has_ui_context=False) == "light"
    assert turn_weight(question, has_ui_context=True) == "light"


@pytest.mark.parametrize(("question", "has_ui"), [
    ("mostrami la schermata CFD", False),  # navigation wording without Bubu's page description
    ("show me the plant", False),
    ("mostrami la schermata 2", True),  # digit
    ("ciao 2", False),
    ("mostrami quanto vale la portata", True),  # calculation word
    ("calcola la perdita di carico", True),
    ("verifica il dimensionamento della pompa", True),
    ("Where is the pressure drop calculation? calculate it", True),
    ("ciao, mi spieghi come funziona la rete di scambio termico del flowsheet", True),  # not a short greeting
    ("design a heat exchanger", True),
    ("go to Design and design a heat exchanger", True),
    ("vai a Design e progetta uno scambiatore", True),
    ("show me the design of the heat exchanger", True),
    ("go to Design", False),  # no page description: not navigation
    ("", True),
])
def test_label_other_turns_stay_work(question: str, has_ui: bool) -> None:
    assert turn_weight(question, has_ui_context=has_ui) == "work"


# --- criterion 3: the light path -------------------------------------------------------------------


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE events (id TEXT PRIMARY KEY, workspace_id TEXT, event_type TEXT, "
                       "actor TEXT, target_type TEXT, target_id TEXT, payload TEXT, created_at TEXT)")
    return connection


class _Keep:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None


def _relay_metadata(monkeypatch: pytest.MonkeyPatch, *, light: bool, tools: list[dict[str, Any]],
                    allowed: list[str]) -> dict[str, Any]:
    monkeypatch.setattr(supervisor_module, "open_sqlite_connection", lambda: _Keep(_db()))
    seen: list[dict[str, Any]] = []

    def runner(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return SimpleNamespace(status="success", response=SimpleNamespace(
            text="Answer: ecco [[point:design.stage.flow]]", finish_reason="stop"), flow_id=None)

    supervisor = HermesSupervisor("unused", runner=runner, route_for_task=lambda _task: "local:llamacpp")
    supervisor.session = SESSION
    supervisor.turn_light = light
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(supervisor, "_send", sent.append)
    supervisor._handle_relay({"type": "relay_request", "id": "r1", "session_ref": SESSION.model_dump(mode="json"),
                              "messages": [{"role": "user", "content": "mostrami la schermata CFD"}],
                              "tools": tools, "allowed_tools": allowed})
    assert len(seen) == 1 and sent[-1]["status"] == "success"
    return {"kwargs": seen[0], "sent": sent[-1]}


_TOOL = "mcp__jarvis__jarvis_python"
_TOOLS = [{"type": "function", "function": {"name": _TOOL, "description": "Run python.",
                                              "parameters": {"type": "object", "properties": {}}}}]


def test_light_relay_has_no_tools_and_asks_for_low_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    light = _relay_metadata(monkeypatch, light=True, tools=_TOOLS, allowed=[])
    assert light["kwargs"]["metadata"] == {"reasoning_effort": "low"}
    assert "tools" not in light["kwargs"]["user_prompt"] and _TOOL not in light["kwargs"]["user_prompt"]
    assert light["sent"].get("admitted_tools") is None
    work = _relay_metadata(monkeypatch, light=False, tools=_TOOLS, allowed=[_TOOL])
    assert work["kwargs"]["metadata"] == {}  # a work turn's request is unchanged
    assert _TOOL in work["kwargs"]["user_prompt"]


def _entry(**overrides: Any) -> ModelEntry:
    return ModelEntry(id="inkling-sq10", label="Inkling", runtime="strata", qualification="default",
                      unit="inkling.service", port=8080, served_model="inkling-2-mini-sq10",
                      reasoning_effort_analysis="high", reasoning_effort_final="low", **overrides)


def _adapter(payloads: list[dict[str, Any]]) -> LocalLlamaCppAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    return LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_adapter_honours_low_effort_override_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_registry, "get_active_model_entry", _entry)
    payloads: list[dict[str, Any]] = []
    adapter = _adapter(payloads)
    adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="p", metadata={}))
    adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="p", metadata={"reasoning_effort": "low"}))
    adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="p", metadata={"reasoning_effort": "high"}))
    assert [p["chat_template_kwargs"]["reasoning_effort"] for p in payloads] == ["high", "low", "high"]


def _ui() -> dict:
    return {"route": "/design/project?stage=plant", "title": "Design",
            "targets": [{"id": "design.stage.plant", "role": "button", "name": "Plant"},
                        {"id": "design.stage.cfd", "role": "button", "name": "CFD"}]}


def _workspace(workspace_id: str) -> str:
    initialize_database()
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute("INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (workspace_id, workspace_id, workspace_id, now, now))
        connection.commit()
    ensure_ai_settings()
    return workspace_id


class _Worker:
    session = None
    live_grants: dict = {}
    last_error = None
    turn_tool_outputs: list = []

    def __init__(self, replies: list[dict[str, Any]], route: str = "local:llamacpp") -> None:
        self.replies = replies
        self.route = route
        self.turns: list[dict[str, Any]] = []

    def selected_turn_route(self) -> str:
        return self.route

    def _alive(self) -> bool:
        return False

    def start(self) -> None:
        pass

    def _send(self, _frame: dict) -> None:
        pass

    def _await(self, _request_id: str, timeout: float) -> dict:
        return {"type": "ack"}

    def turn(self, prompt: str, **kwargs: Any) -> dict:
        self.turns.append({"prompt": prompt, **kwargs})
        return self.replies[min(len(self.turns), len(self.replies)) - 1]


def _submit(worker: _Worker, workspace_id: str, request_id: str, prompt: str, **extra: Any) -> Any:
    thread = create_thread(AIThreadCreate(workspace_id=workspace_id))
    pool = SimpleNamespace(for_thread=lambda _t: worker,
                           lock_for=lambda _t: contextmanager(lambda: (yield))(),
                           schedule_idle_stop=lambda _t: None)
    availability = [{"route_class": "hermes:agent", "availability": {"runtime_reachable": True},
                     "roles": [{"role_id": "isaac", "available": True}]}]
    payload = AIThreadSubmit(request_id=request_id, prompt=prompt, route_class="hermes:agent", **extra)
    return submit_interaction(workspace_id=workspace_id, thread_id=thread.id, payload=payload,
                              route_availability=availability,
                              app_state=SimpleNamespace(hermes_supervisor=pool)).interaction


def test_light_turn_sends_one_light_turn_with_the_page_block_under_3k_tokens() -> None:
    workspace_id = _workspace("workspace-375-light")
    worker = _Worker([{"status": "failed"}])
    _submit(worker, workspace_id, "light-1", "mostrami la schermata CFD", ui_context=_ui())
    first = worker.turns[0]
    assert first["light"] is True and first["allowed_tools"] == []
    assert [b["source"] for b in first["context_blocks"]] == [VISIBLE_PAGE_SOURCE]
    assert "design.stage.cfd" in first["context_blocks"][0]["content"]
    assembled = _assemble_turn_prompt(first["prompt"], first["context_blocks"], include_rules=False)
    assert estimate_tokens(len(assembled) + len(LIGHT_ROLE_LINE)) < 3000


def test_work_turn_is_unchanged() -> None:
    workspace_id = _workspace("workspace-375-work")
    worker = _Worker([{"status": "failed"}])
    _submit(worker, workspace_id, "work-1", "calcola la perdita di carico", ui_context=_ui())
    assert len(worker.turns) == 1 and "light" not in worker.turns[0]
    assert worker.turns[0]["allowed_tools"]
    sources = {b["source"] for b in worker.turns[0]["context_blocks"]}
    assert "jarvis:system-envelope" in sources


def test_cloud_role_route_skips_the_light_path_and_keeps_grants_and_context() -> None:
    workspace_id = _workspace("workspace-375-cloud")
    worker = _Worker([{"status": "failed"}], route="cloud:scaleway")
    _submit(worker, workspace_id, "cloud-1", "mostrami la schermata CFD", ui_context=_ui())
    assert len(worker.turns) == 1 and "light" not in worker.turns[0]
    assert worker.turns[0]["allowed_tools"]
    assert "jarvis:system-envelope" in {b["source"] for b in worker.turns[0]["context_blocks"]}


def test_supervisor_route_helper_follows_the_role_route() -> None:
    supervisor = HermesSupervisor("unused", runner=lambda **_k: None, route_for_task=lambda _t: "cloud:task",
                                  route_for_role=lambda role: f"local:{role}")
    assert supervisor.selected_turn_route() is None  # no bound session
    supervisor.session = SESSION
    assert supervisor.selected_turn_route() == "local:isaac"
    supervisor.route_for_role = None
    assert supervisor.selected_turn_route() == "cloud:task"


def test_light_turn_discards_grants_left_by_a_prior_work_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = HermesSupervisor("unused", route_for_task=lambda _task: "local:llamacpp")
    worker.session = SESSION
    worker.live_grants["prior-turn"] = object()  # type: ignore[assignment]
    monkeypatch.setattr(worker, "_alive", lambda: True)
    monkeypatch.setattr(worker, "_send", lambda _frame: None)
    monkeypatch.setattr(worker, "_await", lambda _id, **_kwargs: {"status": "failed", "completed": False})
    monkeypatch.setattr(supervisor_module, "open_sqlite_connection", lambda: _Keep(_db()))
    monkeypatch.setattr(model_registry, "get_active_model_entry", lambda: None)

    worker.turn("ciao", light=True, allowed_tools=[])
    assert worker.live_grants == {}


# --- criterion 4: fallback -------------------------------------------------------------------------


def test_light_turn_without_an_answer_falls_back_once_to_the_work_path() -> None:
    workspace_id = _workspace("workspace-375-fallback")
    worker = _Worker([{"status": "success", "completed": True, "final_response": "  "}, {"status": "failed"}])
    _submit(worker, workspace_id, "fb-1", "mostrami la schermata CFD", ui_context=_ui())
    assert len(worker.turns) == 2
    assert worker.turns[0]["light"] is True and worker.turns[0]["allowed_tools"] == []
    assert "light" not in worker.turns[1] and worker.turns[1]["allowed_tools"]
    assert "jarvis:system-envelope" in {b["source"] for b in worker.turns[1]["context_blocks"]}


# --- criterion 5: DOM first ------------------------------------------------------------------------


def test_images_on_a_hermes_turn_never_reach_the_model() -> None:
    png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    workspace_id = _workspace("workspace-375-images")
    for prompt in ("mostrami la schermata CFD", "calcola la perdita di carico"):
        worker = _Worker([{"status": "failed"}, {"status": "failed"}])
        interaction = _submit(worker, workspace_id, f"img-{prompt[:3]}", prompt, ui_context=_ui(), images=[png])
        assert interaction.images_unsupported is True
        for turn in worker.turns:
            assert "images" not in turn
            assert all(png not in b["content"] for b in turn["context_blocks"])
