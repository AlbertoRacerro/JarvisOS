"""Deterministic revision and failure-boundary checks for the 149 editor."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from app.modules.process_stack import editor
from app.modules.process_stack.editor_models import EditorCommand


def _initial(directory: Path) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=True)
    case = directory / "seed.dwxmz"
    with zipfile.ZipFile(case, "w") as archive:
        archive.writestr(
            "case.xml", "<Simulation><GraphicObject><Name>id-1</Name><Tag>Unit1</Tag></GraphicObject></Simulation>"
        )
    return editor._write_revision(
        directory, case, command="create_case", parent=None, readback={}, version="10.2.9", mcp_sha="a" * 64
    )


class _FakeClient:
    def __init__(self, *, wrong_position: bool = False) -> None:
        self.wrong_position = wrong_position
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def call(self, name, args, _timeout):
        self.calls.append(name)
        if name == "dwsim_flowsheet_load":
            return {"flowsheet_id": "flow"}
        if name == "dwsim_graphic_edit":
            return {"ok": True}
        if name == "dwsim_flowsheet_list_objects":
            return {
                "objects": [
                    {"id": "id-1", "name": "Unit1", "type": "Mixer", "x": 1 if self.wrong_position else 20, "y": 30}
                ]
            }
        if name == "dwsim_flowsheet_save":
            # Native file save is not reached in the mismatch test.
            Path(args["filepath"]).write_bytes(b"fake native case")
            return {"ok": True}
        return {}


def test_stale_revision_is_rejected_before_mcp_execution(tmp_path, monkeypatch):
    directory = tmp_path / "case"
    record = _initial(directory)
    monkeypatch.setattr(editor, "_directory", lambda *_args: directory)
    monkeypatch.setattr(editor, "_client", lambda: pytest.fail("stale command must not start MCP"))
    command = TypeAdapter(EditorCommand).validate_python(
        {"kind": "move", "expected_revision": "0" * 64 + ":1", "object": "Unit1", "x": 20, "y": 30}
    )
    with pytest.raises(editor.EditorError) as error:
        editor.execute("workspace", "case", command)
    assert error.value.status == 409
    assert error.value.current_revision == record["revision"]
    assert len(list((directory / "revisions").iterdir())) == 1


def test_readback_mismatch_does_not_create_revision(tmp_path, monkeypatch):
    directory = tmp_path / "case"
    record = _initial(directory)
    fake = _FakeClient(wrong_position=True)
    monkeypatch.setattr(editor, "_directory", lambda *_args: directory)
    monkeypatch.setattr(editor, "_client", lambda: (fake, "a" * 64, "10.2.9"))
    command = TypeAdapter(EditorCommand).validate_python(
        {"kind": "move", "expected_revision": record["revision"], "object": "Unit1", "x": 20, "y": 30}
    )
    with pytest.raises(editor.EditorError, match="position read-back"):
        editor.execute("workspace", "case", command)
    assert editor._head(directory)["revision"] == record["revision"]
    assert len(list((directory / "revisions").iterdir())) == 1


def test_invalid_command_quantity_and_port_are_rejected():
    commands = TypeAdapter(EditorCommand)
    with pytest.raises(ValueError):
        commands.validate_python(
            {"kind": "connect", "expected_revision": "x", "unit": "M1", "stream": "F1", "role": "feed", "port": -1}
        )
    with pytest.raises(ValueError):
        commands.validate_python(
            {"kind": "set_stream_conditions", "expected_revision": "x", "stream": "F1", "temperature": {"unit": "degC"}}
        )


def test_command_manifest_and_head_are_separate(tmp_path):
    directory = tmp_path / "case"
    record = _initial(directory)
    persisted = json.loads((directory / "head.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "records" / "1.json").read_text(encoding="utf-8"))
    assert persisted == manifest
    assert record["case_sha256"] == editor._sha(editor._case_file(directory, 1, record["case_sha256"]))


def test_restore_adds_revision_without_mutating_history(tmp_path, monkeypatch):
    directory = tmp_path / "case"
    original = _initial(directory)
    original_file = editor._case_file(directory, 1, original["case_sha256"])
    changed = tmp_path / "changed.dwxmz"
    changed.write_bytes(b"new native content")
    second = editor._write_revision(
        directory,
        changed,
        command="move",
        parent=original["revision"],
        readback={},
        version="10.2.9",
        mcp_sha="a" * 64,
    )
    monkeypatch.setattr(editor, "_directory", lambda *_args: directory)
    monkeypatch.setattr(editor, "_client", lambda: (_FakeClient(), "a" * 64, "10.2.9"))
    monkeypatch.setattr(editor, "_projection", lambda *_args: None)
    restored = editor.restore("workspace", "case", second["revision"], original["revision"])
    restored_file = editor._case_file(directory, 3, original["case_sha256"])
    assert restored["case"].revision == f"{original['case_sha256']}:3"
    assert restored_file.read_bytes() == original_file.read_bytes()
    assert original_file.read_bytes().startswith(b"PK")
    assert original_file.exists()


def test_unconfigured_runtime_is_typed_unavailable(monkeypatch):
    monkeypatch.setattr(editor, "_configured_runtime", lambda: (None, "", "DWSIM_MCP_EXECUTABLE_UNAVAILABLE"))
    with pytest.raises(editor.EditorError) as error:
        editor._runtime()
    assert error.value.status == 503
    assert error.value.code == "DWSIM_MCP_EXECUTABLE_UNAVAILABLE"


def test_unknown_workspace_is_scoped_404():
    from fastapi.testclient import TestClient

    from app.core.database import initialize_database
    from app.main import app

    initialize_database()
    with TestClient(app) as client:
        response = client.get("/workspaces/00000000-0000-0000-0000-000000000000/process/dwsim/cases")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "workspace_not_found"



class _StreamClient(_FakeClient):
    def call(self, name, args, timeout):
        if name == "dwsim_flowsheet_list_objects":
            self.calls.append(name)
            return {"objects": [{"id": "id-2", "name": "F1", "type": "MaterialStream", "x": 5, "y": 6}]}
        if name == "dwsim_stream_get_results":
            self.calls.append(name)
            return {"temperature_K": 280.0, "pressure_Pa": 101325.0}
        return super().call(name, args, timeout)


def test_create_stream_condition_mismatch_creates_no_revision(tmp_path, monkeypatch):
    directory = tmp_path / "case"
    record = _initial(directory)
    monkeypatch.setattr(editor, "_directory", lambda *_args: directory)
    monkeypatch.setattr(editor, "_client", lambda: (_StreamClient(), "a" * 64, "10.2.9"))
    command = TypeAdapter(EditorCommand).validate_python(
        {"kind": "create_material_stream", "expected_revision": record["revision"], "tag": "F1", "x": 5, "y": 6,
         "temperature": {"value": 300, "unit": "K"}}
    )
    with pytest.raises(editor.EditorError, match="temperature_K read-back"):
        editor.execute("workspace", "case", command)
    assert editor._head(directory)["revision"] == record["revision"]
    assert len(list((directory / "revisions").iterdir())) == 1


class _StaleRenameClient(_FakeClient):
    def call(self, name, args, timeout):
        if name == "dwsim_flowsheet_list_objects":
            self.calls.append(name)
            return {"objects": [{"id": "id-1", "name": "Unit1", "type": "Mixer"},
                                {"id": "id-9", "name": "M-100", "type": "Mixer"}]}
        return super().call(name, args, timeout)


def test_rename_requires_previous_tag_to_disappear(tmp_path, monkeypatch):
    directory = tmp_path / "case"
    record = _initial(directory)
    monkeypatch.setattr(editor, "_directory", lambda *_args: directory)
    monkeypatch.setattr(editor, "_client", lambda: (_StaleRenameClient(), "a" * 64, "10.2.9"))
    command = TypeAdapter(EditorCommand).validate_python(
        {"kind": "rename", "expected_revision": record["revision"], "object": "Unit1", "new_tag": "M-100"}
    )
    with pytest.raises(editor.EditorError, match="previous tag"):
        editor.execute("workspace", "case", command)
    assert editor._head(directory)["revision"] == record["revision"]


def test_revision_listing_ignores_uncommitted_orphans(tmp_path, monkeypatch):
    directory = tmp_path / "case"
    record = _initial(directory)
    (directory / "revisions" / f"2-{'b' * 64}.dwxmz").write_bytes(b"orphan")
    (directory / "records" / "2.json").write_text(json.dumps({**record, "seq": 2}), encoding="utf-8")
    monkeypatch.setattr(editor, "_directory", lambda *_args: directory)
    assert [row.revision for row in editor.list_revisions("workspace", "case")] == [record["revision"]]


def test_workspace_path_segments_are_rejected_before_lookup(monkeypatch):
    monkeypatch.setattr(editor, "get_workspace", lambda _workspace_id: pytest.fail("lookup must not run"))
    for workspace_id in ("..", "a/b", "a\\b", ""):
        with pytest.raises(editor.EditorError) as error:
            editor._directory(workspace_id)
        assert error.value.status == 404


def test_import_size_is_bounded(monkeypatch):
    monkeypatch.setattr(editor, "MAX_IMPORT_BYTES", 4)
    monkeypatch.setattr(editor, "_directory", lambda *_args: pytest.fail("oversized upload must not touch storage"))
    with pytest.raises(editor.EditorError) as error:
        editor.import_case("workspace", "case.dwxmz", b"12345")
    assert error.value.status == 413


class _DynamicsMismatchClient(_FakeClient):
    def call(self, name, args, timeout):
        self.calls.append(name)
        if name == "dwsim_flowsheet_load":
            return {"flowsheet_id": "flow"}
        if name == "dwsim_dynamics_controller":
            return {"controllers": [{"tag": args.get("tag", "PID"), "sp": "9"}]}
        if name == "dwsim_dynamics_event":
            if args.get("action") == "list" and args.get("event_set") == "events":
                return {"events": ["remove me"]}
            return {"events": []}
        if name == "dwsim_dynamics_inspect":
            return {"objects": [{"tag": "Unit1"}]}
        if name == "dwsim_dynamics_properties":
            return {"properties": [{"id": "P", "value": "1"}]}
        if name == "dwsim_dynamics_state":
            return {"stored_states": []}
        if name == "dwsim_dynamics_run":
            return {"run_id": "run", "state": "completed"}
        if name == "dwsim_dynamics_status":
            return {"state": "completed", "summary": {"simulated_s": "1"}}
        if name == "dwsim_dynamics_series":
            return {"t_s": [], "series": {}, "points": 0}
        return super().call(name, args, timeout)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "controller_set", "tag": "PID", "sp": 2},
        {"kind": "event_add", "event_set": "events", "tag": "Feed", "property": "P", "value": 2, "at_s": 1},
        {"kind": "event_remove", "event_set": "events", "description": "remove me"},
        {"kind": "state_save", "name": "snapshot"},
        {"kind": "dynamics_run", "duration_s": 1},
    ],
)
def test_dynamic_readback_mismatch_does_not_create_revision(tmp_path, monkeypatch, payload):
    directory = tmp_path / "case"
    record = _initial(directory)
    fake = _DynamicsMismatchClient()
    monkeypatch.setattr(editor, "_directory", lambda *_args: directory)
    monkeypatch.setattr(editor, "_client", lambda: (fake, "a" * 64, "10.2.9"))
    command = TypeAdapter(EditorCommand).validate_python({**payload, "expected_revision": record["revision"]})
    with pytest.raises(editor.EditorError):
        editor.execute("workspace", "case", command)
    assert editor._head(directory)["revision"] == record["revision"]
    assert len(list((directory / "revisions").iterdir())) == 1


def test_dynamics_caps_reject_before_mcp(monkeypatch):
    command = TypeAdapter(EditorCommand).validate_python(
        {"kind": "dynamics_run", "expected_revision": "stale", "duration_s": editor.MAX_DYNAMIC_DURATION_S + 1}
    )
    monkeypatch.setattr(editor, "_client", lambda: pytest.fail("capped command must not start MCP"))
    with pytest.raises(editor.EditorError) as error:
        editor.execute("workspace", "case", command)
    assert error.value.status == 422


def test_state_restore_readback_mismatch_does_not_create_revision(tmp_path, monkeypatch):
    directory = tmp_path / "case"
    record = _initial(directory)
    fake = _DynamicsMismatchClient()
    monkeypatch.setattr(editor, "_directory", lambda *_args: directory)
    monkeypatch.setattr(editor, "_client", lambda: (fake, "a" * 64, "10.2.9"))
    monkeypatch.setattr(editor, "_saved_state_snapshot", lambda *_args: {"Unit1": {"P": "9"}})
    command = TypeAdapter(EditorCommand).validate_python(
        {"kind": "state_restore", "expected_revision": record["revision"], "name": "snapshot"}
    )
    with pytest.raises(editor.EditorError, match="Restored state"):
        editor.execute("workspace", "case", command)
    assert editor._head(directory)["revision"] == record["revision"]
    assert len(list((directory / "revisions").iterdir())) == 1


def test_dynamic_projection_reports_read_only_tool_data():
    class ProjectionClient:
        def call(self, name, args, _timeout):
            if name == "dwsim_dynamics_inspect":
                return {"current_schedule": "S", "event_sets": ["E"]}
            if name == "dwsim_dynamics_controller":
                return {"controllers": [{"tag": "PID"}]}
            if name == "dwsim_dynamics_state":
                return {"stored_states": ["Initial"]}
            if name == "dwsim_dynamics_event":
                return {"event_set": "E", "events": ["t = 1 s"]}
            raise AssertionError(name)

    result = editor._dynamic_projection(ProjectionClient(), "flow", {"command_kind": "create_case"})
    assert result["current_schedule"] == "S"
    assert result["controllers"] == [{"tag": "PID"}]
    assert result["saved_states"] == ["Initial"]
    assert result["event_sets"] == [{"event_set": "E", "events": ["t = 1 s"]}]


@pytest.mark.skipif(
    not os.environ.get("JARVISOS_DWSIM_MCP_PATH"), reason="set JARVISOS_DWSIM_MCP_PATH to opt in to DWSIM runtime"
)
def test_opt_in_runtime_creates_editor_case(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.core.database import initialize_database
    from app.main import app

    runtime = Path(os.environ["JARVISOS_DWSIM_MCP_PATH"])
    monkeypatch.setenv("JARVISOS_DWSIM_MCP_SHA256", hashlib.sha256(runtime.read_bytes()).hexdigest())
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "editor-data"))
    get_settings.cache_clear()
    initialize_database()
    with TestClient(app) as client:
        workspace = client.post("/workspaces", json={"name": "editor runtime test", "slug": "editor-runtime-test"})
        assert workspace.status_code == 201
        response = client.post(f"/workspaces/{workspace.json()['id']}/process/dwsim/cases")
        assert response.status_code == 200, response.text
        assert response.json()["revision"].endswith(":1")
    get_settings.cache_clear()


class _StaticEventClient(_FakeClient):
    """DWSIM accepts the add call but its event list never changes."""

    def call(self, name, args, timeout):
        if name == "dwsim_dynamics_event":
            self.calls.append(f"event:{args['action']}")
            return {"events": [{"description": "Feed step"}]} if args["action"] == "list" else {"ok": True}
        return super().call(name, args, timeout)


def test_event_add_with_preexisting_description_requires_a_new_event(tmp_path, monkeypatch):
    directory = tmp_path / "case"
    record = _initial(directory)
    monkeypatch.setattr(editor, "_directory", lambda *_args: directory)
    monkeypatch.setattr(editor, "_client", lambda: (_StaticEventClient(), "a" * 64, "10.2.9"))
    command = TypeAdapter(EditorCommand).validate_python(
        {"kind": "event_add", "expected_revision": record["revision"], "event_set": "S1", "tag": "FEED",
         "property": "PROP_MS_2", "value": 5, "at_s": 10, "description": "Feed step"}
    )
    with pytest.raises(editor.EditorError, match="event list did not match"):
        editor.execute("workspace", "case", command)
    assert editor._head(directory)["revision"] == record["revision"]
    with pytest.raises(ValueError):
        TypeAdapter(EditorCommand).validate_python(
            {"kind": "event_remove", "expected_revision": "x", "event_set": "S1", "description": ""}
        )
