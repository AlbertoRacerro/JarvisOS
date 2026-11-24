"""Hash-pinned, typed access to the user's local FreeCAD add-on."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

FREECAD_ALLOWLIST = ("ping", "list_documents", "get_objects", "get_object", "edit_object")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_RELAY = Path(__file__).with_name("freecad_relay.py")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_WIN_RELAY_PATH = re.compile(r"^(?:[A-Za-z]:\\|\\\\wsl(?:\.localhost|\$)\\)")
_WIN_FORBIDDEN = re.compile(r'[\x00-\x1f"<>|*?&%^]')


class FreecadBridgeError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _configured_runtime() -> tuple[Path | None, str, str | None]:
    raw_path = os.environ.get("JARVISOS_FREECAD_PYTHON_PATH")
    expected = os.environ.get("JARVISOS_FREECAD_PYTHON_SHA256", "").lower()
    if not raw_path:
        return None, expected, "FREECAD_BRIDGE_UNAVAILABLE"
    path = Path(raw_path).expanduser()
    if not path.is_file():
        return path, expected, "FREECAD_BRIDGE_UNAVAILABLE"
    if _DIGEST_RE.fullmatch(expected) is None:
        return path, expected, "FREECAD_BRIDGE_DIGEST_UNCONFIGURED"
    try:
        actual = _sha256(path)
    except OSError:
        return path, expected, "FREECAD_BRIDGE_UNAVAILABLE"
    if actual != expected:
        return path, expected, "FREECAD_BRIDGE_DIGEST_MISMATCH"
    return path, expected, None


def _relay_path(executable: Path) -> str:
    if executable.suffix.lower() == ".exe" and str(executable).lower().startswith("/mnt/"):
        try:
            completed = subprocess.run(
                ["wslpath", "-w", str(_RELAY)], shell=False, timeout=10, capture_output=True, text=True
            )
        except (OSError, subprocess.TimeoutExpired):
            raise FreecadBridgeError("FREECAD_BRIDGE_UNAVAILABLE", "wslpath could not convert relay path.", 502) from None
        value = str(completed.stdout or "").strip()
        if completed.returncode != 0 or _WIN_RELAY_PATH.match(value) is None or _WIN_FORBIDDEN.search(value) is not None:
            raise FreecadBridgeError("FREECAD_BRIDGE_UNAVAILABLE", "wslpath returned an unusable relay path.", 502)
        return value
    return str(_RELAY)


def _token() -> tuple[str | None, str | None]:
    path = Path(os.environ.get("JARVISOS_FREECAD_MCP_TOKEN_FILE", "~/.config/jarvis/freecad_mcp_token")).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                return None, "FREECAD_MCP_TOKEN_UNSAFE"
            if info.st_size > 4096:
                return None, "FREECAD_MCP_TOKEN_UNSAFE"
            with os.fdopen(fd, "rb", closefd=False) as stream:
                value = stream.read(4097).decode("utf-8").strip()
        finally:
            os.close(fd)
        if len(value) > 4096:
            return None, "FREECAD_MCP_TOKEN_UNSAFE"
        if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
            return None, "FREECAD_MCP_TOKEN_MISSING"
        return value, None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None, "FREECAD_MCP_TOKEN_UNSAFE"
        return None, "FREECAD_MCP_TOKEN_MISSING"
    except UnicodeError:
        return None, "FREECAD_MCP_TOKEN_MISSING"


def _validate_identifier(value: Any) -> None:
    if not isinstance(value, str) or len(value) > 128 or _IDENTIFIER.fullmatch(value) is None:
        raise FreecadBridgeError("FREECAD_INVALID_IDENTIFIER", "Nome FreeCAD non valido.", 400)


def _property_info(obj: dict[str, Any], name: str) -> tuple[bool, Any, bool]:
    properties = obj.get("Properties", obj.get("properties", {}))
    if not isinstance(properties, dict) or name not in properties:
        return False, None, False
    entry = properties[name]
    if isinstance(entry, dict) and ("value" in entry or "Value" in entry):
        value = entry.get("value", entry.get("Value"))
        readonly = bool(entry.get("read_only", entry.get("readonly", entry.get("ReadOnly", False))))
        return True, value, readonly
    return True, entry, False


def _same_value(old: Any, new: Any) -> bool:
    if isinstance(old, bool):
        return isinstance(new, bool)
    if isinstance(old, (int, float)) and not isinstance(old, bool):
        return isinstance(new, (int, float)) and not isinstance(new, bool)
    return isinstance(old, str) and isinstance(new, str)


def _unit_suffix(value: str) -> str | None:
    match = re.search(r"\s([A-Za-z][A-Za-z0-9*/^.-]*)$", value)
    return match.group(1) if match else None


def validate_edit(obj: dict[str, Any], property_name: str, value: Any) -> Any:
    _validate_identifier(property_name)
    exists, old, readonly = _property_info(obj, property_name)
    if not exists:
        raise FreecadBridgeError("FREECAD_PROPERTY_NOT_FOUND", "La proprietà non esiste in questo oggetto.")
    if readonly:
        raise FreecadBridgeError("FREECAD_PROPERTY_READ_ONLY", "La proprietà è di sola lettura.")
    if not _same_value(old, value):
        raise FreecadBridgeError("FREECAD_PROPERTY_TYPE_MISMATCH", "Il nuovo valore deve essere dello stesso tipo di quello attuale.")
    if any(isinstance(item, float) and not math.isfinite(item) for item in (old, value)):
        raise FreecadBridgeError("FREECAD_INVALID_EDIT", "Il valore deve essere finito.", 400)
    if isinstance(old, str) and _unit_suffix(old) != _unit_suffix(value):
        raise FreecadBridgeError("FREECAD_PROPERTY_UNIT_MISMATCH", "Il nuovo valore deve mantenere la stessa unità di misura.")
    return old


class FreecadBridge:
    def __init__(self, timeout_s: float = 50.0) -> None:
        self.timeout_s = timeout_s

    def configuration(self) -> dict[str, Any]:
        path, _, reason = _configured_runtime()
        if reason is None:
            _, reason = _token()
        return {"configured": reason is None, "code": reason, "path": path}

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method not in FREECAD_ALLOWLIST:
            raise FreecadBridgeError("FREECAD_METHOD_NOT_ALLOWED", "Operazione non consentita verso FreeCAD.", 400)
        params = params or {}
        for key in ("doc_name", "obj_name", "property"):
            if key in params:
                _validate_identifier(params[key])
        executable, _, reason = _configured_runtime()
        if reason or executable is None:
            raise FreecadBridgeError(reason or "FREECAD_BRIDGE_UNAVAILABLE", "Il collegamento a FreeCAD non è configurato.")
        token, token_reason = _token()
        if token_reason or token is None:
            raise FreecadBridgeError(token_reason or "FREECAD_MCP_TOKEN_MISSING", "Token MCP FreeCAD non disponibile.")
        request = {"id": 1, "method": method, "params": params, "token": token}
        try:
            process = subprocess.Popen(
                [str(executable), _relay_path(executable)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                text=True,
            )
            stdout, _ = process.communicate(json.dumps(request) + "\n", timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise FreecadBridgeError("FREECAD_BRIDGE_TIMEOUT", "FreeCAD non ha risposto in tempo.", 504) from exc
        except OSError as exc:
            raise FreecadBridgeError("FREECAD_BRIDGE_UNAVAILABLE", "Il collegamento a FreeCAD non è partito.") from exc
        try:
            response = json.loads(stdout.splitlines()[0])
        except (IndexError, json.JSONDecodeError) as exc:
            raise FreecadBridgeError(
                "FREECAD_BRIDGE_PROTOCOL_ERROR", "Risposta non valida dal collegamento con FreeCAD.", 502
            ) from exc
        if response.get("ok") is not True:
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            code = str(error.get("code") or "FREECAD_ADDON_ERROR")
            if code == "FREECAD_ADDON_NOT_RUNNING":
                raise FreecadBridgeError(
                    code, "L'add-on MCP Bridge è spento: attiva MCP On in FreeCAD e riprova.", 503
                )
            if code == "FREECAD_MCP_UNAUTHORIZED":
                raise FreecadBridgeError(code, "Il token MCP non è stato accettato da FreeCAD.", 401)
            if code == "FREECAD_MCP_DECLINED":
                raise FreecadBridgeError(code, "Lo script Jarvis è stato rifiutato in FreeCAD.")
            if code == "FREECAD_MCP_PENDING":
                raise FreecadBridgeError(code, "Lo script Jarvis attende ancora conferma in FreeCAD: se lo confermi ora non modificherà il documento.", 504)
            if code == "FREECAD_STALE_PREVIEW":
                raise FreecadBridgeError(code, "Il valore in FreeCAD è cambiato dopo l'anteprima: rifai l'anteprima.")
            if code == "FREECAD_EDIT_EXPIRED":
                raise FreecadBridgeError(
                    code, "La conferma in FreeCAD è arrivata troppo tardi: il documento non è stato modificato.", 409
                )
            if code == "FREECAD_ROLLBACK_FAILED":
                raise FreecadBridgeError(code, "FreeCAD non ha confermato il ripristino del valore precedente.", 502)
            raise FreecadBridgeError(code, "FreeCAD ha rifiutato la richiesta.", 502)
        return response.get("result")

    def get_object(self, document: str, obj_name: str) -> dict[str, Any]:
        _validate_identifier(document)
        _validate_identifier(obj_name)
        result = self.request("get_object", {"doc_name": document, "obj_name": obj_name})
        if not isinstance(result, dict) or result.get("success") is False or not isinstance(result.get("Name"), str):
            raise FreecadBridgeError("FREECAD_OBJECT_NOT_FOUND", "Oggetto non trovato nel documento FreeCAD.", 404)
        return result

    def validate_edit(self, document: str, obj_name: str, property_name: str, value: Any) -> Any:
        return validate_edit(self.get_object(document, obj_name), property_name, value)


def validate_request(bridge: FreecadBridge, document: str, obj_name: str, property_name: str, value: Any) -> Any:
    if not document or not obj_name or not property_name:
        raise FreecadBridgeError("FREECAD_INVALID_EDIT", "Scegli documento, oggetto e proprietà.", 400)
    for name in (document, obj_name, property_name):
        _validate_identifier(name)
    return bridge.validate_edit(document, obj_name, property_name, value)
