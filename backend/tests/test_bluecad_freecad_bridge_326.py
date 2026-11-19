from __future__ import annotations

import hashlib
import json
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.main import create_app
from app.modules.bluecad.freecad_bridge import FreecadBridge, FreecadBridgeError
from app.modules.workspaces.service import list_workspaces


def _fake_peer(path: Path, log: Path, state: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json, os, sys
log = os.environ["FREECAD_METHOD_LOG"]
state_path = os.environ["FREECAD_STATE"]
state = json.loads(open(state_path).read())
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    with open(log, "a", encoding="utf-8") as out:
        out.write(method + "\\n")
    if os.environ.get("FREECAD_NOT_RUNNING"):
        response = {"id": request["id"], "ok": False, "error": {"code": "FREECAD_ADDON_NOT_RUNNING"}}
    elif method not in {"ping", "list_documents", "get_objects", "get_object", "edit_object"}:
        response = {"id": request["id"], "ok": False, "error": {"code": "FREECAD_METHOD_NOT_ALLOWED"}}
    elif method == "ping":
        response = {"id": request["id"], "ok": True, "result": True}
    elif method == "list_documents":
        response = {"id": request["id"], "ok": True, "result": ["Demo"]}
    elif method == "get_objects":
        response = {"id": request["id"], "ok": True, "result": [{
            "Name": "Box", "Label": "Box", "TypeId": "Part::Box",
            "Properties": {"Length": state["Length"]},
        }]}
    elif method == "get_object":
        response = {"id": request["id"], "ok": True, "result": {
            "Name": "Box", "Label": "Box", "TypeId": "Part::Box",
            "Properties": {"Length": state["Length"], "ReadOnly": {"value": 1, "read_only": True}},
        }}
    else:
        properties = request["params"]["properties"]["Properties"]
        if properties.get("Length") == 99:
            response = {"id": request["id"], "ok": True, "result": {
                "success": False, "error": "invalid", "mutation_outcome": "rolled_back",
            }}
        else:
            state["Length"] = properties["Length"]
            open(state_path, "w", encoding="utf-8").write(json.dumps(state))
            response = {"id": request["id"], "ok": True, "result": {"success": True, "object_name": "Box"}}
    print(json.dumps(response), flush=True)
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    log.write_text("", encoding="utf-8")
    state.write_text(json.dumps({"Length": 10}), encoding="utf-8")
    return path


@pytest.fixture
def peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    executable = _fake_peer(tmp_path / "fake-python", tmp_path / "methods.log", tmp_path / "state.json")
    monkeypatch.setenv("JARVISOS_FREECAD_PYTHON_PATH", str(executable))
    monkeypatch.setenv("JARVISOS_FREECAD_PYTHON_SHA256", hashlib.sha256(executable.read_bytes()).hexdigest())
    monkeypatch.setenv("FREECAD_METHOD_LOG", str(tmp_path / "methods.log"))
    monkeypatch.setenv("FREECAD_STATE", str(tmp_path / "state.json"))
    return executable, tmp_path / "methods.log", tmp_path / "state.json"


def test_allowlist_and_digest_reasons(peer: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FreecadBridge()
    with pytest.raises(FreecadBridgeError) as refused:
        bridge.request("execute_code")
    assert refused.value.code == "FREECAD_METHOD_NOT_ALLOWED"
    assert peer[1].read_text() == ""
    with pytest.raises(FreecadBridgeError) as refused_delete:
        bridge.request("delete_object")
    assert refused_delete.value.code == "FREECAD_METHOD_NOT_ALLOWED"
    assert peer[1].read_text() == ""
    monkeypatch.setenv("JARVISOS_FREECAD_PYTHON_SHA256", "0" * 64)
    with pytest.raises(FreecadBridgeError) as mismatch:
        bridge.request("ping")
    assert mismatch.value.code == "FREECAD_BRIDGE_DIGEST_MISMATCH"
    monkeypatch.setenv("JARVISOS_FREECAD_PYTHON_SHA256", "")
    with pytest.raises(FreecadBridgeError) as unconfigured:
        bridge.request("ping")
    assert unconfigured.value.code == "FREECAD_BRIDGE_DIGEST_UNCONFIGURED"
    monkeypatch.delenv("JARVISOS_FREECAD_PYTHON_PATH")
    assert bridge.configuration()["code"] == "FREECAD_BRIDGE_UNAVAILABLE"


def test_missing_addon_is_reported(peer: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREECAD_NOT_RUNNING", "1")
    with pytest.raises(FreecadBridgeError) as missing:
        FreecadBridge().request("ping")
    assert missing.value.code == "FREECAD_ADDON_NOT_RUNNING"


def test_routes_validate_preview_stale_and_failed_apply(
    peer: tuple[Path, Path, Path], isolated_data_root: None
) -> None:
    initialize_storage()
    workspace_id = list_workspaces()[0].id
    with TestClient(create_app()) as client:
        status = client.get(f"/workspaces/{workspace_id}/bluecad/freecad/status")
        assert status.json()["connected"] is True
        assert client.get(f"/workspaces/{workspace_id}/bluecad/freecad/documents").json() == ["Demo"]
        preview = client.post(
            f"/workspaces/{workspace_id}/bluecad/freecad/edit/preview",
            json={"document": "Demo", "object": "Box", "property": "Length", "value": 20},
        )
        assert preview.status_code == 200
        assert preview.json()["old_value"] == 10
        assert "edit_object" not in peer[1].read_text()
        applied = client.post(
            f"/workspaces/{workspace_id}/bluecad/freecad/edit/apply",
            json={"document": "Demo", "object": "Box", "property": "Length", "value": 20,
                  "old_value": preview.json()["old_value"]},
        )
        assert applied.status_code == 200
        assert applied.json()["outcome"]["success"] is True
        stale = client.post(
            f"/workspaces/{workspace_id}/bluecad/freecad/edit/apply",
            json={"document": "Demo", "object": "Box", "property": "Length", "value": 30, "old_value": 10},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "FREECAD_STALE_PREVIEW"
        failed = client.post(
            f"/workspaces/{workspace_id}/bluecad/freecad/edit/apply",
            json={"document": "Demo", "object": "Box", "property": "Length", "value": 99, "old_value": 20},
        )
        assert failed.status_code == 200
        assert failed.json()["document_unchanged"] is True
        assert failed.json()["outcome"] == {"success": False, "mutation_outcome": "rolled_back"}
        assert "non è stato modificato" in failed.json()["message"]


def _run_relay(*requests: dict) -> list[dict]:
    relay = Path(__file__).resolve().parents[1] / "app" / "modules" / "bluecad" / "freecad_relay.py"
    completed = subprocess.run(
        [sys.executable, str(relay)],
        input="".join(json.dumps(item) + "\n" for item in requests),
        capture_output=True, text=True, timeout=30, check=True,
    )
    return [json.loads(line) for line in completed.stdout.splitlines()]


def test_relay_refuses_methods_outside_the_allowlist_without_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.bluecad import freecad_relay

    def no_proxy(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the relay must not connect for a refused method")

    monkeypatch.setattr(freecad_relay.xmlrpc.client, "ServerProxy", no_proxy)
    for method in ("execute_code", "execute_code_async", "delete_object", "create_object", "reload_document"):
        with pytest.raises(ValueError, match="FREECAD_METHOD_NOT_ALLOWED"):
            freecad_relay._call(method, {})
    responses = _run_relay({"id": 1, "method": "execute_code", "params": {"code": "print(1)"}}, {"id": 2})
    assert responses == [
        {"id": 1, "ok": False, "error": {"code": "FREECAD_METHOD_NOT_ALLOWED", "message": "FreeCAD request failed."}},
        {"id": 2, "ok": False, "error": {"code": "FREECAD_INVALID_REQUEST", "message": "FreeCAD request failed."}},
    ]


def test_relay_reports_a_missing_addon() -> None:
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 9875)) == 0:
            pytest.skip("a listener occupies the add-on port on this machine")
    [response] = _run_relay({"id": 3, "method": "ping", "params": {}})
    assert response["ok"] is False
    assert response["error"]["code"] == "FREECAD_ADDON_NOT_RUNNING"


def test_relay_forwards_typed_edit_to_the_addon(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.bluecad import freecad_relay

    calls: list[tuple] = []

    class Proxy:
        def __init__(self, url: str, **_kwargs: object) -> None:
            assert url == "http://127.0.0.1:9875"

        def edit_object(self, *args: object) -> dict:
            calls.append(args)
            return {"success": True}

    monkeypatch.setattr(freecad_relay.xmlrpc.client, "ServerProxy", Proxy)
    params = {"doc_name": "Demo", "obj_name": "Box", "properties": {"Properties": {"Length": "20 mm"}}}
    assert freecad_relay._call("edit_object", params) == {"success": True}
    assert calls == [("Demo", "Box", {"Properties": {"Length": "20 mm"}})]
