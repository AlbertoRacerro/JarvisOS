from __future__ import annotations

from app.modules.ai.contracts import AIRequest, AIResponse, AITaskType, AIUsage
from app.modules.ai.execution_types import ProviderBinding


class _LengthAdapter:
    provider_id = "fake"

    def complete(self, request: AIRequest) -> AIResponse:
        return AIResponse(
            provider_id="fake",
            model_id="fake-modeling-draft-v1",
            request_id=request.request_id,
            text="Truncated local answer.",
            content="Truncated local answer.",
            finish_reason="length",
            usage=AIUsage(
                provider_id="fake",
                model_id="fake-modeling-draft-v1",
                input_tokens=3,
                output_tokens=4,
            ),
        )

    def health(self):  # pragma: no cover - protocol method unused
        raise NotImplementedError

    def list_models(self):  # pragma: no cover - protocol method unused
        return []

    def stream(self, request: AIRequest):  # pragma: no cover - protocol method unused
        raise NotImplementedError


def test_local_length_response_retries_until_guard_then_partial(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "local-partial"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.database import initialize_database, open_sqlite_connection
    from app.modules.ai.execution import run_ai_task
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()
    binding = ProviderBinding(
        route_class="local:fake",
        provider_id="fake",
        model_id="fake-modeling-draft-v1",
        requires_network=False,
        max_output_tokens=64,
        execution_class="synthetic",
        context_window_tokens=4096,
    )

    outcome = run_ai_task(
        user_prompt="Return a bounded answer.",
        task_kind=AITaskType.synthesis.value,
        route_class=binding.route_class,
        max_output_tokens=32,
        adapters={"fake": _LengthAdapter()},
        bindings={binding.route_class: binding},
    )

    assert outcome.status == "success"
    assert outcome.proposed_record_ids is None
    with open_sqlite_connection() as connection:
        flow = connection.execute(
            "SELECT state, terminal_reason, terminal_attempt_id FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert flow["state"] == "partial_terminal"
    assert flow["terminal_reason"] == "continuation_guard_exhausted"
    assert flow["terminal_attempt_id"] == outcome.ledger_id
