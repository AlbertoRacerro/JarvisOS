from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from app.core.paths import JarvisPaths, build_paths
from app.modules.secrets.protection import (
    WINDOWS_DPAPI_PROTECTOR_ID,
    SecretProtectionOperationError,
    SecretProtectionUnavailableError,
    SecretProtector,
    build_product_secret_protector,
)

SCALEWAY_API_KEY_ENV_VAR: Final = "SCALEWAY_API_KEY"
SCALEWAY_SECRET_ID: Final = "scaleway_api_key"
ENV_SOURCE: Final = "env"
PERSISTED_SOURCE: Final = "secure_persisted"
NONE_SOURCE: Final = "none"
PERSISTED_ABSENT: Final = "absent"
PERSISTED_USABLE: Final = "usable"
PERSISTED_CORRUPTED: Final = "corrupted"
PERSISTED_UNAVAILABLE: Final = "unavailable"
MAX_SCALEWAY_API_KEY_LENGTH: Final = 4096
MAX_SECRET_FILE_BYTES: Final = 64 * 1024
MAX_SYSTEMD_CREDENTIAL_BYTES: Final = 4096
_INNER_SCHEMA: Final = 1
_ENVELOPE_SCHEMA: Final = 1
_MUTATION_LOCK = threading.RLock()


class SecretStorageError(RuntimeError):
    """Base error for safe persisted-secret failures."""


class SecretStorageUnavailableError(SecretStorageError):
    """Raised when secure persistence cannot be used safely."""


class SecretStorageCorruptedError(SecretStorageError):
    """Raised when the canonical persisted artifact is malformed or tampered."""


class SecretEnvironmentOverrideError(SecretStorageError):
    """Raised when an environment credential prevents persisted mutation."""


@dataclass(frozen=True, repr=False)
class PersistedSecret:
    value: str | None
    state: str
    last_updated_at: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True, repr=False)
class EffectiveSecret:
    value: str | None
    source: str
    persisted_state: str = PERSISTED_ABSENT
    last_updated_at: str | None = None
    reason_code: str | None = None

    @property
    def key_present(self) -> bool:
        return bool(self.value)


class ScalewaySecretStore:
    def __init__(
        self,
        *,
        paths: JarvisPaths | None = None,
        protector: SecretProtector | None = None,
    ) -> None:
        self.paths = paths or build_paths()
        self.protector = protector or build_product_secret_protector()

    def read(self) -> PersistedSecret:
        path = self.paths.scaleway_secret_file
        if not _path_lexists(path):
            return PersistedSecret(value=None, state=PERSISTED_ABSENT)
        try:
            self._assert_storage_path(write=False)
            raw = _bounded_read(path)
            value = self._decode_envelope(raw)
            return PersistedSecret(
                value=value,
                state=PERSISTED_USABLE,
                last_updated_at=_mtime_iso(path),
            )
        except SecretStorageCorruptedError as exc:
            return PersistedSecret(
                value=None,
                state=PERSISTED_CORRUPTED,
                last_updated_at=_mtime_iso(path),
                reason_code=str(exc),
            )
        except (
            OSError,
            SecretProtectionUnavailableError,
            SecretStorageUnavailableError,
        ) as exc:
            return PersistedSecret(
                value=None,
                state=PERSISTED_UNAVAILABLE,
                last_updated_at=_mtime_iso(path),
                reason_code=_safe_reason(exc, "secret_storage_unavailable"),
            )

    def save(self, api_key: str) -> PersistedSecret:
        normalized = normalize_scaleway_api_key(api_key)
        if os.getenv(SCALEWAY_API_KEY_ENV_VAR):
            raise SecretEnvironmentOverrideError("secret_environment_override")
        with _MUTATION_LOCK:
            self._assert_storage_path(write=True)
            inner = _canonical_json_bytes(
                {
                    "payload_schema": _INNER_SCHEMA,
                    "secret_id": SCALEWAY_SECRET_ID,
                    "value": normalized,
                    "value_sha256": hashlib.sha256(
                        normalized.encode("utf-8")
                    ).hexdigest(),
                }
            )
            try:
                ciphertext = self.protector.protect(
                    secret_id=SCALEWAY_SECRET_ID,
                    plaintext=inner,
                )
            except SecretProtectionUnavailableError as exc:
                raise SecretStorageUnavailableError(
                    "secret_storage_unavailable"
                ) from exc
            except SecretProtectionOperationError as exc:
                raise SecretStorageError("secret_protect_failed") from exc
            envelope = _canonical_json_bytes(
                {
                    "envelope_schema": _ENVELOPE_SCHEMA,
                    "secret_id": SCALEWAY_SECRET_ID,
                    "protector_id": WINDOWS_DPAPI_PROTECTOR_ID,
                    "ciphertext_base64": base64.b64encode(ciphertext).decode("ascii"),
                    "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
                }
            )
            if len(envelope) > MAX_SECRET_FILE_BYTES:
                raise SecretStorageError("secret_file_too_large")
            self._replace_atomically(envelope, expected_value=normalized)
            return PersistedSecret(
                value=normalized,
                state=PERSISTED_USABLE,
                last_updated_at=_mtime_iso(self.paths.scaleway_secret_file),
            )

    def delete(self) -> PersistedSecret:
        with _MUTATION_LOCK:
            path = self.paths.scaleway_secret_file
            if not _path_lexists(path):
                return PersistedSecret(value=None, state=PERSISTED_ABSENT)
            self._assert_storage_path(write=True)
            try:
                path.unlink()
            except OSError as exc:
                raise SecretStorageError("secret_delete_failed") from exc
            return PersistedSecret(value=None, state=PERSISTED_ABSENT)

    def _decode_envelope(self, raw: bytes) -> str:
        envelope = _parse_json_object(raw, "secret_storage_corrupted")
        expected_keys = {
            "envelope_schema",
            "secret_id",
            "protector_id",
            "ciphertext_base64",
            "ciphertext_sha256",
        }
        if set(envelope) != expected_keys:
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        if envelope["envelope_schema"] != _ENVELOPE_SCHEMA:
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        if envelope["secret_id"] != SCALEWAY_SECRET_ID:
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        if envelope["protector_id"] != WINDOWS_DPAPI_PROTECTOR_ID:
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        if not isinstance(envelope["ciphertext_base64"], str):
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        if not _is_sha256(envelope["ciphertext_sha256"]):
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        try:
            ciphertext = base64.b64decode(
                envelope["ciphertext_base64"],
                validate=True,
            )
        except (ValueError, TypeError) as exc:
            raise SecretStorageCorruptedError(
                "secret_storage_corrupted"
            ) from exc
        if not ciphertext:
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        if hashlib.sha256(ciphertext).hexdigest() != envelope["ciphertext_sha256"]:
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        if self.protector.protector_id != WINDOWS_DPAPI_PROTECTOR_ID:
            raise SecretStorageUnavailableError("secret_storage_unavailable")
        try:
            plaintext = self.protector.unprotect(
                secret_id=SCALEWAY_SECRET_ID,
                ciphertext=ciphertext,
            )
        except SecretProtectionUnavailableError as exc:
            raise SecretStorageUnavailableError(
                "secret_storage_unavailable"
            ) from exc
        except SecretProtectionOperationError as exc:
            raise SecretStorageUnavailableError(
                "secret_unprotect_unavailable"
            ) from exc
        payload = _parse_json_object(plaintext, "secret_storage_corrupted")
        expected_payload_keys = {
            "payload_schema",
            "secret_id",
            "value",
            "value_sha256",
        }
        if set(payload) != expected_payload_keys:
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        if payload["payload_schema"] != _INNER_SCHEMA:
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        if payload["secret_id"] != SCALEWAY_SECRET_ID:
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        value = payload["value"]
        digest = payload["value_sha256"]
        if not isinstance(value, str) or not _is_sha256(digest):
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        if hashlib.sha256(value.encode("utf-8")).hexdigest() != digest:
            raise SecretStorageCorruptedError("secret_storage_corrupted")
        try:
            return normalize_scaleway_api_key(value)
        except ValueError as exc:
            raise SecretStorageCorruptedError(
                "secret_storage_corrupted"
            ) from exc

    def _replace_atomically(self, encoded: bytes, *, expected_value: str) -> None:
        secrets_dir = self.paths.secrets_dir
        try:
            secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise SecretStorageUnavailableError(
                "secret_storage_unavailable"
            ) from exc
        self._assert_storage_path(write=True)
        file_descriptor: int | None = None
        temporary: Path | None = None
        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".scaleway-api-key-",
                suffix=".tmp",
                dir=secrets_dir,
            )
            temporary = Path(temporary_name)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            with os.fdopen(file_descriptor, "wb", closefd=True) as handle:
                file_descriptor = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_regular_file(temporary)
            if _bounded_read(temporary) != encoded:
                raise SecretStorageError("secret_write_verify_failed")
            if self._decode_envelope(encoded) != expected_value:
                raise SecretStorageError("secret_write_verify_failed")
            os.replace(temporary, self.paths.scaleway_secret_file)
            temporary = None
        except SecretStorageError:
            raise
        except OSError as exc:
            raise SecretStorageError("secret_write_failed") from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _assert_storage_path(self, *, write: bool) -> None:
        data_root = self.paths.data_root
        secrets_dir = self.paths.secrets_dir
        canonical = self.paths.scaleway_secret_file
        _assert_within_root(data_root, secrets_dir)
        _assert_within_root(data_root, canonical)
        if _path_lexists(data_root):
            _assert_not_redirected(data_root)
        if _path_lexists(secrets_dir):
            _assert_not_redirected(secrets_dir)
            if not secrets_dir.is_dir():
                raise SecretStorageUnavailableError(
                    "secret_storage_unavailable"
                )
        elif not write:
            raise SecretStorageUnavailableError("secret_storage_unavailable")
        if _path_lexists(canonical):
            self._assert_regular_file(canonical)

    @staticmethod
    def _assert_regular_file(path: Path) -> None:
        _assert_not_redirected(path)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise SecretStorageUnavailableError(
                "secret_storage_unavailable"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise SecretStorageUnavailableError(
                "secret_storage_unavailable"
            )


def get_effective_scaleway_api_key(
    *,
    store: ScalewaySecretStore | None = None,
) -> EffectiveSecret:
    resolved_store = store or ScalewaySecretStore()
    persisted = resolved_store.read()
    env_value = os.getenv(SCALEWAY_API_KEY_ENV_VAR)
    if env_value:
        try:
            normalized = normalize_scaleway_api_key(env_value)
        except ValueError:
            return EffectiveSecret(
                value=None,
                source=NONE_SOURCE,
                persisted_state=persisted.state,
                last_updated_at=persisted.last_updated_at,
                reason_code="secret_environment_invalid",
            )
        return EffectiveSecret(
            value=normalized,
            source=ENV_SOURCE,
            persisted_state=persisted.state,
            last_updated_at=persisted.last_updated_at,
        )
    credential = _systemd_credential(SCALEWAY_API_KEY_ENV_VAR)
    if credential is not None:
        try:
            normalized = normalize_scaleway_api_key(credential)
        except ValueError:
            return EffectiveSecret(value=None, source=NONE_SOURCE, reason_code="secret_credential_invalid")
        return EffectiveSecret(value=normalized, source=PERSISTED_SOURCE, persisted_state=PERSISTED_USABLE)
    if persisted.state == PERSISTED_USABLE:
        return EffectiveSecret(
            value=persisted.value,
            source=PERSISTED_SOURCE,
            persisted_state=persisted.state,
            last_updated_at=persisted.last_updated_at,
        )
    return EffectiveSecret(
        value=None,
        source=NONE_SOURCE,
        persisted_state=persisted.state,
        last_updated_at=persisted.last_updated_at,
        reason_code=persisted.reason_code,
    )


def set_persisted_scaleway_api_key(
    api_key: str,
    *,
    store: ScalewaySecretStore | None = None,
) -> EffectiveSecret:
    resolved_store = store or ScalewaySecretStore()
    resolved_store.save(api_key)
    return get_effective_scaleway_api_key(store=resolved_store)


def delete_persisted_scaleway_api_key(
    *,
    store: ScalewaySecretStore | None = None,
) -> EffectiveSecret:
    resolved_store = store or ScalewaySecretStore()
    resolved_store.delete()
    return get_effective_scaleway_api_key(store=resolved_store)


def resolve_secret_ref(secret_ref: str | None) -> EffectiveSecret:
    """Resolve the existing env-style reference through the effective secret boundary."""
    if not secret_ref:
        return EffectiveSecret(value=None, source=NONE_SOURCE)
    if not secret_ref.startswith("env:"):
        raise ValueError("Only env: secret references are supported.")
    env_name = secret_ref.removeprefix("env:")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
        raise ValueError("Invalid credential reference name.")
    if env_name == SCALEWAY_API_KEY_ENV_VAR:
        return get_effective_scaleway_api_key()
    env_value = os.getenv(env_name)
    value = env_value or _systemd_credential(env_name)
    return EffectiveSecret(
        value=value if value else None,
        source=(ENV_SOURCE if env_value else PERSISTED_SOURCE) if value else NONE_SOURCE,
    )


def _systemd_credential(name: str) -> str | None:
    """Read a named credential supplied by the service manager, never its store."""
    directory = os.getenv("CREDENTIALS_DIRECTORY")
    if not directory:
        return None
    path = Path(directory) / name
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_SYSTEMD_CREDENTIAL_BYTES:
            return None
        value = os.read(descriptor, MAX_SYSTEMD_CREDENTIAL_BYTES + 1)
        if not value or len(value) > MAX_SYSTEMD_CREDENTIAL_BYTES:
            return None
        return value.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(descriptor)


def normalize_scaleway_api_key(api_key: str | None) -> str:
    stripped = (api_key or "").strip()
    if not stripped:
        raise ValueError("empty")
    if len(stripped) > MAX_SCALEWAY_API_KEY_LENGTH:
        raise ValueError("too_long")
    if any(character.isspace() for character in stripped):
        raise ValueError("contains_whitespace")
    return stripped


def _parse_json_object(raw: bytes, reason: str) -> dict[str, object]:
    try:
        decoded = raw.decode("utf-8")
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretStorageCorruptedError(reason) from exc
    if not isinstance(parsed, dict):
        raise SecretStorageCorruptedError(reason)
    return parsed


def _canonical_json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_read(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SecretStorageUnavailableError(
            "secret_storage_unavailable"
        ) from exc
    if size < 1 or size > MAX_SECRET_FILE_BYTES:
        raise SecretStorageCorruptedError("secret_storage_corrupted")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SecretStorageUnavailableError(
            "secret_storage_unavailable"
        ) from exc
    if len(raw) != size:
        raise SecretStorageCorruptedError("secret_storage_corrupted")
    return raw


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _assert_within_root(data_root: Path, path: Path) -> None:
    root = data_root.resolve(strict=False)
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SecretStorageUnavailableError(
            "secret_storage_unavailable"
        ) from exc


def _assert_not_redirected(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SecretStorageUnavailableError(
            "secret_storage_unavailable"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise SecretStorageUnavailableError("secret_storage_unavailable")
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attributes & reparse_flag:
        raise SecretStorageUnavailableError("secret_storage_unavailable")


def _mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    except OSError:
        return None


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _safe_reason(error: object, default: str) -> str:
    value = str(error)
    return value if value.startswith("secret_") else default
