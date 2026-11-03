from __future__ import annotations

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.contracts import AIRequest, AIResponse, AIUsage
from app.modules.ai.execution import run_ai_task
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings


class CountingExternalAdapter:
    provider_id = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: AIRequest) -> AIResponse:
        self.calls += 1
        return AIResponse(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text="Unexpected provider call.",
            content="Unexpected provider call.",
            usage=AIUsage(
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
                input_tokens=1,
                output_tokens=1,
            ),
            finish_reason="stop",
            safety_status="allowed",
        )

    def health(self):  # pragma: no cover - protocol method unused here
        raise NotImplementedError

    def list_models(self):  # pragma: no cover - protocol method unused here
        raise NotImplementedError

    def stream(self, request: AIRequest):  # pragma: no cover - protocol method unused
        raise NotImplementedError


def _bootstrap(monkeypatch) -> None:
    initialize_database()
    ensure_ai_settings()
    update_ai_settings(
        AISettingsUpdate(
            policy_mode="FAST_DEV",
            monthly_api_budget_usd=100.0,
            paid_ai_enabled=True,
            provider_mode="deepseek",
        )
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")


def _binding() -> ProviderBinding:
    return ProviderBinding(
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        requires_network=True,
        max_output_tokens=2048,
    )


def test_run_ai_task_external_route_requires_egress_ticket_before_adapter(monkeypatch):
    _bootstrap(monkeypatch)
    adapter = CountingExternalAdapter()

    outcome = run_ai_task(
        user_prompt="Explain a generic pump sizing method.",
        task_kind="general",
        route_class="external:cheap",
        max_output_tokens=128,
        adapters={"deepseek": adapter},
        bindings={"external:cheap": _binding()},
    )

    assert outcome.status == "validation_error"
    assert outcome.egress_reason_code == "confirmation_required"
    assert outcome.egress_ticket_id is not None
    assert outcome.egress_packet_digest is not None
    assert adapter.calls == 0
    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT status FROM ai_jobs WHERE id = ?",
            (outcome.ledger_id,),
        ).fetchone()
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) AS count FROM {table}"
            ).fetchone()["count"]
            for table in (
                "egress_packets",
                "egress_decisions",
                "egress_confirmation_tickets",
                "egress_budget_reservations",
            )
        }
    assert job["status"] == "validation_error"
    assert counts == {
        "egress_packets": 1,
        "egress_decisions": 1,
        "egress_confirmation_tickets": 1,
        "egress_budget_reservations": 0,
    }


def test_run_ai_task_secret_external_prompt_is_prepacket_denied(monkeypatch):
    _bootstrap(monkeypatch)
    adapter = CountingExternalAdapter()
    secret = "api_key=super-secret-value"

    outcome = run_ai_task(
        user_prompt=secret,
        task_kind="general",
        route_class="external:cheap",
        max_output_tokens=128,
        adapters={"deepseek": adapter},
        bindings={"external:cheap": _binding()},
    )

    assert outcome.status == "config_error"
    assert outcome.egress_reason_code == "prompt_secret_detected"
    assert outcome.egress_packet_digest is None
    assert adapter.calls == 0
    with open_sqlite_connection() as connection:
        decision = connection.execute("SELECT * FROM egress_decisions").fetchone()
        packet_count = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_packets"
        ).fetchone()["count"]
    assert decision["packet_id"] is None
    assert decision["packet_digest"] is None
    assert secret not in str(dict(decision))
    assert packet_count == 0


def test_run_ai_task_local_route_does_not_enter_egress_boundary():
    initialize_database()

    outcome = run_ai_task(
        user_prompt="Local-only generic task.",
        task_kind="general",
        route_class="local:fake",
    )

    assert outcome.status == "success"
    assert outcome.egress_decision_id is None
    assert outcome.egress_packet_digest is None
    with open_sqlite_connection() as connection:
        decision_count = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_decisions"
        ).fetchone()["count"]
        packet_count = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_packets"
        ).fetchone()["count"]
    assert decision_count == 0
    assert packet_count == 0
