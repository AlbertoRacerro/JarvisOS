"""Deterministic fake provider on the provider-neutral adapter interface.

This is distinct from the legacy FakeModelingProvider (providers/fake.py), which
implements the older base.AIProvider/ModelingDraft interface. This adapter
implements contracts.AIProviderAdapter so the positive execution spine
(run_ai_task) can be exercised end-to-end with no network and no secrets.
"""

from __future__ import annotations

from app.modules.ai.contracts import (
    AIModelCapability,
    AIPrivacyClass,
    AIProviderHealth,
    AIRequest,
    AIResponse,
    AIUsage,
    AIUsageSource,
    ModelRegistryEntry,
)

FAKE_PROVIDER_ID = "fake"
FAKE_MODEL_ID = "fake-deterministic-v1"


def _prompt_text(request: AIRequest) -> str:
    if request.prompt is not None:
        return request.prompt
    return "\n".join(f"{message.role}: {message.content}" for message in request.messages).strip()


class FakeProviderAdapter:
    provider_id = FAKE_PROVIDER_ID

    def health(self) -> AIProviderHealth:
        return AIProviderHealth.healthy

    def list_models(self) -> list[ModelRegistryEntry]:
        return [
            ModelRegistryEntry(
                model_id=FAKE_MODEL_ID,
                provider_id=FAKE_PROVIDER_ID,
                provider_model_name=FAKE_MODEL_ID,
                display_name="Fake deterministic",
                enabled=True,
                capabilities={AIModelCapability.chat_text, AIModelCapability.low_cost},
                allowed_privacy_classes={AIPrivacyClass.public, AIPrivacyClass.internal},
                notes="Deterministic local fake for tests and dry-runs; no network.",
            )
        ]

    def complete(self, request: AIRequest) -> AIResponse:
        prompt = _prompt_text(request)
        text = f"[fake:{request.task_type.value}] {prompt[:200]}" if prompt else f"[fake:{request.task_type.value}] (empty)"
        input_tokens = max(1, len(prompt) // 4)
        output_tokens = max(1, len(text) // 4)
        return AIResponse(
            provider_id=FAKE_PROVIDER_ID,
            model_id=FAKE_MODEL_ID,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text=text,
            content=text,
            usage=AIUsage(
                provider_id=FAKE_PROVIDER_ID,
                model_id=FAKE_MODEL_ID,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usage_source=AIUsageSource.estimated,
            ),
            finish_reason="stop",
            safety_status="allowed",
        )

    def stream(self, request: AIRequest) -> object:
        raise NotImplementedError("Fake adapter streaming is not implemented.")
