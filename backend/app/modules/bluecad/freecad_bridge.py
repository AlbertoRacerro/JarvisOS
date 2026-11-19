"""Hash-pinned, typed access to the user's local FreeCAD add-on."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from app.modules.bluecad.print_handoff import _to_windows_path

FREECAD_ALLOWLIST = ("ping", "list_documents", "get_objects", "get_object", "edit_object")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_RELAY = Path(__file__).with_name("freecad_relay.py")


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
        return _to_windows_path(_RELAY)
    return str(_RELAY)


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
    exists, old, readonly = _property_info(obj, property_name)
    if not exists:
        raise FreecadBridgeError("FREECAD_PROPERTY_NOT_FOUND", "FreeCAD property was not found.")
    if readonly:
        raise FreecadBridgeError("FREECAD_PROPERTY_READ_ONLY", "FreeCAD property is read-only.")
    if not _same_value(old, value):
        raise FreecadBridgeError("FREECAD_PROPERTY_TYPE_MISMATCH", "FreeCAD property value has a different type.")
    if isinstance(old, str) and _unit_suffix(old) != _unit_suffix(value):
        raise FreecadBridgeError("FREECAD_PROPERTY_UNIT_MISMATCH", "FreeCAD quantity must keep its unit.")
    return old


class FreecadBridge:
    def __init__(self, timeout_s: float = 10.0) -> None:
        self.timeout_s = timeout_s

    def configuration(self) -> dict[str, Any]:
        path, _, reason = _configured_runtime()
        return {"configured": reason is None, "code": reason, "path": path}

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method not in FREECAD_ALLOWLIST:
            raise FreecadBridgeError("FREECAD_METHOD_NOT_ALLOWED", "FreeCAD method is not allowed.", 400)
        executable, _, reason = _configured_runtime()
        if reason or executable is None:
            raise FreecadBridgeError(reason or "FREECAD_BRIDGE_UNAVAILABLE", "FreeCAD bridge is unavailable.")
        request = {"id": 1, "method": method, "params": params or {}}
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
            raise FreecadBridgeError("FREECAD_BRIDGE_TIMEOUT", "FreeCAD request timed out.", 504) from exc
        except OSError as exc:
            raise FreecadBridgeError("FREECAD_BRIDGE_UNAVAILABLE", "FreeCAD bridge could not start.") from exc
        try:
            response = json.loads(stdout.splitlines()[0])
        except (IndexError, json.JSONDecodeError) as exc:
            raise FreecadBridgeError(
                "FREECAD_BRIDGE_PROTOCOL_ERROR", "FreeCAD relay returned an invalid response.", 502
            ) from exc
        if response.get("ok") is not True:
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            code = str(error.get("code") or "FREECAD_ADDON_ERROR")
            status = 503 if code == "FREECAD_ADDON_NOT_RUNNING" else 502
            raise FreecadBridgeError(code, "FreeCAD add-on request failed.", status)
        return response.get("result")

    def get_object(self, document: str, obj_name: str) -> dict[str, Any]:
        result = self.request("get_object", {"doc_name": document, "obj_name": obj_name})
        if not isinstance(result, dict) or result.get("success") is False or not isinstance(result.get("Name"), str):
            raise FreecadBridgeError("FREECAD_OBJECT_NOT_FOUND", "FreeCAD object was not found.", 404)
        return result

    def validate_edit(self, document: str, obj_name: str, property_name: str, value: Any) -> Any:
        return validate_edit(self.get_object(document, obj_name), property_name, value)


def validate_request(bridge: FreecadBridge, document: str, obj_name: str, property_name: str, value: Any) -> Any:
    if not document or not obj_name or not property_name:
        raise FreecadBridgeError("FREECAD_INVALID_EDIT", "FreeCAD edit identifiers are required.", 400)
    return bridge.validate_edit(document, obj_name, property_name, value)
