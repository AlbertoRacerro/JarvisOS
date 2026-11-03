from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.contracts import (
    AIProviderHealth,
    AIRequest,
    AIResponse,
    AIUsage,
    AIUsageSource,
)
from app.modules.ai.execution import run_ai_task
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.token_flow_service import get_flow

UNKNOWN_RECORD_OUTPUT = (
    "decision body before record\n"
    "```jarvis-records\n"
    '{"record_version":"jarvis_records_v0","records":['
    '{"record_kind":"decision","title":"Must not capture",'
    '"decision_text":"Unknown finish is not complete"}]}\n'
    "```"
)


@dataclass(frozen=True)
class _ResponseSpec:
    text: str
    finish_reason: str | None


class _SequenceAdapter:
    provider_id = "sequence"

    def __init__(self, *responses: _ResponseSpec) -> None:
        self._responses = list(responses)
        self.requests: list[AIRequest] = []

    def health(self) -> AIProviderHealth:
        return AIProviderHealth.healthy

    def list_models(self) -> list[object]:
        return []

    def complete(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("unexpected continuation adapter invocation")
        response = self._responses.pop(0)
        return AIResponse(
            provider_id=self.provider_id,
            model_id="sequence-v0",
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text=response.text,
            content=response.text,
            usage=AIUsage(
                provider_id=self.provider_id,
                model_id="sequence-v0",
                input_tokens=max(1, len(request.prompt or "") // 4),
                output_tokens=max(1, len(response.text) // 4),
                usage_source=AIUsageSource.estimated,
            ),
            finish_reason=response.finish_reason,
            safety_status="allowed",
        )

    def stream(self, request: AIRequest) -> object:
        raise NotImplementedError


@pytest.fixture
def initialized_database(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()
    yield
    get_settings.cache_clear()


def _binding(*, context_window_tokens: int = 4096) -> ProviderBinding:
    return ProviderBinding(
        route_class="local:sequence",
        provider_id="sequence",
        model_id="sequence-v0",
        requires_network=False,
        max_output_tokens=128,
        execution_class="synthetic",
        context_window_tokens=context_window_tokens,
    )


def _attempt_rows(flow_id: str) -> list[dict[str, object]]:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, flow_attempt_index, parent_attempt_id, continuation_index,
                   normalized_finish_reason, output_digest, status, input_tokens,
                   output_tokens
            FROM ai_jobs
            WHERE flow_id = ?
            ORDER BY flow_attempt_index
            """,
            (flow_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_exact_length_creates_one_child_and_returns_assembled_output(
    initialized_database,
) -> None:
    adapter = _SequenceAdapter(
        _ResponseSpec("alpha ", "length"),
        _ResponseSpec("omega", "stop"),
    )

    outcome = run_ai_task(
        user_prompt="Produce the bounded answer.",
        task_kind="synthesis",
        route_class="local:sequence",
        max_output_tokens=64,
        adapters={"sequence": adapter},
        bindings={"local:sequence": _binding()},
    )

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text == "alpha omega"
    assert len(adapter.requests) == 2
    assert "VALIDATED_PARTIAL_OUTPUT" in (adapter.requests[1].prompt or "")
    assert "alpha " in (adapter.requests[1].prompt or "")

    flow = get_flow(str(outcome.flow_id))
    assert flow["state"] == "complete"
    assert flow["attempt_count"] == 2
    assert flow["continuation_count"] == 1
    assert flow["final_output_digest"] == canonical_digest({"text": "alpha omega"})

    attempts = _attempt_rows(str(outcome.flow_id))
    assert [row["flow_attempt_index"] for row in attempts] == [0, 1]
    assert attempts[1]["parent_attempt_id"] == attempts[0]["id"]
    assert attempts[1]["continuation_index"] == 1


def test_guard_zero_preserves_first_segment_without_hidden_retry(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_settings SET max_direct_continuations = 0 WHERE id = 'default'"
        )
        connection.commit()
    adapter = _SequenceAdapter(_ResponseSpec("bounded partial", "length"))

    outcome = run_ai_task(
        user_prompt="Produce the bounded answer.",
        task_kind="synthesis",
        route_class="local:sequence",
        max_output_tokens=64,
        adapters={"sequence": adapter},
        bindings={"local:sequence": _binding()},
    )

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text == "bounded partial"
    assert len(adapter.requests) == 1
    flow = get_flow(str(outcome.flow_id))
    assert flow["state"] == "partial_terminal"
    assert flow["terminal_reason"] == "continuation_guard_exhausted"
    assert flow["attempt_count"] == 1
    assert flow["continuation_count"] == 0


def test_nonstop_child_finish_is_partial_and_not_complete(
    initialized_database,
) -> None:
    adapter = _SequenceAdapter(
        _ResponseSpec("first ", "length"),
        _ResponseSpec("filtered", "content_filter"),
    )

    outcome = run_ai_task(
        user_prompt="Produce the bounded answer.",
        task_kind="decision_support",
        route_class="local:sequence",
        max_output_tokens=64,
        adapters={"sequence": adapter},
        bindings={"local:sequence": _binding()},
    )

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text == "first filtered"
    assert outcome.proposed_record_ids in (None, [])
    flow = get_flow(str(outcome.flow_id))
    assert flow["state"] == "partial_terminal"
    assert flow["terminal_reason"] == "continuation_finish_content_filter"
    assert flow["attempt_count"] == 2
    assert flow["continuation_count"] == 1


def test_empty_continuation_response_preserves_partial_output_without_500(
    initialized_database,
) -> None:
    adapter = _SequenceAdapter(
        _ResponseSpec("visible prefix", "length"),
        _ResponseSpec("", "length"),
    )

    outcome = run_ai_task(
        user_prompt="Produce the bounded answer.",
        task_kind="synthesis",
        route_class="local:sequence",
        max_output_tokens=64,
        adapters={"sequence": adapter},
        bindings={"local:sequence": _binding()},
    )

    assert outcome.status == "provider_error"
    assert outcome.error_type == "output_budget_exhausted"
    assert outcome.response is not None
    assert outcome.response.text == "visible prefix"
    assert len(adapter.requests) == 2

    flow = get_flow(str(outcome.flow_id))
    assert flow["state"] == "partial_terminal"
    assert flow["terminal_reason"] == "continuation_output_budget_exhausted"
    assert flow["attempt_count"] == 2
    assert flow["continuation_count"] == 1

    attempts = _attempt_rows(str(outcome.flow_id))
    assert attempts[1]["output_digest"] is None
    assert attempts[1]["status"] == "provider_error"
    assert attempts[1]["normalized_finish_reason"] == "length"
    assert attempts[1]["output_tokens"] == 1


def test_fresh_context_capacity_check_blocks_before_second_adapter_call(
    initialized_database,
) -> None:
    adapter = _SequenceAdapter(_ResponseSpec("partial", "length"))

    outcome = run_ai_task(
        user_prompt="x" * 400,
        task_kind="synthesis",
        route_class="local:sequence",
        max_output_tokens=64,
        adapters={"sequence": adapter},
        bindings={
            "local:sequence": _binding(context_window_tokens=100),
        },
    )

    assert outcome.response is not None
    assert outcome.response.text == "partial"
    assert len(adapter.requests) == 1
    flow = get_flow(str(outcome.flow_id))
    assert flow["state"] == "partial_terminal"
    assert flow["terminal_reason"] == "continuation_context_capacity_exceeded"
    assert flow["attempt_count"] == 1
    assert flow["continuation_count"] == 0


def test_unknown_finish_is_partial_and_does_not_capture_records(
    initialized_database,
) -> None:
    adapter = _SequenceAdapter(
        _ResponseSpec(UNKNOWN_RECORD_OUTPUT, None),
    )

    outcome = run_ai_task(
        user_prompt="Return one candidate decision.",
        task_kind="decision_support",
        route_class="local:sequence",
        max_output_tokens=64,
        adapters={"sequence": adapter},
        bindings={"local:sequence": _binding()},
        workspace_id="main",
    )

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text == UNKNOWN_RECORD_OUTPUT
    assert outcome.proposed_record_ids in (None, [])
    assert len(adapter.requests) == 1
    flow = get_flow(str(outcome.flow_id))
    assert flow["state"] == "partial_terminal"
    assert flow["terminal_reason"] == "finish_unknown"

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        proposals = connection.execute(
            "SELECT COUNT(*) AS count FROM decisions WHERE origin = 'ai_proposed'"
        ).fetchone()
    assert proposals["count"] == 0


def test_length_finish_without_visible_text_is_budget_exhausted_not_unavailable(
    initialized_database,
) -> None:
    adapter = _SequenceAdapter(_ResponseSpec("", "length"))

    outcome = run_ai_task(
        user_prompt="Produce the bounded answer.",
        task_kind="synthesis",
        route_class="local:sequence",
        max_output_tokens=64,
        adapters={"sequence": adapter},
        bindings={"local:sequence": _binding()},
    )

    assert outcome.status == "provider_error"
    assert outcome.error_type == "output_budget_exhausted"
    flow = get_flow(str(outcome.flow_id))
    assert flow["state"] == "failed_terminal"
    assert flow["terminal_reason"] == "output_budget_exhausted"
    attempts = _attempt_rows(str(outcome.flow_id))
    assert attempts[0]["normalized_finish_reason"] == "length"
