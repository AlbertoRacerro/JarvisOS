"""Spec 165: BLUECAD mm 3MF export and opt-in WSL slicer handoff."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.modules.bluecad import print_handoff
from app.modules.bluecad.export import build_artifacts, sha256_file

FIXTURE = Path(__file__).parent / "fixtures" / "chain_tube_bend_joint.json"
TUBE = {"template": "tube", "params": {"outer_d_mm": 40.0, "wall_t_mm": 3.0, "length_mm": 120.0}}
EXPLORER = "/mnt/c/Windows/explorer.exe"


def _spec() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _read_3mf(path: Path):
    import lib3mf

    wrapper = lib3mf.get_wrapper()
    model = wrapper.CreateModel()
    model.QueryReader("3mf").ReadFromFile(str(path))
    meshes = []
    iterator = model.GetMeshObjects()
    while iterator.MoveNext():
        meshes.append(iterator.GetCurrentMeshObject())
    return lib3mf, model, meshes


def _mesh_stats(mesh) -> tuple[list[float], list[float], float]:
    vertices = [tuple(v.Coordinates) for v in mesh.GetVertices()]
    mins = [min(v[i] for v in vertices) for i in range(3)]
    maxs = [max(v[i] for v in vertices) for i in range(3)]
    volume = 0.0
    for tri in mesh.GetTriangleIndices():
        a, b, c = (vertices[i] for i in tri.Indices)
        volume += (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6.0
    return mins, maxs, abs(volume)


def test_3mf_is_millimetre_one_object_per_part_and_matches_manifest(tmp_path: Path) -> None:
    manifest = build_artifacts(_spec(), tmp_path)
    lib3mf, model, meshes = _read_3mf(tmp_path / "model.3mf")

    assert model.GetUnit() == lib3mf.ModelUnit.MilliMeter
    part_ids = sorted(manifest["parts"])
    assert sorted(mesh.GetName() for mesh in meshes) == part_ids
    assert sorted(mesh.GetPartNumber() for mesh in meshes) == part_ids

    stats = [_mesh_stats(mesh) for mesh in meshes]
    mins = [min(s[0][i] for s in stats) for i in range(3)]
    maxs = [max(s[1][i] for s in stats) for i in range(3)]
    bbox = manifest["assembly"]["bbox_mm"]
    for axis in range(3):
        assert mins[axis] == pytest.approx(bbox["min"][axis], abs=0.1)  # angular_deflection 0.1 rad bounds the chord error on curved parts
        assert maxs[axis] == pytest.approx(bbox["max"][axis], abs=0.1)  # angular_deflection 0.1 rad bounds the chord error on curved parts
    total = sum(s[2] for s in stats)
    assert total == pytest.approx(manifest["assembly"]["total_volume_mm3"], rel=0.005)

    block = manifest["print_handoff"]
    assert block["unit"] == "mm"
    assert block["object_count"] == len(part_ids)
    assert sorted(block["objects"]) == part_ids
    assert (block["linear_deflection"], block["angular_deflection"]) == (0.001, 0.1)
    assert re.fullmatch(r"[0-9a-f]{64}", block["mesh_digest"])
    assert manifest["artifacts"]["model.3mf"]["sha256"] == sha256_file(tmp_path / "model.3mf")


def test_two_builds_have_equal_mesh_digest(tmp_path: Path) -> None:
    first = build_artifacts(_spec(), tmp_path / "a")
    second = build_artifacts(_spec(), tmp_path / "b")
    # Measured on lib3mf 2.5.0: raw writer output differs between identical builds
    # only through random p:UUID values. The exporter normalises them, but the
    # determinism contract is mesh_digest, so file bytes are deliberately not asserted.
    assert first["print_handoff"]["mesh_digest"] == second["print_handoff"]["mesh_digest"]
    assert first["print_handoff"] == second["print_handoff"]


# --- registration, serving and the open-in-slicer endpoint -------------------------------------


@pytest.fixture
def client(isolated_data_root) -> TestClient:
    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def built(client: TestClient) -> dict:
    workspace_id = client.get("/workspaces").json()[0]["id"]
    candidate = client.post(f"/workspaces/{workspace_id}/bluecad/candidates/from-template", json=TUBE).json()
    aggregate = client.get(f"/workspaces/{workspace_id}/bluecad/candidates/{candidate['id']}/aggregate").json()
    exports = {item["roles"][0]: item for item in aggregate["exports"]}
    return {"workspace_id": workspace_id, "candidate": candidate, "exports": exports}


def _artifact_row(artifact_id: str):
    with open_sqlite_connection() as connection:
        return connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()


def test_3mf_registered_served_and_hash_matches_manifest(client: TestClient, built: dict) -> None:
    workspace_id, candidate = built["workspace_id"], built["candidate"]
    exports = built["exports"]
    assert {"export.stl", "export.step", "export.3mf"} <= set(exports)
    item = exports["export.3mf"]
    served = client.get(item["content_url"])
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("model/3mf")
    assert f'filename="bluecad-{candidate["id"][:8]}-attempt1.3mf"' in served.headers["content-disposition"]
    assert served.content[:2] == b"PK"

    row = _artifact_row(item["id"])
    attempt = candidate["attempts"][0]
    manifest_row = _artifact_row(attempt["manifest_artifact_id"])
    manifest = json.loads(Path(manifest_row["stored_path"]).read_text(encoding="utf-8"))
    assert manifest["artifacts"]["model.3mf"]["sha256"] == row["sha256"]
    for role, name in (("export.stl", "model.stl"), ("export.step", "model.step")):
        assert manifest["artifacts"][name]["sha256"] == _artifact_row(exports[role]["id"])["sha256"]
    assert client.get(f"/workspaces/{workspace_id}/bluecad/artifacts/{exports['export.stl']['id']}/content").status_code == 200


class _Recorder:
    def __init__(self, windows_path: str = "C:\\handoff\\out.3mf", wsl_stdout: str | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.windows_path = windows_path if wsl_stdout is None else wsl_stdout

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[0] == "wslpath":
            return subprocess.CompletedProcess(argv, 0, stdout=self.windows_path + "\n", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")


@pytest.fixture
def handoff_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target = tmp_path / "slicer"
    target.mkdir()
    monkeypatch.setenv("JARVISOS_PRINT_HANDOFF_ENABLED", "1")
    monkeypatch.setenv("JARVISOS_PRINT_HANDOFF_DIR", str(target))
    get_settings.cache_clear()
    monkeypatch.setattr(print_handoff, "running_under_wsl", lambda: True)
    monkeypatch.setattr(print_handoff, "_HANDOFF_DIR", re.compile(r"^/.+"))
    monkeypatch.setattr(print_handoff, "_explorer_binary", lambda: EXPLORER)
    recorder = _Recorder()
    monkeypatch.setattr(print_handoff.subprocess, "run", recorder)
    return {"dir": target.resolve(), "recorder": recorder, "monkeypatch": monkeypatch}


def _open(client: TestClient, workspace_id: str, artifact_id: str):
    return client.post(f"/workspaces/{workspace_id}/bluecad/artifacts/{artifact_id}/open-in-slicer")


def _events() -> list:
    with open_sqlite_connection() as connection:
        return connection.execute("SELECT * FROM print_handoff_events").fetchall()


def test_open_in_slicer_success_argv_and_event_row(client: TestClient, built: dict, handoff_env: dict) -> None:
    item = built["exports"]["export.3mf"]
    response = _open(client, built["workspace_id"], item["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "handed_to_windows"
    sha = _artifact_row(item["id"])["sha256"]
    expected = handoff_env["dir"] / f"{built['candidate']['id'][:8]}-attempt1-{sha[:8]}.3mf"
    assert expected.is_file() and sha256_file(expected) == sha
    calls = handoff_env["recorder"].calls
    assert calls[0][0] == ["wslpath", "-w", str(expected)]
    explorer_argv, kwargs = calls[1]
    assert explorer_argv == [EXPLORER, "C:\\handoff\\out.3mf"]
    assert kwargs["shell"] is False and kwargs["timeout"] == 15
    assert all(kw.get("shell") is False for _, kw in calls)
    [row] = _events()
    assert (row["workspace_id"], row["artifact_id"], row["sha256"]) == (built["workspace_id"], item["id"], sha)
    assert row["windows_path"] == "C:\\handoff\\out.3mf" and row["handoff_path"] == str(expected)
    assert row["outcome"] == "handed_to_windows"
    capability = client.get(f"/workspaces/{built['workspace_id']}/bluecad/print-handoff/capability").json()
    assert capability == {"available": True, "code": None}


@pytest.mark.parametrize(
    ("setup", "code"),
    [("flag", "print_handoff_disabled"), ("wsl", "print_handoff_not_wsl"), ("dir", "print_handoff_dir_unset")],
)
def test_gates_refuse_with_409_and_do_nothing(client: TestClient, built: dict, handoff_env: dict, setup: str, code: str) -> None:
    mp = handoff_env["monkeypatch"]
    if setup == "flag":
        mp.setenv("JARVISOS_PRINT_HANDOFF_ENABLED", "0")
    elif setup == "wsl":
        mp.setattr(print_handoff, "running_under_wsl", lambda: False)
    else:
        mp.delenv("JARVISOS_PRINT_HANDOFF_DIR")
    get_settings.cache_clear()

    response = _open(client, built["workspace_id"], built["exports"]["export.3mf"]["id"])

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    assert handoff_env["recorder"].calls == []
    assert list(handoff_env["dir"].iterdir()) == []
    assert _events() == []
    assert client.get(f"/workspaces/{built['workspace_id']}/bluecad/print-handoff/capability").json()["available"] is False


def test_refuses_wrong_type_foreign_workspace_hash_mismatch_and_symlink(client: TestClient, built: dict, handoff_env: dict) -> None:
    ws = built["workspace_id"]
    item = built["exports"]["export.3mf"]
    assert _open(client, ws, built["exports"]["export.stl"]["id"]).json()["detail"]["code"] == "print_handoff_not_3mf"
    assert _open(client, "other-workspace", item["id"]).status_code == 404
    assert _open(client, ws, str(uuid.uuid4())).status_code == 404

    sha = _artifact_row(item["id"])["sha256"]
    symlink = handoff_env["dir"] / f"{built['candidate']['id'][:8]}-attempt1-{sha[:8]}.3mf"
    victim = handoff_env["dir"].parent / "victim.txt"
    victim.write_text("keep")
    os.symlink(victim, symlink)
    response = _open(client, ws, item["id"])
    assert response.status_code == 409 and response.json()["detail"]["code"] == "print_handoff_unsafe_target"
    assert victim.read_text() == "keep"
    symlink.unlink()

    stored = Path(_artifact_row(item["id"])["stored_path"])
    stored.write_bytes(stored.read_bytes() + b"tamper")
    response = _open(client, ws, item["id"])
    assert response.status_code == 409 and response.json()["detail"]["code"] == "print_handoff_hash_mismatch"
    assert not symlink.exists()
    assert handoff_env["recorder"].calls == [] and _events() == []


def test_handoff_does_not_overwrite_a_hard_linked_file(client: TestClient, built: dict, handoff_env: dict) -> None:
    item = built["exports"]["export.3mf"]
    sha = _artifact_row(item["id"])["sha256"]
    target = handoff_env["dir"] / f"{built['candidate']['id'][:8]}-attempt1-{sha[:8]}.3mf"
    victim = handoff_env["dir"].parent / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    os.link(victim, target)

    response = _open(client, built["workspace_id"], item["id"])

    assert response.status_code == 200
    assert victim.read_text(encoding="utf-8") == "keep"
    assert sha256_file(target) == sha


def test_directory_swap_during_copy_cannot_redirect_handoff(client: TestClient, built: dict, handoff_env: dict) -> None:
    directory = handoff_env["dir"]
    original = directory.with_name("original-slicer")
    victim = directory.with_name("other-slicer")
    victim.mkdir()
    copy = print_handoff._copy_verified

    def swap_then_copy(source, filename, directory_fd, expected_sha256):
        directory.rename(original)
        os.symlink(victim, directory)
        return copy(source, filename, directory_fd, expected_sha256)

    handoff_env["monkeypatch"].setattr(print_handoff, "_copy_verified", swap_then_copy)
    item = built["exports"]["export.3mf"]
    response = _open(client, built["workspace_id"], item["id"])

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "print_handoff_unsafe_target"
    assert list(victim.iterdir()) == []
    assert len(list(original.glob("*.3mf"))) == 1
    assert all(argv[0] != EXPLORER for argv, _ in handoff_env["recorder"].calls)
    assert _events() == []


def test_replaced_destination_before_launch_is_refused(client: TestClient, built: dict, handoff_env: dict) -> None:
    convert = print_handoff._to_windows_path

    def replace_after_conversion(destination):
        windows_path = convert(destination)
        replacement = destination.with_suffix(".replacement")
        replacement.write_bytes(b"other 3mf bytes")
        os.replace(replacement, destination)
        return windows_path

    handoff_env["monkeypatch"].setattr(print_handoff, "_to_windows_path", replace_after_conversion)
    item = built["exports"]["export.3mf"]
    response = _open(client, built["workspace_id"], item["id"])

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "print_handoff_unsafe_target"
    assert all(argv[0] != EXPLORER for argv, _ in handoff_env["recorder"].calls)
    assert _events() == []


def test_wslpath_result_must_be_a_drive_path(client: TestClient, built: dict, handoff_env: dict) -> None:
    handoff_env["monkeypatch"].setattr(print_handoff.subprocess, "run", _Recorder(wsl_stdout="\\\\wsl$\\x\\y.3mf"))
    response = _open(client, built["workspace_id"], built["exports"]["export.3mf"]["id"])
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "print_handoff_wslpath_failed"
    assert _events() == []


def test_filename_inputs_never_leave_the_fixed_charset() -> None:
    import random
    import string

    rng = random.Random(165)
    alphabet = string.printable + "\u202e\u0000é\\/:\"&|<>^%"
    allowed = re.compile(r"^[0-9a-f]{8}-attempt[0-9]{1,6}-[0-9a-f]{8}\.3mf$")
    for _ in range(2000):
        candidate = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        attempt = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
        sha = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 70)))
        try:
            name = print_handoff.handoff_filename(candidate, attempt, sha)
        except print_handoff.PrintHandoffError:
            continue
        assert allowed.fullmatch(name)
    assert print_handoff.handoff_filename("0123abcd-ffff", 3, "f" * 64) == "0123abcd-attempt3-ffffffff.3mf"


def test_new_module_has_no_shell_or_cmd() -> None:
    source = Path(print_handoff.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "cmd.exe" not in source
