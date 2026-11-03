import hashlib
import json
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.modules.secrets import service, storage
from app.modules.secrets.protection import WINDOWS_DPAPI_PROTECTOR_ID


class DeterministicProtector:
    protector_id = WINDOWS_DPAPI_PROTECTOR_ID

    def protect(self, *, secret_id: str, plaintext: bytes) -> bytes:
        key = hashlib.sha256(secret_id.encode()).digest()
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(plaintext))

    def unprotect(self, *, secret_id: str, ciphertext: bytes) -> bytes:
        return self.protect(secret_id=secret_id, plaintext=ciphertext)


class UnavailableProtector:
    protector_id = "unavailable-test-protector"

    def protect(self, *, secret_id: str, plaintext: bytes) -> bytes:
        del secret_id, plaintext
        raise AssertionError("unavailable protector must not write")

    def unprotect(self, *, secret_id: str, ciphertext: bytes) -> bytes:
        del secret_id, ciphertext
        raise AssertionError("unavailable protector must not decrypt")


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    for name in ("SCALEWAY_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(storage, "build_product_secret_protector", DeterministicProtector)

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client

    get_settings.cache_clear()


def _providers(client: TestClient) -> dict[str, dict[str, object]]:
    response = client.get("/ai/provider-settings")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "providers",
        "default_provider_id",
        "policy_mode",
        "external_calls_allowed",
        "blocking_reason",
        "monthly_api_budget_usd",
        "spend_month_to_date_usd",
    }
    return {provider["provider_id"]: provider for provider in payload["providers"]}


def _save_scaleway(client: TestClient, value: str = "sk-test-provider-settings-124") -> None:
    response = client.post("/secrets/scaleway/api-key", json={"api_key": value})
    assert response.status_code == 200
    assert value not in response.text


def _snapshot(
    *,
    global_actual: float = 0.0,
    global_reserved: float = 0.0,
    provider_cost: float = 0.0,
    provider_tokens: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        global_actual_cost_usd=global_actual,
        global_reserved_cost_usd=global_reserved,
        provider_actual_tokens=provider_tokens,
        provider_actual_cost_usd=provider_cost,
        provider_reserved_tokens=0,
        provider_reserved_cost_usd=0.0,
        today_actual_cost_usd=0.0,
        today_reserved_cost_usd=0.0,
    )


def test_provider_settings_uses_registry_identity_and_never_serializes_secret_refs(
    client: TestClient,
) -> None:
    response = client.get("/ai/provider-settings")
    assert response.status_code == 200
    text = response.text
    for forbidden in (
        "SCALEWAY_API_KEY",
        "DEEPSEEK_API_KEY",
        "GLM_API_KEY",
        "KIMI_API_KEY",
        "api_key_ref",
        "base_url",
        "masked_preview",
    ):
        assert forbidden not in text

    providers = {provider["provider_id"]: provider for provider in response.json()["providers"]}
    assert set(providers) == {"fake", "local_ollama", "local_llamacpp", "scaleway", "deepseek", "glm", "kimi"}
    assert providers["fake"]["credential"] == {
        "key_present": False,
        "effective_source": "not_required",
        "persisted_state": "not_supported",
        "reason_code": "credential_not_required",
    }
    assert providers["local_ollama"]["credential_capabilities"] == {
        "replace_persisted": False,
        "delete_persisted": False,
    }


def test_environment_managed_providers_mirror_execution_presence_semantics(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-value")
    monkeypatch.setenv("GLM_API_KEY", "   ")
    providers = _providers(client)

    for provider_id in ("deepseek", "glm"):
        assert providers[provider_id]["credential"] == {
            "key_present": True,
            "effective_source": "environment",
            "persisted_state": "not_supported",
            "reason_code": None,
        }
        assert providers[provider_id]["credential_capabilities"] == {
            "replace_persisted": False,
            "delete_persisted": False,
        }
    assert providers["kimi"]["credential"] == {
        "key_present": False,
        "effective_source": "absent",
        "persisted_state": "not_supported",
        "reason_code": "credential_environment_missing",
    }


def test_scaleway_projection_uses_real_secure_persisted_owner_state(client: TestClient) -> None:
    _save_scaleway(client)
    scaleway = _providers(client)["scaleway"]
    assert scaleway["credential"]["effective_source"] == "secure_persisted"
    assert scaleway["credential"]["persisted_state"] == "usable"
    assert scaleway["credential_capabilities"] == {
        "replace_persisted": True,
        "delete_persisted": True,
    }


def test_scaleway_truthy_environment_blocks_replace_even_when_owner_marks_it_invalid(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_scaleway(client)
    monkeypatch.setenv(storage.SCALEWAY_API_KEY_ENV_VAR, "invalid env value")
    scaleway = _providers(client)["scaleway"]
    assert scaleway["credential"]["effective_source"] == "invalid"
    assert scaleway["credential"]["persisted_state"] == "usable"
    assert scaleway["credential_capabilities"]["replace_persisted"] is False
    conflict = client.post("/secrets/scaleway/api-key", json={"api_key": "replacement-value"})
    assert conflict.status_code == 409


def test_scaleway_corrupted_envelope_remains_deletable(
    client: TestClient,
) -> None:
    _save_scaleway(client)
    path = storage.build_paths().scaleway_secret_file
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["ciphertext_sha256"] = "0" * 64
    path.write_text(json.dumps(envelope), encoding="utf-8")

    scaleway = _providers(client)["scaleway"]
    assert scaleway["credential"]["persisted_state"] == "corrupted"
    assert scaleway["credential_capabilities"]["delete_persisted"] is True
    deleted = client.delete("/secrets/scaleway/api-key")
    assert deleted.status_code == 200
    assert deleted.json()["persisted_state"] == "absent"


def test_scaleway_unavailable_envelope_remains_deletable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_scaleway(client)
    monkeypatch.setattr(storage, "build_product_secret_protector", UnavailableProtector)

    scaleway = _providers(client)["scaleway"]
    assert scaleway["credential"]["effective_source"] == "unknown"
    assert scaleway["credential"]["persisted_state"] == "unavailable"
    assert scaleway["credential_capabilities"] == {
        "replace_persisted": False,
        "delete_persisted": True,
    }
    deleted = client.delete("/secrets/scaleway/api-key")
    assert deleted.status_code == 200
    assert deleted.json()["persisted_state"] == "absent"


def test_scaleway_empty_store_with_unavailable_protector_advertises_no_mutation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "build_product_secret_protector", UnavailableProtector)
    scaleway = _providers(client)["scaleway"]
    assert scaleway["credential"]["persisted_state"] == "absent"
    assert scaleway["credential_capabilities"] == {
        "replace_persisted": False,
        "delete_persisted": False,
    }


def test_scaleway_non_regular_secret_path_is_not_advertised_as_mutable(
    client: TestClient,
) -> None:
    path = storage.build_paths().scaleway_secret_file
    path.mkdir(parents=True, exist_ok=True)
    scaleway = _providers(client)["scaleway"]
    assert scaleway["credential"]["persisted_state"] == "unavailable"
    assert scaleway["credential_capabilities"] == {
        "replace_persisted": False,
        "delete_persisted": False,
    }


def test_scaleway_symlink_secret_path_is_not_advertised_as_deletable(
    client: TestClient,
    tmp_path,
) -> None:
    path = storage.build_paths().scaleway_secret_file
    target = tmp_path / "outside-secret"
    target.write_text("not-a-secret", encoding="utf-8")
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable on this platform")

    scaleway = _providers(client)["scaleway"]
    assert scaleway["credential"]["persisted_state"] == "unavailable"
    assert scaleway["credential_capabilities"]["delete_persisted"] is False


def test_scaleway_unwritable_secret_directory_is_not_advertised_as_mutable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_scaleway(client)
    secrets_dir = storage.build_paths().secrets_dir
    original_access = service.os.access

    def guarded_access(path, mode):
        if path == secrets_dir:
            return False
        return original_access(path, mode)

    monkeypatch.setattr(service.os, "access", guarded_access)
    scaleway = _providers(client)["scaleway"]
    assert scaleway["credential_capabilities"] == {
        "replace_persisted": False,
        "delete_persisted": False,
    }


def test_provider_settings_does_not_invoke_provider_smoke(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.ai.gateway import AIGateway

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("provider smoke must not be invoked by provider settings")

    monkeypatch.setattr(AIGateway, "run_provider_smoke", fail)
    response = client.get("/ai/provider-settings")
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("actual_spend", "reserved_spend", "expected_spend"),
    ((6.0, 0.0, 6.0), (2.0, 3.1, 2.0)),
)
def test_status_and_provider_settings_share_canonical_global_budget_blocking(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    actual_spend: float,
    reserved_spend: float,
    expected_spend: float,
) -> None:
    from app.modules.ai import egress_persistence
    from app.modules.ai.gateway import AIGateway

    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-value")
    update = client.put(
        "/ai/settings",
        json={
            "monthly_api_budget_usd": 5.0,
            "paid_ai_enabled": True,
            "default_ai_provider": "deepseek",
        },
    )
    assert update.status_code == 200

    baseline = AIGateway().status().model_copy(
        update={
            "provider_id": "deepseek",
            "external_calls_allowed": True,
            "blocking_reason": None,
            "budget_status": "within_budget",
        }
    )
    monkeypatch.setattr(AIGateway, "status", lambda self: baseline)
    monkeypatch.setattr(
        egress_persistence,
        "_budget_snapshot",
        lambda *args, **kwargs: _snapshot(
            global_actual=actual_spend,
            global_reserved=reserved_spend,
            provider_cost=1000.0,
            provider_tokens=10_000_000,
        ),
    )

    status_response = client.get("/ai/status")
    provider_response = client.get("/ai/provider-settings")
    assert status_response.status_code == 200
    assert provider_response.status_code == 200

    status_payload = status_response.json()
    provider_payload = provider_response.json()
    deepseek = next(item for item in provider_payload["providers"] if item["provider_id"] == "deepseek")
    assert status_payload["spend_month_to_date_usd"] == expected_spend
    assert provider_payload["spend_month_to_date_usd"] == expected_spend
    assert status_payload["budget_status"] == "monthly_budget_exhausted"
    assert status_payload["external_calls_allowed"] is False
    assert provider_payload["external_calls_allowed"] is False
    assert deepseek["external_calls_allowed"] is False
    assert status_payload["blocking_reason"] == "global_monthly_cost_cap_exceeded"
    assert provider_payload["blocking_reason"] == "global_monthly_cost_cap_exceeded"
    assert deepseek["blocking_reason"] == "global_monthly_cost_cap_exceeded"


def test_non_active_provider_uses_owner_derived_blocking_state(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.ai import egress_persistence

    update = client.put(
        "/ai/settings",
        json={
            "monthly_api_budget_usd": 100.0,
            "paid_ai_enabled": True,
            "default_ai_provider": "fake",
            "provider_mode": "fake",
        },
    )
    assert update.status_code == 200
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-value")
    seen: list[str] = []

    def owner_blocker(connection, *, material, projection, registry, snapshot):
        del connection, projection, registry, snapshot
        seen.append(material.provider_id)
        if material.provider_id == "deepseek":
            return "provider_monthly_cost_cap_exceeded"
        return None

    monkeypatch.setattr(egress_persistence, "_hard_blocking_reason", owner_blocker)
    monkeypatch.setattr(egress_persistence, "_budget_snapshot", lambda *args, **kwargs: _snapshot())

    providers = _providers(client)
    assert "deepseek" in seen
    assert providers["deepseek"]["external_calls_allowed"] is False
    assert providers["deepseek"]["blocking_reason"] == "provider_monthly_cost_cap_exceeded"


def test_non_network_active_provider_preserves_canonical_global_spend(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.ai import egress_persistence

    monkeypatch.setattr(
        egress_persistence,
        "_budget_snapshot",
        lambda *args, **kwargs: _snapshot(global_actual=7.25),
    )
    payload = client.get("/ai/provider-settings").json()
    assert payload["default_provider_id"] == "fake"
    assert payload["spend_month_to_date_usd"] == 7.25


def test_provider_settings_read_never_expires_reservations_or_dispatches_provider(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.ai import egress_persistence
    from app.modules.ai.gateway import AIGateway

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("read projection crossed a mutation or provider-dispatch boundary")

    monkeypatch.setattr(egress_persistence, "_expire_stale_active_reservations", forbidden)
    monkeypatch.setattr(AIGateway, "run_provider_smoke", forbidden)
    monkeypatch.setattr(egress_persistence, "_hard_blocking_reason", lambda *args, **kwargs: None)
    monkeypatch.setattr(egress_persistence, "_budget_snapshot", lambda *args, **kwargs: _snapshot())

    response = client.get("/ai/provider-settings")
    assert response.status_code == 200



def _set_availability_settings_124(**values: object) -> None:
    from app.core.database import open_sqlite_connection

    allowed = {
        "policy_mode",
        "paid_ai_enabled",
        "monthly_api_budget_usd",
        "api_spend_month_to_date_usd",
        "provider_mode",
        "scaleway_enabled",
        "scaleway_monthly_token_cap",
        "scaleway_hard_stop_token_cap",
        "scaleway_input_tokens_month_to_date",
        "scaleway_output_tokens_month_to_date",
    }
    assert values and set(values) <= allowed
    assignments = ", ".join(f"{name} = ?" for name in values)
    with open_sqlite_connection() as connection:
        connection.execute(
            f"UPDATE ai_settings SET {assignments} WHERE id = 'default'",
            tuple(values.values()),
        )
        connection.commit()


def test_unknown_provider_availability_fails_closed_124(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.ai import egress_persistence

    _set_availability_settings_124(
        monthly_api_budget_usd=5.0,
        api_spend_month_to_date_usd=4.0,
    )
    monkeypatch.setattr(
        egress_persistence,
        "_budget_snapshot",
        lambda *args, **kwargs: _snapshot(global_actual=2.0, global_reserved=1.0),
    )

    projection = egress_persistence.project_egress_availability(
        "definitely-unknown-provider"
    )
    assert projection.available is False
    assert projection.blocking_reason == "provider_unknown"
    assert projection.global_actual_cost_usd == 2.0
    assert projection.global_reserved_cost_usd == 1.0
    assert projection.budget_exhausted is True


def test_read_availability_honors_configured_spend_floor_124(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.ai import egress_persistence

    _set_availability_settings_124(
        policy_mode="FAST_DEV",
        paid_ai_enabled=1,
        monthly_api_budget_usd=5.0,
        api_spend_month_to_date_usd=5.0,
    )
    monkeypatch.setattr(
        egress_persistence,
        "resolve_secret_ref",
        lambda ref: SimpleNamespace(key_present=True),
    )
    monkeypatch.setattr(
        egress_persistence,
        "_budget_snapshot",
        lambda *args, **kwargs: _snapshot(global_actual=1.25),
    )

    projection = egress_persistence.project_egress_availability("deepseek")
    assert projection.global_actual_cost_usd == 1.25
    assert projection.available is False
    assert projection.blocking_reason == "global_monthly_cost_cap_exceeded"
    assert projection.budget_exhausted is True


def test_read_availability_blocks_exact_provider_caps_124(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.ai import egress_persistence
    from app.modules.ai.provider_registry import load_default_provider_registry

    _set_availability_settings_124(
        policy_mode="FAST_DEV",
        paid_ai_enabled=1,
        monthly_api_budget_usd=1000.0,
        api_spend_month_to_date_usd=0.0,
    )
    monkeypatch.setattr(
        egress_persistence,
        "resolve_secret_ref",
        lambda ref: SimpleNamespace(key_present=True),
    )
    provider = load_default_provider_registry().providers["deepseek"]

    monkeypatch.setattr(
        egress_persistence,
        "_budget_snapshot",
        lambda *args, **kwargs: _snapshot(provider_tokens=provider.monthly_token_cap),
    )
    token_projection = egress_persistence.project_egress_availability("deepseek")
    assert token_projection.blocking_reason == "provider_monthly_token_cap_exceeded"
    assert token_projection.budget_exhausted is False

    monkeypatch.setattr(
        egress_persistence,
        "_budget_snapshot",
        lambda *args, **kwargs: _snapshot(provider_cost=provider.monthly_cost_cap_usd),
    )
    cost_projection = egress_persistence.project_egress_availability("deepseek")
    assert cost_projection.blocking_reason == "provider_monthly_cost_cap_exceeded"
    assert cost_projection.budget_exhausted is False


@pytest.mark.parametrize("provider_id", ("fake", "local_ollama"))
def test_non_network_status_overlays_independent_global_budget_124(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
) -> None:
    from app.modules.ai import egress_persistence
    from app.modules.ai.gateway import AIGateway

    _set_availability_settings_124(
        policy_mode="FAST_DEV",
        monthly_api_budget_usd=5.0,
        api_spend_month_to_date_usd=0.0,
        provider_mode=provider_id,
    )
    baseline = AIGateway().status().model_copy(
        update={
            "provider_id": provider_id,
            "external_calls_allowed": True,
            "blocking_reason": None,
            "budget_status": "within_budget",
        }
    )
    monkeypatch.setattr(AIGateway, "status", lambda self: baseline)
    monkeypatch.setattr(
        egress_persistence,
        "_budget_snapshot",
        lambda *args, **kwargs: _snapshot(global_actual=4.0, global_reserved=1.0),
    )

    projection = egress_persistence.project_egress_availability(provider_id)
    assert projection.available is True
    assert projection.blocking_reason is None
    assert projection.budget_exhausted is True

    status = client.get("/ai/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["provider_id"] == provider_id
    assert payload["external_calls_allowed"] is True
    assert payload["blocking_reason"] is None
    assert payload["budget_status"] == "monthly_budget_exhausted"


def test_read_availability_blocks_exact_scaleway_caps_124(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.ai import egress_persistence

    _set_availability_settings_124(
        policy_mode="FAST_DEV",
        paid_ai_enabled=1,
        monthly_api_budget_usd=1000.0,
        api_spend_month_to_date_usd=0.0,
        provider_mode="scaleway",
        scaleway_enabled=1,
        scaleway_monthly_token_cap=10,
        scaleway_hard_stop_token_cap=20,
        scaleway_input_tokens_month_to_date=0,
        scaleway_output_tokens_month_to_date=0,
    )
    monkeypatch.setattr(
        egress_persistence,
        "resolve_secret_ref",
        lambda ref: SimpleNamespace(key_present=True),
    )
    monkeypatch.setattr(
        egress_persistence,
        "_budget_snapshot",
        lambda *args, **kwargs: _snapshot(provider_tokens=10),
    )
    monthly_projection = egress_persistence.project_egress_availability("scaleway")
    assert monthly_projection.blocking_reason == "scaleway_monthly_token_cap_exceeded"
    assert monthly_projection.budget_exhausted is False

    _set_availability_settings_124(
        scaleway_monthly_token_cap=20,
        scaleway_hard_stop_token_cap=10,
    )
    hard_stop_projection = egress_persistence.project_egress_availability("scaleway")
    assert hard_stop_projection.blocking_reason == "scaleway_hard_stop_token_cap_exceeded"
    assert hard_stop_projection.budget_exhausted is False


def test_disabled_policy_blocks_credential_free_providers_124(client: TestClient) -> None:
    from app.modules.ai.egress_persistence import project_egress_availability

    _set_availability_settings_124(
        policy_mode="DISABLED",
        monthly_api_budget_usd=5.0,
        api_spend_month_to_date_usd=5.0,
    )
    for provider_id in ("fake", "local_ollama"):
        projection = project_egress_availability(provider_id)
        assert projection.available is False
        assert projection.blocking_reason == "ai_policy_disabled"
        assert projection.budget_exhausted is True


def test_default_policy_is_strict_ip_and_unclassified_prompts_are_not_external_eligible(client: TestClient) -> None:
    from app.modules.ai.egress_authority import authorize_prompt
    from app.modules.ai.settings import _policy_mode, get_ai_settings

    settings = get_ai_settings()
    assert settings.policy_mode == "STRICT_IP"
    assert _policy_mode("not-a-mode") == "STRICT_IP" and _policy_mode(None) == "STRICT_IP"
    authority = authorize_prompt(raw_prompt="Summarize our reactor layout tradeoffs.",
                                 task_kind="synthesis", policy_mode=settings.policy_mode)
    assert authority.result == "pause" and authority.reason_code == "prompt_classification_required"
    assert authority.effective_prompt is None
