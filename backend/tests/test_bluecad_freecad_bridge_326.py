"""Offline MCP Bridge contract and route tests for the five fixed operations."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import stat
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.modules.bluecad import freecad_relay, routes
from app.modules.bluecad.freecad_bridge import FreecadBridge, FreecadBridgeError
from app.modules.workspaces.service import list_workspaces


class FakeObject:
    Name = "Box"
    Label = "Box"
    TypeId = "Part::Box"
    PropertiesList = ["Length", "ReadOnly"]

    def __init__(self) -> None:
        self.Length = 10
        self.ReadOnly = 1

    def getEditorMode(self, name: str) -> int:
        return int(name == "ReadOnly")

    def __setattr__(self, name: str, value: object) -> None:
        if name == "Length" and value == 99:
            super().__setattr__(name, 99)
            raise ValueError("simulated failing set")
        if name == "Length" and value == 77:
            # Fails after writing, and its restore silently coerces the old value.
            super().__setattr__(name, 77)
            self.coerce_restore = True
            raise ValueError("simulated failing set")
        if name == "Length" and getattr(self, "coerce_restore", False):
            super().__setattr__("coerce_restore", False)
            super().__setattr__(name, value + 1)
            return
        super().__setattr__(name, value)


class FakeDocument:
    def __init__(self) -> None:
        self.Objects = [FakeObject()]

    def getObject(self, name: str) -> FakeObject | None:
        return self.Objects[0] if name == "Box" else None


class FakeApp:
    def __init__(self) -> None:
        self.doc = FakeDocument()

    def getDocument(self, name: str) -> FakeDocument | None:
        return self.doc if name == "Demo" else None

    def listDocuments(self) -> dict:
        return {"Demo": self.doc}


@pytest.fixture
def peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = FakeApp()
    calls: list[dict] = []
    mode = {"reply": "json", "pending": False}
    token = "secret-for-offline-test"

    class FakeResponse:
        status = 200

        def __init__(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.body = body
            self.content_type = content_type
            self.status = status

        def getheader(self, name: str, default: str = "") -> str:
            return self.content_type if name == "Content-Type" else default

        def read(self, size: int) -> bytes:
            return self.body[:size]

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            assert (host, port) == ("127.0.0.1", 39280)

        def request(self, verb: str, path: str, body: bytes, headers: dict) -> None:
            request = json.loads(body)
            calls.append({"path": path, "headers": headers, "request": request})
            assert verb == "POST" and path == "/mcp"
            assert headers["MCP-Protocol-Version"] == "2025-11-25"
            assert headers["Authorization"] == "Bearer " + token
            self.request_data = request

        def getresponse(self) -> FakeResponse:
            if mode["reply"] == "unauthorized":
                return FakeResponse(b"", "text/plain", 401)
            request = self.request_data
            args = request["params"]["arguments"]
            name = request["params"]["name"]
            if name == "execute_python":
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(args["code"], {"App": app})
                page = [{"stream": "stdout", "text": "untrusted line\n" + output.getvalue()}]
                if mode["pending"]:
                    payload = {"has_more": True, "job_token": "job-1", "page": []}
                    mode["saved_page"] = page
                else:
                    payload = {"has_more": False, "page": page}
            else:
                assert name == "get_output_page"
                assert args == {"job_token": "job-1", "page_no": 0}
                payload = {"has_more": False, "page_no": 0, "job_token": "job-1", "page": mode["saved_page"]}
            if mode["reply"] == "declined":
                payload = {"has_more": False, "error": "The code was declined", "page": []}
            envelope = {"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                **({"isError": True} if "error" in payload else {}),
            }}
            if mode["reply"] == "sse":
                body = b"event: message\ndata: " + json.dumps(envelope).encode() + b"\n\n"
                content_type = "text/event-stream"
            else:
                body = json.dumps(envelope).encode()
                content_type = "application/json"
            return FakeResponse(body, content_type)

        def close(self) -> None:
            pass

    monkeypatch.setattr(freecad_relay.http.client, "HTTPConnection", FakeConnection)

    class FakeProcess:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            assert token not in json.dumps(argv)
            self.argv = argv

        def communicate(self, request_line: str, timeout: float) -> tuple[str, None]:
            request = json.loads(request_line)
            try:
                result = freecad_relay._call(request["method"], request["params"], request["token"])
                response = {"id": request["id"], "ok": True, "result": result}
            except ValueError as error:
                response = {"id": request["id"], "ok": False, "error": {"code": str(error)}}
            return json.dumps(response) + "\n", None

    from app.modules.bluecad import freecad_bridge
    monkeypatch.setattr(freecad_bridge.subprocess, "Popen", FakeProcess)
    executable = tmp_path / "fake-python"
    executable.write_text("#!/bin/sh\nexec python3 \"$1\"\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    token_file = tmp_path / "token"
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("JARVISOS_FREECAD_PYTHON_PATH", str(executable))
    monkeypatch.setenv("JARVISOS_FREECAD_PYTHON_SHA256", hashlib.sha256(executable.read_bytes()).hexdigest())
    monkeypatch.setenv("JARVISOS_FREECAD_MCP_TOKEN_FILE", str(token_file))
    yield app, calls, mode, token_file, token


def test_five_templates_json_parameters_and_paging(peer) -> None:
    app, calls, mode, _, token = peer
    bridge = FreecadBridge(timeout_s=50)
    assert bridge.request("ping") is True
    assert bridge.request("list_documents") == ["Demo"]
    assert bridge.request("get_objects", {"doc_name": "Demo"})[0]["Name"] == "Box"
    mode["reply"] = "sse"
    mode["pending"] = True
    assert bridge.get_object("Demo", "Box")["Properties"]["Length"]["value"] == 10
    assert bridge.request("edit_object", {"doc_name": "Demo", "obj_name": "Box", "property": "Length", "old_value": 10, "value": 20})["success"]
    assert app.doc.Objects[0].Length == 20
    with pytest.raises(FreecadBridgeError) as stale:
        bridge.request("edit_object", {"doc_name": "Demo", "obj_name": "Box", "property": "Length", "old_value": 10, "value": 30})
    assert stale.value.code == "FREECAD_STALE_PREVIEW"
    assert app.doc.Objects[0].Length == 20
    scripts = [item["request"]["params"]["arguments"]["code"] for item in calls if item["request"]["params"]["name"] == "execute_python"]
    assert len(scripts) == 6
    for script in scripts:
        assert script.count("json.loads(") == 1
        assert "JARVIS_FREECAD_RESULT:" in script
        assert token not in script
        assert all(word not in script for word in ("saveDocument(", "closeDocument(", "openDocument(", "recompute("))
    assert all(item["headers"]["Authorization"] == "Bearer " + token for item in calls)


def test_rejections_before_peer_and_token_permissions(peer, monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls, _, token_file, _ = peer
    bridge = FreecadBridge()
    for method in ("execute_python", "delete_object"):
        with pytest.raises(FreecadBridgeError, match="Operazione") as error:
            bridge.request(method)
        assert error.value.code == "FREECAD_METHOD_NOT_ALLOWED"
    for bad in ('Box"', "Box\n", "__import__('os')"):
        with pytest.raises(FreecadBridgeError) as error:
            bridge.get_object("Demo", bad)
        assert error.value.code == "FREECAD_INVALID_IDENTIFIER"
    assert calls == []
    token_file.chmod(0o640)
    with pytest.raises(FreecadBridgeError) as error:
        bridge.request("ping")
    assert error.value.code == "FREECAD_MCP_TOKEN_UNSAFE"
    assert calls == []
    token_file.unlink()
    assert bridge.configuration()["code"] == "FREECAD_MCP_TOKEN_MISSING"
    monkeypatch.setenv("JARVISOS_FREECAD_PYTHON_SHA256", "0" * 64)
    assert bridge.configuration()["code"] == "FREECAD_BRIDGE_DIGEST_MISMATCH"


def test_transport_errors_and_decline(peer) -> None:
    _, _, mode, _, _ = peer
    bridge = FreecadBridge()
    for reply, expected in (("unauthorized", "FREECAD_MCP_UNAUTHORIZED"), ("declined", "FREECAD_MCP_DECLINED")):
        mode["reply"] = reply
        with pytest.raises(FreecadBridgeError) as error:
            bridge.request("ping")
        assert error.value.code == expected


def test_pending_timeout_and_missing_addon(peer, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, mode, _, _ = peer
    mode["pending"] = True
    monkeypatch.setattr(freecad_relay, "_DEADLINE_S", 0.0)
    with pytest.raises(FreecadBridgeError) as pending:
        FreecadBridge().request("ping")
    assert pending.value.code == "FREECAD_MCP_PENDING"

    def refused(*_args: object, **_kwargs: object) -> None:
        raise ConnectionRefusedError()

    monkeypatch.setattr(freecad_relay.http.client, "HTTPConnection", refused)
    monkeypatch.setattr(freecad_relay, "_DEADLINE_S", 45.0)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"id": 1, "method": "ping", "params": {}, "token": "secret"})))
    monkeypatch.setattr(sys, "stdout", output)
    freecad_relay.main()
    assert json.loads(output.getvalue())["error"]["code"] == "FREECAD_ADDON_NOT_RUNNING"
    assert "secret" not in output.getvalue()


def test_routes_preview_stale_and_rollback(peer, isolated_data_root: None) -> None:
    app, calls, mode, _, token = peer
    initialize_storage()
    workspace_id = list_workspaces()[0].id
    assert routes.freecad_status_endpoint(workspace_id)["connected"] is True
    assert routes.freecad_documents_endpoint(workspace_id) == ["Demo"]
    preview = routes.freecad_edit_preview_endpoint(
        workspace_id, routes.FreecadEditRequest(document="Demo", object="Box", property="Length", value=20))
    assert preview["old_value"] == 10
    apply = {"document": "Demo", "object": "Box", "property": "Length", "value": 20, "old_value": 10}
    assert routes.freecad_edit_apply_endpoint(workspace_id, routes.FreecadApplyRequest(**apply))["outcome"]["success"]
    before = len(calls)
    with pytest.raises(HTTPException) as stale:
        routes.freecad_edit_apply_endpoint(workspace_id, routes.FreecadApplyRequest(**{**apply, "value": 30}))
    assert stale.value.detail["code"] == "FREECAD_STALE_PREVIEW"
    assert not any(item["request"]["params"]["name"] == "execute_python" and "edit_object" in item["request"]["params"]["arguments"]["code"] for item in calls[before:])
    failed = routes.freecad_edit_apply_endpoint(workspace_id, routes.FreecadApplyRequest(**{**apply, "old_value": 20, "value": 99}))
    assert failed["document_unchanged"] is True
    assert failed["outcome"]["mutation_outcome"] == "rolled_back"
    assert app.doc.Objects[0].Length == 20
    assert token not in json.dumps(failed)
    assert token not in json.dumps(routes.freecad_status_endpoint(workspace_id))


def test_relay_guard_and_no_xmlrpc() -> None:
    with pytest.raises(ValueError, match="FREECAD_METHOD_NOT_ALLOWED"):
        freecad_relay._script("execute_python", {})
    source = Path(freecad_relay.__file__).read_text(encoding="utf-8")
    assert "xmlrpc" not in source
    for bad in ("Box'", "Box\n", "x;print(1)"):
        with pytest.raises(ValueError, match="FREECAD_INVALID_IDENTIFIER"):
            freecad_relay._script("get_object", {"doc_name": "Demo", "obj_name": bad})
    value = '20"; __import__("os").system("false")\n'
    script = freecad_relay._script("edit_object", {
        "doc_name": "Demo", "obj_name": "Box", "property": "Length", "old_value": 10, "value": value,
    }, expires_at=0.0)
    literal = script.split("_params = json.loads(", 1)[1].split("\n", 1)[0].removesuffix(")")
    assert json.loads(json.loads(literal))["value"] == value
    assert script.count("json.loads(") == 1
    with pytest.raises(ValueError, match="FREECAD_INVALID_EDIT"):
        freecad_relay._script("edit_object", {
            "doc_name": "Demo", "obj_name": "Box", "property": "Length", "old_value": 10, "value": float("nan"),
        })


def test_late_approval_never_applies_and_unconfirmed_rollback_is_unknown(
    peer, isolated_data_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _, _, _ = peer
    initialize_storage()
    workspace_id = list_workspaces()[0].id
    box = app.doc.Objects[0]
    apply = {"document": "Demo", "object": "Box", "property": "Length", "value": 20, "old_value": 10}
    # The edit window has closed by the time FreeCAD runs the script (a late approval).
    monkeypatch.setattr(freecad_relay, "_EDIT_WINDOW_S", -1.0)
    with pytest.raises(HTTPException) as expired:
        routes.freecad_edit_apply_endpoint(workspace_id, routes.FreecadApplyRequest(**apply))
    assert expired.value.detail["code"] == "FREECAD_EDIT_EXPIRED"
    assert box.Length == 10
    monkeypatch.setattr(freecad_relay, "_EDIT_WINDOW_S", 30.0)
    with pytest.raises(HTTPException) as unknown:
        routes.freecad_edit_apply_endpoint(workspace_id, routes.FreecadApplyRequest(**{**apply, "value": 77}))
    assert unknown.value.detail["code"] == "FREECAD_ROLLBACK_FAILED"
    assert box.Length == 11
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT payload FROM events WHERE event_type = 'FreecadPropertyEditApplied'"
        ).fetchall()
    outcomes = [json.loads(row[0])["outcome"] for row in rows]
    assert {"success": False, "mutation_outcome": "unchanged", "code": "FREECAD_EDIT_EXPIRED"} in outcomes
    assert {"success": False, "mutation_outcome": "unknown", "code": "FREECAD_ROLLBACK_FAILED"} in outcomes

    class TimedOutBridge(FreecadBridge):
        def request(self, method: str, params: dict | None = None) -> object:
            if method == "edit_object":
                raise FreecadBridgeError("FREECAD_BRIDGE_TIMEOUT", "timeout", 504)
            return super().request(method, params)

    monkeypatch.setattr(routes, "_freecad_bridge", TimedOutBridge)
    with pytest.raises(HTTPException):
        routes.freecad_edit_apply_endpoint(workspace_id, routes.FreecadApplyRequest(**{**apply, "old_value": 11}))
    with open_sqlite_connection() as connection:
        last = connection.execute(
            "SELECT payload FROM events WHERE event_type = 'FreecadPropertyEditApplied' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert json.loads(last[0])["outcome"] == {
        "success": False, "mutation_outcome": "unknown", "code": "FREECAD_BRIDGE_TIMEOUT"
    }


def test_relay_path_accepts_wsl_unc_path_for_windows_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.bluecad import freecad_bridge as fb

    unc = r"\\wsl.localhost\Ubuntu\home\developer\sample\backend\app\modules\bluecad\freecad_relay.py"
    monkeypatch.setattr(
        fb.subprocess,
        "run",
        lambda *args, **kwargs: type("R", (), {"returncode": 0, "stdout": unc + "\n"})(),
    )
    assert fb._relay_path(Path("/mnt/c/Python312/python.exe")) == unc
