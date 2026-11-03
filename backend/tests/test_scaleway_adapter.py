import hashlib
import json
from collections.abc import Iterator

import httpx
import pytest

from app.modules.ai.contracts import (
    AIPrivacyClass,
    AIProviderAdapter,
    AIProviderErrorCode,
    AIProviderHealth,
    AIProviderStatus,
    AIRequest,
    AITaskType,
    AIUsageSource,
    ModelRegistry,
    ProviderRegistry,
)
from app.modules.ai.providers.scaleway import (
    ScalewayChatResult,
    ScalewayNotConfiguredError,
    ScalewayProvider,
)
from app.modules.ai.providers.scaleway_adapter import (
    SCALEWAY_ADAPTER_INTERFACE,
    SCALEWAY_PROVIDER_ID,
    ScalewayProviderAdapter,
    scaleway_model_registry_entry,
    scaleway_provider_registry_entry,
)
from app.modules.secrets.protection import WINDOWS_DPAPI_PROTECTOR_ID


class DeterministicTestProtector:
    protector_id = WINDOWS_DPAPI_PROTECTOR_ID

    @staticmethod
    def _stream(secret_id: str, size: int) -> bytes:
        seed = hashlib.sha256(f"test-only:{secret_id}".encode()).digest()
        return (seed * ((size // len(seed)) + 1))[:size]

    def protect(self, *, secret_id: str, plaintext: bytes) -> bytes:
        stream = self._stream(secret_id, len(plaintext))
        return b"TEST1" + bytes(
            left ^ right
            for left, right in zip(plaintext, stream, strict=True)
        )

    def unprotect(self, *, secret_id: str, ciphertext: bytes) -> bytes:
        body = ciphertext.removeprefix(b"TEST1")
        stream = self._stream(secret_id, len(body))
        return bytes(
            left ^ right for left, right in zip(body, stream, strict=True)
        )


@pytest.fixture(autouse=True)
def isolated_secret_state(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.delenv("SCALEWAY_API_KEY", raising=False)
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("SCALEWAY_MODEL", raising=False)

    from app.core.config import get_settings
    from app.modules.secrets import storage

    monkeypatch.setattr(
        storage,
        "build_product_secret_protector",
        DeterministicTestProtector,
    )
    get_settings.cache_clear()
    yield
    storage.delete_persisted_scaleway_api_key()
    get_settings.cache_clear()


def test_scaleway_adapter_conforms_to_provider_neutral_interface() -> None:
    adapter: AIProviderAdapter = ScalewayProviderAdapter()

    assert adapter.provider_id == SCALEWAY_PROVIDER_ID
    assert adapter.health() == AIProviderHealth.unavailable
    assert adapter.list_models()[0].provider_id == SCALEWAY_PROVIDER_ID


def test_scaleway_adapter_registry_entries_are_static_and_provider_neutral(monkeypatch) -> None:
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    provider_entry = scaleway_provider_registry_entry()
    model_entry = scaleway_model_registry_entry()

    provider_registry = ProviderRegistry()
    adapter = ScalewayProviderAdapter()
    provider_registry.register_provider(adapter, provider_entry)
    model_registry = ModelRegistry()
    model_registry.register_model(model_entry)

    assert provider_entry.status == AIProviderStatus.enabled
    assert provider_entry.health == AIProviderHealth.healthy
    assert provider_registry.get_provider("scaleway") is adapter
    assert model_entry.model_id == ScalewayProvider.default_model
    assert AITaskType.smoke_console_test in model_entry.default_task_types
    assert AITaskType.smoke_test in model_entry.default_task_types
    assert model_registry.find_models(task_type=AITaskType.smoke_test) == [model_entry]


def test_neutral_console_request_maps_to_mocked_scaleway_call(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    def fake_completion(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> ScalewayChatResult:
        calls.append((prompt, estimated_output_tokens))
        return ScalewayChatResult(
            provider_name="scaleway",
            model="mock-model",
            mode="live_smoke_console",
            external_call_attempted=True,
            external_call_succeeded=True,
            response_text="Ciao.",
            reported_input_tokens=3,
            reported_output_tokens=4,
            reported_total_tokens=7,
            sanitized_metadata={
                "implementation": "mock",
                "usage_returned": True,
                "finish_reason": "stop",
                "Authorization": "Bearer should-not-leak",
                "api_key": "should-not-leak",
                "raw_response": {"secret": "should-not-leak"},
            },
        )

    monkeypatch.setattr(ScalewayProvider, "create_live_console_completion", fake_completion)
    monkeypatch.setattr(httpx, "post", _fail_if_network_called)

    response = ScalewayProviderAdapter().complete(
        AIRequest(
            task_type=AITaskType.smoke_console_test,
            privacy_class=AIPrivacyClass.public,
            prompt="ciao",
            max_output_tokens=12,
        )
    )

    assert calls == [("ciao", 12)]
    assert response.provider_id == "scaleway"
    assert response.model_id == "mock-model"
    assert response.text == "Ciao."
    assert response.finish_reason == "stop"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 4
    assert response.usage.total_tokens == 7
    assert response.usage.usage_source == AIUsageSource.actual
    assert response.raw_provider_metadata["adapter_interface"] == SCALEWAY_ADAPTER_INTERFACE
    assert response.raw_provider_metadata["reported_total_tokens"] == 7
    response_text = json.dumps(response.model_dump(mode="json"))
    assert "should-not-leak" not in response_text
    assert "Authorization" not in response_text
    assert "raw_response" not in response_text


def test_neutral_fixed_smoke_request_uses_fixed_smoke_completion(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    def fake_completion(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> ScalewayChatResult:
        calls.append((prompt, estimated_output_tokens))
        return ScalewayChatResult(
            provider_name="scaleway",
            model="mock-model",
            mode="live",
            external_call_attempted=True,
            external_call_succeeded=True,
            response_text="public synthetic classification",
            reported_input_tokens=5,
            reported_output_tokens=2,
            reported_total_tokens=7,
            sanitized_metadata={"implementation": "mock"},
        )

    monkeypatch.setattr(ScalewayProvider, "create_live_smoke_completion", fake_completion)

    response = ScalewayProviderAdapter().complete(
        AIRequest(
            task_type=AITaskType.smoke_test,
            privacy_class=AIPrivacyClass.internal,
            prompt="Generic engineering note.",
            max_output_tokens=9,
        )
    )

    assert calls == [("Generic engineering note.", 9)]
    assert response.text == "public synthetic classification"
    assert response.raw_provider_metadata["mode"] == "live"


def test_missing_scaleway_usage_falls_back_to_conservative_estimate(monkeypatch) -> None:
    def fake_completion(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> ScalewayChatResult:
        return ScalewayChatResult(
            provider_name="scaleway",
            model="mock-model",
            mode="live_smoke_console",
            external_call_attempted=True,
            external_call_succeeded=True,
            response_text="Hello.",
            reported_input_tokens=None,
            reported_output_tokens=None,
            reported_total_tokens=None,
            sanitized_metadata={"implementation": "mock", "usage_returned": False},
        )

    monkeypatch.setattr(ScalewayProvider, "create_live_console_completion", fake_completion)

    response = ScalewayProviderAdapter().complete(
        AIRequest(
            task_type=AITaskType.smoke_console_test,
            privacy_class=AIPrivacyClass.public,
            prompt="hello",
            max_output_tokens=12,
        )
    )

    assert response.usage.input_tokens == 2
    assert response.usage.output_tokens == 12
    assert response.usage.total_tokens == 14
    assert response.usage.usage_source == AIUsageSource.estimated
    assert response.raw_provider_metadata["reported_input_tokens"] is None
    assert response.raw_provider_metadata["reported_output_tokens"] is None


def test_partial_reported_scaleway_usage_is_marked_mixed(monkeypatch) -> None:
    def fake_completion(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> ScalewayChatResult:
        return ScalewayChatResult(
            provider_name="scaleway",
            model="mock-model",
            mode="live_smoke_console",
            external_call_attempted=True,
            external_call_succeeded=True,
            response_text="Hello.",
            reported_input_tokens=0,
            reported_output_tokens=None,
            reported_total_tokens=None,
            sanitized_metadata={"implementation": "mock", "usage_returned": True},
        )

    monkeypatch.setattr(ScalewayProvider, "create_live_console_completion", fake_completion)

    response = ScalewayProviderAdapter().complete(
        AIRequest(
            task_type=AITaskType.smoke_console_test,
            privacy_class=AIPrivacyClass.public,
            prompt="hello",
            max_output_tokens=12,
        )
    )

    assert response.usage.input_tokens == 0
    assert response.usage.output_tokens == 12
    assert response.usage.total_tokens == 12
    assert response.usage.usage_source == AIUsageSource.mixed


def test_missing_key_error_maps_to_safe_provider_error_without_key_leak(monkeypatch) -> None:
    def fake_completion(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> ScalewayChatResult:
        raise ScalewayNotConfiguredError("missing before transport")

    monkeypatch.setattr(ScalewayProvider, "create_live_console_completion", fake_completion)

    response = ScalewayProviderAdapter().complete(
        AIRequest(
            task_type=AITaskType.smoke_console_test,
            privacy_class=AIPrivacyClass.public,
            prompt="ciao",
            max_output_tokens=8,
        )
    )

    assert response.error is not None
    assert response.error.code == AIProviderErrorCode.provider_auth_missing
    assert response.blocked_reason == "scaleway_api_key_missing"
    assert response.raw_provider_metadata["adapter_interface"] == SCALEWAY_ADAPTER_INTERFACE
    assert response.raw_provider_metadata["external_call_attempted"] is False
    assert response.raw_provider_metadata["external_dispatch_state"] == "not_started"
    assert "missing before transport" not in response.model_dump_json()


def test_unsupported_task_type_does_not_call_scaleway(monkeypatch) -> None:
    def fail(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> ScalewayChatResult:
        raise AssertionError("Scaleway provider should not be called.")

    monkeypatch.setattr(ScalewayProvider, "create_live_console_completion", fail)
    monkeypatch.setattr(ScalewayProvider, "create_live_smoke_completion", fail)

    response = ScalewayProviderAdapter().complete(
        AIRequest(
            task_type=AITaskType.model_spec_draft,
            privacy_class=AIPrivacyClass.public,
            prompt="Draft a model spec.",
        )
    )

    assert response.error is not None
    assert response.error.code == AIProviderErrorCode.provider_bad_request
    assert response.blocked_reason == "unsupported_scaleway_task_type"
    assert response.raw_provider_metadata["external_call_attempted"] is False


def test_persisted_key_and_env_priority_remain_in_existing_provider_boundary(monkeypatch) -> None:
    from app.modules.secrets.storage import (
        get_effective_scaleway_api_key,
        set_persisted_scaleway_api_key,
    )

    set_persisted_scaleway_api_key("persisted-test-key")
    assert get_effective_scaleway_api_key().source == "secure_persisted"
    assert ScalewayProviderAdapter().health() == AIProviderHealth.healthy

    monkeypatch.setenv("SCALEWAY_API_KEY", "env-test-key")
    effective = get_effective_scaleway_api_key()
    assert effective.source == "env"
    assert effective.value == "env-test-key"
    assert ScalewayProviderAdapter().health() == AIProviderHealth.healthy


def _fail_if_network_called(*args, **kwargs):
    raise AssertionError("Automated Scaleway adapter tests must not call the network.")
