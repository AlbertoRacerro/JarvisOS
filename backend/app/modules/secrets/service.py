import os
from dataclasses import dataclass
from pathlib import Path

from app.core.database import open_sqlite_connection
from app.modules.events.service import log_event
from app.modules.secrets.models import ScalewaySecretStatus
from app.modules.secrets.protection import WINDOWS_DPAPI_PROTECTOR_ID
from app.modules.secrets.storage import (
    ENV_SOURCE,
    PERSISTED_CORRUPTED,
    PERSISTED_SOURCE,
    PERSISTED_UNAVAILABLE,
    SCALEWAY_API_KEY_ENV_VAR,
    EffectiveSecret,
    ScalewaySecretStore,
    SecretEnvironmentOverrideError,
    SecretStorageError,
    SecretStorageUnavailableError,
    delete_persisted_scaleway_api_key,
    get_effective_scaleway_api_key,
    normalize_scaleway_api_key,
    resolve_secret_ref,
    set_persisted_scaleway_api_key,
)


@dataclass(frozen=True)
class ScalewaySecretMutationCapabilities:
    replace_persisted: bool
    delete_persisted: bool


def read_scaleway_secret_status(*, log_status_check: bool = False) -> ScalewaySecretStatus:
    status = _status_from_secret(get_effective_scaleway_api_key())
    if log_status_check:
        _log_secret_event(
            "ScalewayApiKeyStatusChecked",
            "scaleway_key_status_checked",
            status,
        )
    return status


def read_scaleway_secret_mutation_capabilities() -> ScalewaySecretMutationCapabilities:
    """Project operation-specific capabilities from the existing secure-store owner."""
    store = ScalewaySecretStore()
    replace_persisted = (
        not bool(os.getenv(SCALEWAY_API_KEY_ENV_VAR))
        and store.protector.protector_id == WINDOWS_DPAPI_PROTECTOR_ID
        and _store_mutation_path_available(store, require_existing=False)
    )
    delete_persisted = _store_mutation_path_available(store, require_existing=True)
    return ScalewaySecretMutationCapabilities(
        replace_persisted=replace_persisted,
        delete_persisted=delete_persisted,
    )


def resolve_provider_secret_ref(secret_ref: str | None) -> EffectiveSecret:
    """Expose the accepted execution-time resolver without duplicating credential semantics."""
    return resolve_secret_ref(secret_ref)


def save_scaleway_api_key(api_key: str | None) -> ScalewaySecretStatus:
    if os.getenv(SCALEWAY_API_KEY_ENV_VAR):
        raise SecretEnvironmentOverrideError("secret_environment_override")
    normalized = normalize_scaleway_api_key(api_key)
    secret = set_persisted_scaleway_api_key(normalized)
    status = _status_from_secret(secret)
    _log_secret_event("ScalewayApiKeySaved", "scaleway_key_saved", status)
    return status


def delete_scaleway_api_key() -> ScalewaySecretStatus:
    secret = delete_persisted_scaleway_api_key()
    status = _status_from_secret(secret)
    _log_secret_event("ScalewayApiKeyDeleted", "scaleway_key_deleted", status)
    return status


def _store_mutation_path_available(
    store: ScalewaySecretStore,
    *,
    require_existing: bool,
) -> bool:
    path = store.paths.scaleway_secret_file
    if require_existing and not os.path.lexists(path):
        return False
    try:
        store._assert_storage_path(write=True)
    except SecretStorageUnavailableError:
        return False
    return _nearest_existing_directory_is_writable(store.paths.secrets_dir)


def _nearest_existing_directory_is_writable(directory: Path) -> bool:
    candidate = directory
    while not os.path.lexists(candidate) and candidate.parent != candidate:
        candidate = candidate.parent
    try:
        return candidate.is_dir() and os.access(candidate, os.W_OK)
    except OSError:
        return False


def _status_from_secret(secret: EffectiveSecret) -> ScalewaySecretStatus:
    if secret.source == ENV_SOURCE and secret.key_present:
        effective_source = "environment"
        storage_mode = "environment"
    elif secret.source == PERSISTED_SOURCE and secret.key_present:
        effective_source = "secure_persisted"
        storage_mode = "secure_persisted"
    else:
        effective_source = "none"
        if secret.persisted_state == PERSISTED_CORRUPTED:
            storage_mode = "corrupted"
        elif secret.persisted_state == PERSISTED_UNAVAILABLE:
            storage_mode = "unavailable"
        else:
            storage_mode = "none"

    return ScalewaySecretStatus(
        key_present=secret.key_present,
        source=secret.source,
        effective_source=effective_source,
        persisted_state=secret.persisted_state,
        masked_preview=None,
        last_updated_at=secret.last_updated_at,
        storage_mode=storage_mode,
        reason_code=secret.reason_code,
    )


def _log_secret_event(
    event_type: str,
    action: str,
    status: ScalewaySecretStatus,
) -> None:
    with open_sqlite_connection() as connection:
        log_event(
            connection,
            event_type=event_type,
            actor="local-user",
            target_type="ScalewayApiKey",
            target_id=None,
            workspace_id=None,
            payload={
                "action": action,
                "key_present": status.key_present,
                "source": status.source,
                "effective_source": status.effective_source,
                "persisted_state": status.persisted_state,
                "storage_mode": status.storage_mode,
                "reason_code": status.reason_code,
                "last_updated_at": status.last_updated_at,
            },
        )
        connection.commit()


__all__ = [
    "ScalewaySecretMutationCapabilities",
    "SecretEnvironmentOverrideError",
    "SecretStorageError",
    "SecretStorageUnavailableError",
    "delete_scaleway_api_key",
    "normalize_scaleway_api_key",
    "read_scaleway_secret_mutation_capabilities",
    "read_scaleway_secret_status",
    "resolve_provider_secret_ref",
    "save_scaleway_api_key",
]
