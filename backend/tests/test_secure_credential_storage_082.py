from __future__ import annotations

import ctypes
import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.database import initialize_database
from app.core.paths import JarvisPaths, build_paths
from app.modules.secrets import protection, storage
from app.modules.secrets.protection import WINDOWS_DPAPI_PROTECTOR_ID
from app.modules.secrets.storage import (
    PERSISTED_CORRUPTED,
    PERSISTED_UNAVAILABLE,
    PERSISTED_USABLE,
    ScalewaySecretStore,
    SecretStorageError,
    SecretStorageUnavailableError,
    get_effective_scaleway_api_key,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "sk-live-DO-NOT-LEAK-82-3f90e77a02a64be0"


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
        if not ciphertext.startswith(b"TEST1"):
            raise protection.SecretProtectionOperationError(
                "secret_unprotect_failed"
            )
        body = ciphertext[5:]
        stream = self._stream(secret_id, len(body))
        return bytes(
            left ^ right for left, right in zip(body, stream, strict=True)
        )


def _paths(root: Path) -> JarvisPaths:
    return JarvisPaths(
        data_root=root,
        database_file=root / "jarvisos.db",
        workspaces_dir=root / "workspaces",
        artifacts_dir=root / "artifacts",
        logs_dir=root / "logs",
    )


def _store(root: Path) -> ScalewaySecretStore:
    return ScalewaySecretStore(
        paths=_paths(root),
        protector=DeterministicTestProtector(),
    )


def test_persisted_secret_survives_store_reconstruction_without_plaintext(
    tmp_path: Path,
) -> None:
    first = _store(tmp_path)
    saved = first.save(SENTINEL)

    assert saved.state == PERSISTED_USABLE
    assert saved.value == SENTINEL
    envelope = first.paths.scaleway_secret_file.read_bytes()
    assert SENTINEL.encode() not in envelope
    assert SENTINEL[:8].encode() not in envelope

    reconstructed = _store(tmp_path)
    resolved = get_effective_scaleway_api_key(store=reconstructed)
    assert resolved.key_present is True
    assert resolved.value == SENTINEL
    assert resolved.source == storage.PERSISTED_SOURCE
    assert resolved.persisted_state == PERSISTED_USABLE


def test_corruption_is_distinct_from_absence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(SENTINEL)
    envelope = json.loads(
        store.paths.scaleway_secret_file.read_text(encoding="utf-8")
    )
    envelope["ciphertext_sha256"] = "0" * 64
    store.paths.scaleway_secret_file.write_text(
        json.dumps(envelope),
        encoding="utf-8",
    )

    persisted = store.read()
    effective = get_effective_scaleway_api_key(store=store)
    assert persisted.state == PERSISTED_CORRUPTED
    assert persisted.value is None
    assert effective.key_present is False
    assert effective.persisted_state == PERSISTED_CORRUPTED


def test_failed_replacement_preserves_previous_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    old_value = SENTINEL
    store.save(old_value)

    def fail_replace(source: object, target: object) -> None:
        del source, target
        raise OSError("simulated replace failure")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(SecretStorageError, match="secret_write_failed"):
        store.save("sk-live-REPLACEMENT-82-66aa3f")

    assert _store(tmp_path).read().value == old_value


def test_environment_precedence_and_invalid_environment_are_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.save(SENTINEL)

    monkeypatch.setenv(storage.SCALEWAY_API_KEY_ENV_VAR, "sk-env-valid-82")
    effective = get_effective_scaleway_api_key(store=store)
    assert effective.source == storage.ENV_SOURCE
    assert effective.value == "sk-env-valid-82"
    assert effective.persisted_state == PERSISTED_USABLE

    monkeypatch.setenv(
        storage.SCALEWAY_API_KEY_ENV_VAR,
        "invalid env value",
    )
    invalid = get_effective_scaleway_api_key(store=store)
    assert invalid.key_present is False
    assert invalid.reason_code == "secret_environment_invalid"
    assert invalid.persisted_state == PERSISTED_USABLE


def test_symlinked_secret_path_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.paths.secrets_dir.mkdir(parents=True)
    target = tmp_path / "outside-secret"
    target.write_text("not a credential", encoding="utf-8")
    try:
        store.paths.scaleway_secret_file.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable")

    assert store.read().state == PERSISTED_UNAVAILABLE
    with pytest.raises(SecretStorageUnavailableError):
        store.save(SENTINEL)


def test_secret_routes_persist_without_returning_fragments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.delenv(storage.SCALEWAY_API_KEY_ENV_VAR, raising=False)
    monkeypatch.setattr(
        storage,
        "build_product_secret_protector",
        DeterministicTestProtector,
    )
    initialize_database()
    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.post(
            "/secrets/scaleway/api-key",
            json={"api_key": SENTINEL},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["key_present"] is True
        assert payload["source"] == "secure_persisted"
        assert payload["effective_source"] == "secure_persisted"
        assert payload["persisted_state"] == "usable"
        assert payload["storage_mode"] == "secure_persisted"
        assert payload["masked_preview"] is None
        assert SENTINEL not in response.text
        assert SENTINEL[:8] not in response.text

        refreshed = client.get("/secrets/scaleway/status")
        assert refreshed.status_code == 200
        assert refreshed.json()["storage_mode"] == "secure_persisted"
        assert SENTINEL not in refreshed.text

        deleted = client.delete("/secrets/scaleway/api-key")
        assert deleted.status_code == 200
        assert deleted.json()["key_present"] is False
        assert deleted.json()["persisted_state"] == "absent"


def test_secret_post_conflicts_with_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.setenv(
        storage.SCALEWAY_API_KEY_ENV_VAR,
        "sk-env-authoritative-82",
    )
    monkeypatch.setattr(
        storage,
        "build_product_secret_protector",
        DeterministicTestProtector,
    )
    initialize_database()
    from app.main import create_app

    with TestClient(create_app()) as client:
        for payload in (
            {"api_key": SENTINEL},
            {},
            {"api_key": ""},
            {"api_key": "contains whitespace"},
            {"api_key": "x" * (storage.MAX_SCALEWAY_API_KEY_LENGTH + 1)},
        ):
            response = client.post(
                "/secrets/scaleway/api-key",
                json=payload,
            )
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == (
                "scaleway_api_key_environment_override"
            )
        status = client.get("/secrets/scaleway/status").json()
        assert status["effective_source"] == "environment"
        assert status["masked_preview"] is None
        assert SENTINEL not in json.dumps(status)


def test_snapshot_excludes_secret_and_restore_removes_it(
    tmp_path: Path,
) -> None:
    if str(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOSITORY_ROOT))
    from scripts.data_root_recovery.restore import restore_snapshot
    from scripts.data_root_recovery.snapshot import create_snapshot

    paths = build_paths()
    initialize_database()
    paths.workspaces_dir.mkdir(parents=True, exist_ok=True)
    paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    paths.scaleway_secret_file.write_text(SENTINEL, encoding="utf-8")

    snapshot = create_snapshot(
        source_root=paths.data_root,
        destination=tmp_path / "snapshots",
        database_filename=paths.database_file.name,
        snapshot_id="20260803T220000Z-082proof",
    )
    all_snapshot_bytes = b"".join(
        path.read_bytes()
        for path in snapshot.snapshot_dir.rglob("*")
        if path.is_file()
    )
    assert b"secrets/" not in all_snapshot_bytes
    assert SENTINEL.encode() not in all_snapshot_bytes
    assert not (snapshot.snapshot_dir / "secrets").exists()

    restore_snapshot(
        snapshot_dir=snapshot.snapshot_dir,
        target_root=paths.data_root,
        allow_nonempty_target=True,
    )
    assert not paths.scaleway_secret_file.exists()


class _NativeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _FakeCrypt32:
    def __init__(self) -> None:
        self.buffers: list[ctypes.Array[ctypes.c_char]] = []
        self.description_buffers: list[ctypes.Array[ctypes.c_wchar]] = []
        self.flags: list[int] = []
        self.protected_description: str | None = None
        self.CryptProtectData = _NativeFunction(self._protect)
        self.CryptUnprotectData = _NativeFunction(self._unprotect)

    def _write(self, output_pointer: object, value: bytes) -> int:
        buffer = ctypes.create_string_buffer(value, len(value))
        self.buffers.append(buffer)
        output = ctypes.cast(
            output_pointer,
            ctypes.POINTER(protection._DataBlob),
        ).contents
        output.cbData = len(value)
        output.pbData = ctypes.cast(
            buffer,
            ctypes.POINTER(ctypes.c_ubyte),
        )
        return 1

    def _protect(self, *args: object) -> int:
        self.flags.append(int(args[5]))
        self.protected_description = ctypes.cast(
            args[1],
            ctypes.c_wchar_p,
        ).value
        return self._write(args[6], b"protected")

    def _unprotect(self, *args: object) -> int:
        self.flags.append(int(args[5]))
        if self.protected_description is None:
            return 0
        buffer = ctypes.create_unicode_buffer(self.protected_description)
        self.description_buffers.append(buffer)
        description = ctypes.cast(
            args[1],
            ctypes.POINTER(ctypes.c_wchar_p),
        )
        description[0] = ctypes.cast(buffer, ctypes.c_wchar_p)
        return self._write(args[6], b"plaintext")


class _FakeKernel32:
    def __init__(self) -> None:
        self.free_calls = 0
        self.LocalFree = _NativeFunction(self._free)

    def _free(self, pointer: object) -> None:
        del pointer
        self.free_calls += 1
        return None


def test_dpapi_wrapper_uses_current_user_noninteractive_flags_and_frees_buffers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crypt32 = _FakeCrypt32()
    kernel32 = _FakeKernel32()

    def fake_windll(name: str, *, use_last_error: bool):
        assert use_last_error is True
        return crypt32 if name == "crypt32" else kernel32

    monkeypatch.setattr(protection.sys, "platform", "win32")
    monkeypatch.setattr(
        protection.ctypes,
        "WinDLL",
        fake_windll,
        raising=False,
    )
    protector = protection.WindowsDpapiCurrentUserProtector()

    assert protector.protect(
        secret_id="scaleway_api_key",
        plaintext=b"value",
    ) == b"protected"
    assert protector.unprotect(
        secret_id="scaleway_api_key",
        ciphertext=b"protected",
    ) == b"plaintext"
    with pytest.raises(
        protection.SecretProtectionOperationError,
        match="secret_unprotect_failed",
    ):
        protector.unprotect(
            secret_id="another_secret",
            ciphertext=b"protected",
        )
    assert crypt32.flags == [protection.CRYPTPROTECT_UI_FORBIDDEN] * 3
    assert all(flag & 0x4 == 0 for flag in crypt32.flags)
    assert kernel32.free_calls == 5
