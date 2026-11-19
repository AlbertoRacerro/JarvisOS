#!/usr/bin/env python3
"""Small Windows-side relay for the local FreeCAD XML-RPC add-on."""

from __future__ import annotations

import json
import sys
import xmlrpc.client
from http.client import HTTPConnection
from typing import Any
from urllib.error import URLError

FREECAD_ALLOWLIST = ("ping", "list_documents", "get_objects", "get_object", "edit_object")
_TIMEOUT_S = 10.0


class _TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host: tuple[str, dict[str, str]] | str) -> HTTPConnection:
        connection = super().make_connection(host)
        connection.timeout = _TIMEOUT_S
        return connection


def _call(method: str, params: dict[str, Any]) -> Any:
    if method not in FREECAD_ALLOWLIST:
        raise ValueError("FREECAD_METHOD_NOT_ALLOWED")
    proxy = xmlrpc.client.ServerProxy(
        "http://127.0.0.1:9875",
        transport=_TimeoutTransport(),
        allow_none=True,
    )
    if method in {"ping", "list_documents"}:
        return getattr(proxy, method)()
    if method == "get_objects":
        return proxy.get_objects(params["doc_name"])
    if method == "get_object":
        return proxy.get_object(params["doc_name"], params["obj_name"])
    return proxy.edit_object(params["doc_name"], params["obj_name"], params["properties"])


def main() -> None:
    for line in sys.stdin:
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("id"), (str, int)):
                raise ValueError("FREECAD_INVALID_REQUEST")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("FREECAD_INVALID_REQUEST")
            result = _call(method, params)
            response = {"id": request["id"], "ok": True, "result": result}
        except (TimeoutError, ConnectionRefusedError, ConnectionError, OSError, URLError):
            response = {"id": request.get("id") if isinstance(request, dict) else None, "ok": False,
                        "error": {"code": "FREECAD_ADDON_NOT_RUNNING", "message": "FreeCAD add-on is not running."}}
        except Exception as exc:
            code = str(exc) if str(exc).startswith("FREECAD_") else "FREECAD_ADDON_ERROR"
            response = {"id": request.get("id") if isinstance(request, dict) else None, "ok": False,
                        "error": {"code": code, "message": "FreeCAD request failed."}}
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
