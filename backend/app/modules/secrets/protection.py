from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Protocol, runtime_checkable

WINDOWS_DPAPI_PROTECTOR_ID = "windows_dpapi_current_user_v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretProtectionError(RuntimeError):
    """Base error for safe secret-protection failures."""


class SecretProtectionUnavailableError(SecretProtectionError):
    """Raised when the production protection mechanism is unavailable."""


class SecretProtectionOperationError(SecretProtectionError):
    """Raised when protect or unprotect fails without exposing secret material."""


@runtime_checkable
class SecretProtector(Protocol):
    protector_id: str

    def protect(self, *, secret_id: str, plaintext: bytes) -> bytes: ...

    def unprotect(self, *, secret_id: str, ciphertext: bytes) -> bytes: ...


class UnsupportedSecretProtector:
    protector_id = "unavailable"

    def protect(self, *, secret_id: str, plaintext: bytes) -> bytes:
        del secret_id, plaintext
        raise SecretProtectionUnavailableError("secret_protection_unavailable")

    def unprotect(self, *, secret_id: str, ciphertext: bytes) -> bytes:
        del secret_id, ciphertext
        raise SecretProtectionUnavailableError("secret_protection_unavailable")


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDpapiCurrentUserProtector:
    protector_id = WINDOWS_DPAPI_PROTECTOR_ID

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SecretProtectionUnavailableError("secret_protection_unavailable")
        try:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise SecretProtectionUnavailableError(
                "secret_protection_unavailable"
            ) from exc

        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def protect(self, *, secret_id: str, plaintext: bytes) -> bytes:
        if not isinstance(secret_id, str) or not secret_id:
            raise ValueError("secret_id is required")
        if not isinstance(plaintext, bytes) or not plaintext:
            raise ValueError("plaintext must be non-empty bytes")
        return self._call_protect(secret_id, plaintext)

    def unprotect(self, *, secret_id: str, ciphertext: bytes) -> bytes:
        if not isinstance(secret_id, str) or not secret_id:
            raise ValueError("secret_id is required")
        if not isinstance(ciphertext, bytes) or not ciphertext:
            raise ValueError("ciphertext must be non-empty bytes")
        return self._call_unprotect(secret_id, ciphertext)

    def _call_protect(self, secret_id: str, plaintext: bytes) -> bytes:
        input_buffer, input_blob = _input_blob(plaintext)
        output_blob = _DataBlob()
        succeeded = self._crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            wintypes.LPCWSTR(_description_for_secret(secret_id)),
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        _ = input_buffer
        return self._copy_and_free_output(
            succeeded,
            output_blob,
            "secret_protect_failed",
        )

    def _call_unprotect(self, secret_id: str, ciphertext: bytes) -> bytes:
        input_buffer, input_blob = _input_blob(ciphertext)
        output_blob = _DataBlob()
        description = wintypes.LPWSTR()
        succeeded = self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        _ = input_buffer
        return self._copy_and_free_output(
            succeeded,
            output_blob,
            "secret_unprotect_failed",
            description=description,
            expected_description=_description_for_secret(secret_id),
        )

    def _copy_and_free_output(
        self,
        succeeded: int,
        output_blob: _DataBlob,
        reason: str,
        *,
        description: wintypes.LPWSTR | None = None,
        expected_description: str | None = None,
    ) -> bytes:
        try:
            if not succeeded:
                raise SecretProtectionOperationError(reason)
            if expected_description is not None:
                if not description or description.value != expected_description:
                    raise SecretProtectionOperationError(reason)
            if not output_blob.pbData or output_blob.cbData < 1:
                raise SecretProtectionOperationError(reason)
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                self._kernel32.LocalFree(
                    ctypes.cast(output_blob.pbData, ctypes.c_void_p)
                )
            if description:
                self._kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))


def _description_for_secret(secret_id: str) -> str:
    return f"JarvisOS:{secret_id}"


def _input_blob(value: bytes) -> tuple[ctypes.Array[ctypes.c_char], _DataBlob]:
    buffer = ctypes.create_string_buffer(value, len(value))
    pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    return buffer, _DataBlob(len(value), pointer)


def build_product_secret_protector() -> SecretProtector:
    if sys.platform != "win32":
        return UnsupportedSecretProtector()
    try:
        return WindowsDpapiCurrentUserProtector()
    except (AttributeError, OSError, SecretProtectionUnavailableError):
        return UnsupportedSecretProtector()
