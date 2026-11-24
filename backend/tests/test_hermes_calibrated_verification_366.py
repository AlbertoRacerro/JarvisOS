import json

from test_hermes_saved_plan_362 import _connection

from app.modules.agents.hermes.broker_mcp import (
    _BENCH_READ_TOOL,
    _BENCH_RUN_TOOL,
    _JARVIS_BROWSER_PAGE_TOOL,
    _JARVIS_PAPER_PDF_READ_TOOL,
    _JARVIS_PAPER_SEARCH_TOOL,
    _JARVIS_PATENT_READ_TOOL,
    _KNOWLEDGE_READ_TOOL,
    _TURN_PLAN_TOOL,
)
from app.modules.agents.hermes.supervisor import _assemble_turn_prompt
from app.modules.agents.hermes.verification import verify_answer
from app.modules.agents.hermes.worker_shim import compact_turn_messages, inject_saved_plan
from app.modules.ai import thread_service


def test_calibrated_prompt_and_tool_descriptions() -> None:
    prompt = _assemble_turn_prompt("Estimate a value")
    assert "confidence high, medium, or low" in prompt
    assert "unconfirmed" in prompt
    for tool in (_BENCH_READ_TOOL, _BENCH_RUN_TOOL, _JARVIS_BROWSER_PAGE_TOOL,
                 _JARVIS_PAPER_PDF_READ_TOOL, _JARVIS_PAPER_SEARCH_TOOL,
                 _JARVIS_PATENT_READ_TOOL, _KNOWLEDGE_READ_TOOL, _TURN_PLAN_TOOL):
        assert "Prefer" in tool["description"]
        assert len(tool["description"]) <= 200


def test_verification_checks_and_clean_answer() -> None:
    assert verify_answer("Density: 1000 kg/m3; temperature 295 K")["status"] == "passed"
    result = verify_answer("Density: 100 kg/m3; temperature -2 K; components: 60% + 50%; 5 kgqq",
                           [{"density": {"value": 1000, "unit": "kg/m3"}}])
    kinds = {check["kind"] for check in result["checks"]}
    assert kinds == {"unit", "magnitude", "physical_bounds"}
    assert result["warning_count"] == len(result["checks"])
    assert all(len(check["summary"]) <= 200 and "/" not in check["summary"] for check in result["checks"])
    assert verify_answer("The output is 100 kg", [{"value": 1000, "unit": "kg"}])["warning_count"] == 1
    assert verify_answer("Gauge pressure is -1 bar")["status"] == "passed"
    assert verify_answer("Absolute pressure is -1 bar")["status"] == "warnings"


def test_compaction_preserves_anchors_and_saved_plan() -> None:
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "request"}]
    messages += [{"role": "assistant", "content": "old" * 2000}, {"role": "tool", "content": "old result" * 500}]
    messages += [{"role": "assistant", "content": "recent"}, {"role": "tool", "content": "latest result"}]
    result = compact_turn_messages(messages, max_chars=1000)
    assert result[0] == messages[0]
    assert messages[1] in result and messages[-2:] == result[-2:]
    assert "compacted" in str(result)
    assert len(json.dumps(result, ensure_ascii=False)) <= 1000
    saved = inject_saved_plan(result, {"steps": [{"id": "a", "title": "check", "status": "done"}], "findings": []})
    assert "[Active turn plan]" in str(saved)


def test_verification_event_projects_to_interaction() -> None:
    connection = _connection()
    connection.execute(
        "INSERT INTO ai_thread_interactions (id, thread_id, request_id, request_digest, interaction_index, "
        "user_text, assistant_text, assistant_text_truncated, flow_id, persistence_state, flow_state, "
        "attempt_count, created_at, updated_at) VALUES "
        "('inter-366', 'thread-362', 'req-366', 'digest', 1, 'Question', 'Answer', 0, 'flow-366', "
        "'captured', 'complete', 1, '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:05+00:00')"
    )
    row = connection.execute("SELECT * FROM ai_thread_interactions WHERE id = 'inter-366'").fetchone()
    assert thread_service._interaction_from_row(row, connection).verification_summary is None
    warning = verify_answer("Temperature -2 K")
    connection.execute(
        "INSERT INTO events VALUES ('evt-366', 'workspace-362', 'hermes.verification_pass', 'jarvis', "
        "'ai_thread_interaction', 'inter-366', ?, '2026-01-01T10:00:05+00:00')",
        (json.dumps(warning),),
    )
    assert thread_service._interaction_from_row(row, connection).verification_summary == warning
