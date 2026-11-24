#!/usr/bin/env python3
"""Bounded loopback MCP client for five fixed FreeCAD scripts."""

from __future__ import annotations

import http.client
import json
import math
import re
import sys
import time
from typing import Any

FREECAD_ALLOWLIST = ("ping", "list_documents", "get_objects", "get_object", "edit_object")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HOST = "127.0.0.1"
_PORT = 39280
_PATH = "/mcp"
_VERSION = "2025-11-25"
_PREFIX = "JARVIS_FREECAD_RESULT:"
_MAX_BODY = 256 * 1024
_MAX_OUTPUT = 256 * 1024
_DEADLINE_S = 45.0
# A fixed edit refuses to run after this window, well before the relay stops waiting.
_EDIT_WINDOW_S = 30.0

_COMMON = '''import json
_params = json.loads(PARAMETERS)
def _document():
    return App.getDocument(_params["doc_name"])
def _object():
    doc = _document()
    if doc is None:
        raise ValueError("FREECAD_DOCUMENT_NOT_FOUND")
    obj = doc.getObject(_params["obj_name"])
    if obj is None:
        raise ValueError("FREECAD_OBJECT_NOT_FOUND")
    return obj
def _value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
def _describe(obj):
    props = {}
    for name in obj.PropertiesList:
        props[name] = {"value": _value(getattr(obj, name)), "read_only": obj.getEditorMode(name) != 0}
    return {"Name": obj.Name, "Label": obj.Label, "TypeId": obj.TypeId, "Properties": props}
def _emit(result):
    print("JARVIS_FREECAD_RESULT:" + json.dumps(result, ensure_ascii=True, separators=(",", ":")))
'''

_TEMPLATES = {
    "ping": '''# Jarvis FreeCAD: ping
_emit(True)
''',
    "list_documents": '''# Jarvis FreeCAD: list_documents
_emit(list(App.listDocuments().keys()))
''',
    "get_objects": '''# Jarvis FreeCAD: get_objects
doc = _document()
if doc is None:
    raise ValueError("FREECAD_DOCUMENT_NOT_FOUND")
_emit([_describe(obj) for obj in doc.Objects])
''',
    "get_object": '''# Jarvis FreeCAD: get_object
_emit(_describe(_object()))
''',
    "edit_object": '''# Jarvis FreeCAD: edit_object
import time
obj = _object()
name = _params["property"]
if name not in obj.PropertiesList or obj.getEditorMode(name) != 0:
    raise ValueError("FREECAD_PROPERTY_NOT_EDITABLE")
old = getattr(obj, name)
if time.time() > _params["expires_at"]:
    # Approved after Jarvis stopped waiting: never apply a late edit.
    _emit({"success": False, "code": "FREECAD_EDIT_EXPIRED", "mutation_outcome": "unchanged"})
elif _value(old) != _params["old_value"]:
    _emit({"success": False, "code": "FREECAD_STALE_PREVIEW", "mutation_outcome": "unchanged"})
else:
    try:
        setattr(obj, name, _params["value"])
    except Exception:
        try:
            setattr(obj, name, old)
            restored = _value(getattr(obj, name)) == _value(old)
        except Exception:
            restored = False
        if restored:
            _emit({"success": False, "mutation_outcome": "rolled_back"})
        else:
            _emit({"success": False, "code": "FREECAD_ROLLBACK_FAILED", "mutation_outcome": "unknown"})
    else:
        _emit({"success": True, "mutation_outcome": "applied", "applied_value": _value(getattr(obj, name))})
''',
}


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 128 and _IDENTIFIER.fullmatch(value) is not None


def _validate(method: str, params: dict[str, Any]) -> None:
    if method not in FREECAD_ALLOWLIST:
        raise ValueError("FREECAD_METHOD_NOT_ALLOWED")
    required = {"ping": (), "list_documents": (), "get_objects": ("doc_name",),
                "get_object": ("doc_name", "obj_name"),
                "edit_object": ("doc_name", "obj_name", "property", "old_value", "value")}[method]
    if set(params) != set(required):
        raise ValueError("FREECAD_INVALID_REQUEST")
    for key in ("doc_name", "obj_name", "property"):
        if key in params and not _identifier(params[key]):
            raise ValueError("FREECAD_INVALID_IDENTIFIER")
    if method == "edit_object":
        for key in ("old_value", "value"):
            if type(params[key]) not in (str, int, float, bool) or len(str(params[key])) > 4096:
                raise ValueError("FREECAD_INVALID_EDIT")
            if isinstance(params[key], float) and not math.isfinite(params[key]):
                raise ValueError("FREECAD_INVALID_EDIT")


def _script(method: str, params: dict[str, Any], expires_at: float | None = None) -> str:
    _validate(method, params)
    if method == "edit_object":
        if expires_at is None:
            raise ValueError("FREECAD_INVALID_EDIT")
        params = {**params, "expires_at": expires_at}
    # This is the only parameter-bearing literal in the source sent to FreeCAD.
    literal = json.dumps(json.dumps(params, ensure_ascii=True), ensure_ascii=True)
    return _COMMON.replace("PARAMETERS", literal, 1) + _TEMPLATES[method]


def _http_call(token: str, name: str, arguments: dict[str, Any], deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("FREECAD_MCP_PENDING")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode("utf-8")
    connection = http.client.HTTPConnection(_HOST, _PORT, timeout=min(5.0, remaining))
    try:
        connection.request("POST", _PATH, body=body, headers={
            "Host": f"{_HOST}:{_PORT}", "Authorization": "Bearer " + token,
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _VERSION,
        })
        if getattr(connection, "sock", None) is not None:
            connection.sock.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            response = connection.getresponse()
        except TimeoutError as exc:
            raise ValueError("FREECAD_MCP_PENDING") from exc
        if response.status == 401:
            raise ValueError("FREECAD_MCP_UNAUTHORIZED")
        if response.status != 200:
            raise ValueError("FREECAD_BRIDGE_PROTOCOL_ERROR")
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        try:
            raw = response.read(_MAX_BODY + 1)
        except TimeoutError as exc:
            raise ValueError("FREECAD_MCP_PENDING") from exc
        if len(raw) > _MAX_BODY:
            raise ValueError("FREECAD_BRIDGE_PROTOCOL_ERROR")
    finally:
        connection.close()
    if content_type == "application/json":
        envelope = json.loads(raw)
    elif content_type == "text/event-stream":
        data = [line[5:].strip() for line in raw.decode("utf-8").splitlines() if line.startswith("data:")]
        envelope = json.loads(data[-1]) if data else None
    else:
        raise ValueError("FREECAD_BRIDGE_PROTOCOL_ERROR")
    if not isinstance(envelope, dict) or envelope.get("id") != 1 or envelope.get("jsonrpc") != "2.0":
        raise ValueError("FREECAD_BRIDGE_PROTOCOL_ERROR")
    result = envelope.get("result")
    if "error" in envelope or not isinstance(result, dict):
        raise ValueError("FREECAD_ADDON_ERROR")
    blocks = result.get("content")
    if not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict) or blocks[0].get("type") != "text":
        raise ValueError("FREECAD_BRIDGE_PROTOCOL_ERROR")
    page = json.loads(blocks[0]["text"])
    if not isinstance(page, dict) or not isinstance(page.get("page"), list) or not isinstance(page.get("has_more"), bool):
        raise ValueError("FREECAD_BRIDGE_PROTOCOL_ERROR")
    if result.get("isError") or page.get("error"):
        error = str(page.get("error", "")).lower()
        declined = any(term in error for term in ("declin", "don't run", "not run", "refus", "cancel", "denied"))
        raise ValueError("FREECAD_MCP_DECLINED" if declined else "FREECAD_ADDON_ERROR")
    return page


def _call(method: str, params: dict[str, Any], token: str) -> Any:
    # The relay runs on the FreeCAD host, so both sides read the same wall clock.
    script = _script(method, params, expires_at=time.time() + _EDIT_WINDOW_S)
    deadline = time.monotonic() + _DEADLINE_S
    page = _http_call(token, "execute_python", {"code": script}, deadline)
    output = ""
    next_page = 0
    while True:
        for chunk in page["page"]:
            if not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str):
                raise ValueError("FREECAD_BRIDGE_PROTOCOL_ERROR")
            if chunk.get("stream", "stdout") == "stdout":
                output += chunk["text"]
            if len(output) > _MAX_OUTPUT:
                raise ValueError("FREECAD_BRIDGE_PROTOCOL_ERROR")
        if not page["has_more"]:
            break
        job_token = page.get("job_token")
        if not isinstance(job_token, str) or not job_token or len(job_token) > 128:
            raise ValueError("FREECAD_BRIDGE_PROTOCOL_ERROR")
        if isinstance(page.get("page_no"), int):
            next_page = page["page_no"] + 1
        if time.monotonic() >= deadline:
            raise ValueError("FREECAD_MCP_PENDING")
        page = _http_call(token, "get_output_page", {"job_token": job_token, "page_no": next_page}, deadline)
    # The fixed script prints its result last; earlier lines may come from object hooks.
    for line in reversed(output.splitlines()):
        if line.startswith(_PREFIX):
            result = json.loads(line[len(_PREFIX):])
            if isinstance(result, dict) and result.get("code") in ("FREECAD_STALE_PREVIEW", "FREECAD_ROLLBACK_FAILED", "FREECAD_EDIT_EXPIRED"):
                raise ValueError(result["code"])
            return result
    raise ValueError("FREECAD_BRIDGE_PROTOCOL_ERROR")


def main() -> None:
    for line in sys.stdin:
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("id"), (str, int)):
                raise ValueError("FREECAD_INVALID_REQUEST")
            method, params, token = request.get("method"), request.get("params"), request.get("token")
            if not isinstance(method, str) or not isinstance(params, dict) or not isinstance(token, str) or not token:
                raise ValueError("FREECAD_INVALID_REQUEST")
            result = _call(method, params, token)
            response = {"id": request["id"], "ok": True, "result": result}
        except (TimeoutError, ConnectionRefusedError, ConnectionError, OSError):
            response = {"id": request.get("id"), "ok": False, "error": {"code": "FREECAD_ADDON_NOT_RUNNING"}}
        except Exception as exc:
            code = str(exc) if str(exc).startswith("FREECAD_") else "FREECAD_BRIDGE_PROTOCOL_ERROR"
            response = {"id": request.get("id"), "ok": False, "error": {"code": code}}
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
