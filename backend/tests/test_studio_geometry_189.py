"""Spec 189: user-authored build123d geometry in the real sandbox, the artifact channel and lineage."""

import hashlib
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.modules.studio_runtime import runtime

PLACEHOLDER = '    raise NotImplementedError("Complete build before testing")'
BAFFLE = '''    helix = bd.Helix(pitch=pitch, height=pitch * turns, radius=radius)
    section = bd.Plane(origin=helix @ 0, z_dir=helix % 0) * bd.Rectangle(width, thickness)
    return bd.sweep(section, path=helix, is_frenet=True)'''
BAFFLE_SOURCE = "import build123d as bd\n\ndef build(radius, pitch, turns, width, thickness):\n" + BAFFLE + \
    "\n\ndef main(inputs):\n    return build(**inputs)\n"
BAFFLE_INPUTS = {"radius": 18.0, "pitch": 12.0, "turns": 4.0, "width": 4.0, "thickness": 2.0}
GB = 1024 ** 3


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch):
    if os.getenv("JARVISOS_STUDIO_CI_SANDBOX_UNAVAILABLE") == "1":
        pytest.skip("CI runner does not permit bubblewrap user namespaces; WSL2 qualification is required")
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "1")
    get_settings.cache_clear()
    assert runtime.status()["available"], "qualification host must run bubblewrap"
    yield
    get_settings.cache_clear()


def geometry(source: str, tmp_path: Path, inputs: dict | None = None) -> runtime.RunRecord:
    return runtime.run(runtime.RunRequest(source=source, inputs=inputs or {}), profile="geometry",
                       artifact_dir=tmp_path / "admitted")


def box_source(body: str = "") -> str:
    """A unit box whose main first runs ``body`` (indented one level)."""
    return "import build123d as bd\n\ndef main(inputs):\n" + (body or "    pass") + "\n    return bd.Box(1, 1, 1)\n"


# --- Runtime profile and artifact channel ---------------------------------------------------------

def test_geometry_profile_admits_three_hashed_read_only_artifacts(tmp_path: Path) -> None:
    run = geometry(BAFFLE_SOURCE, tmp_path, BAFFLE_INPUTS)
    assert run.classification == "ok", run.stderr
    assert run.profile == "geometry" and run.limit is None
    assert run.limits.model_dump() == {"wall_seconds": 120, "cpu_seconds": 480, "memory_bytes": 4 * GB,
                                       "processes": 32, "file_bytes": 64 * 1024 * 1024}
    assert run.result["valid"] is True and run.result["solids"] == 1
    assert run.result["volume"] == pytest.approx(4 * 2 * 4 * (((2 * 3.141592653589793 * 18) ** 2 + 12 ** 2) ** 0.5), rel=0.01)
    assert [item.name for item in run.artifacts] == list(runtime.ARTIFACT_NAMES)
    for item in run.artifacts:
        path = tmp_path / "admitted" / item.name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item.sha256 and path.stat().st_size == item.size_bytes
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert (tmp_path / "admitted" / "model.glb").read_bytes()[:4] == b"glTF"


def test_calculation_profile_is_unchanged_and_cannot_borrow_geometry_limits() -> None:
    assert runtime.PROFILES["calculation"] == {"wall_seconds": 15, "cpu_seconds": 12, "memory_bytes": GB,
                                               "processes": 32, "file_bytes": 131_072,
                                               "scratch_bytes": 4 * 1024 * 1024, "artifact_bytes": 0}
    for field, value in (("wall_seconds", 120), ("memory_bytes", 4 * GB), ("file_bytes", 64 * 1024 * 1024)):
        with pytest.raises(ValueError):
            runtime.Limits(**{field: value})
    calc = runtime.run(runtime.RunRequest(source="def main(inputs):\n return 1"))
    assert calc.classification == "ok" and calc.profile == "calculation" and calc.artifacts == []


def test_request_cannot_select_a_profile_and_geometry_ignores_request_limits(tmp_path: Path) -> None:
    from app.main import create_app

    client = TestClient(create_app())
    refused = client.post("/studio/runtime/runs", headers={"Origin": "http://localhost:5173"},
                          json={"source": "def main(inputs): return 1", "profile": "geometry"})
    assert refused.status_code == 422
    run = runtime.run(runtime.RunRequest(source=box_source(), limits=runtime.Limits(wall_seconds=1)),
                      profile="geometry", artifact_dir=tmp_path / "out")
    assert run.classification == "ok" and run.limits.wall_seconds == 120
    with pytest.raises(ValueError):
        runtime.run(runtime.RunRequest(source=box_source()), profile="geometry")


def test_sandbox_cannot_add_replace_or_link_host_artifact_entries(tmp_path: Path) -> None:
    body = '''    import os
    attempts = []
    for action in (lambda: open('/work/artifacts/extra.bin', 'wb').write(b'x'),
                   lambda: os.remove('/work/artifacts/model.step'),
                   lambda: os.rename('/work/artifacts/model.stl', '/work/moved.stl'),
                   lambda: os.symlink('/etc/passwd', '/work/artifacts/model.glb'),
                   lambda: os.link('/work/artifacts/model.glb', '/work/hard.glb')):
        try:
            action()
            attempts.append('ok')
        except OSError:
            attempts.append('refused')
    if attempts[1:] != ['refused'] * 4:
        raise RuntimeError(attempts)'''
    run = geometry(box_source(body), tmp_path)
    # The extra file lands in the sandbox tmpfs only; the host sees exactly the three bound files.
    assert run.classification == "ok", run.stderr
    assert sorted(path.name for path in (tmp_path / "admitted").iterdir()) == sorted(runtime.ARTIFACT_NAMES)
    assert all(item.size_bytes > 0 for item in run.artifacts)


def _source_dir(tmp_path: Path, **overrides: bytes | None) -> Path:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    glb_json = b'{"asset":{"version":"2.0"}}   '
    glb = b"glTF" + (2).to_bytes(4, "little") + (20 + len(glb_json)).to_bytes(4, "little")
    glb += len(glb_json).to_bytes(4, "little") + b"JSON" + glb_json
    contents = {"model.step": b"ISO-10303-21;\nEND-ISO-10303-21;\n",
                "model.stl": b"\0" * 80 + (1).to_bytes(4, "little") + b"\0" * 50, "model.glb": glb}
    contents.update({name: value for name, value in overrides.items() if value is not None})
    for name, data in contents.items():
        (source / name).write_bytes(data)
    return source


def test_host_admission_rejects_every_invalid_set_and_registers_none(tmp_path: Path) -> None:
    profile = dict(runtime.PROFILES["geometry"])
    ok, error, hit = runtime._admit(_source_dir(tmp_path / "good"), tmp_path / "good" / "out", profile)
    assert error is None and hit is None and len(ok) == 3

    def admit(prepare, label: str = "", **overrides) -> tuple[str | None, str | None]:
        case = tmp_path / (label or prepare.__name__)
        source = _source_dir(case, **overrides)
        prepare(source)
        artifacts, code, limit = runtime._admit(source, case / "out", profile)
        assert artifacts == [] and not (case / "out").exists()
        return code, limit

    def extra(source: Path) -> None:
        (source / "extra.txt").write_text("x")

    def missing(source: Path) -> None:
        (source / "model.stl").unlink()

    def symlink(source: Path) -> None:
        (source / "model.step").unlink()
        (source / "model.step").symlink_to("/etc/passwd")

    def hardlink(source: Path) -> None:
        os.link(source / "model.glb", source.parent / "second-link")

    def directory(source: Path) -> None:
        (source / "model.stl").unlink()
        (source / "model.stl").mkdir()

    def fifo(source: Path) -> None:
        (source / "model.glb").unlink()
        os.mkfifo(source / "model.glb")

    def empty(source: Path) -> None:
        (source / "model.step").write_bytes(b"")

    def malformed_glb(source: Path) -> None:
        (source / "model.glb").write_bytes(b"not a glb at all, just bytes")

    def malformed_step(source: Path) -> None:
        (source / "model.step").write_bytes(b"#!/bin/sh\nrm -rf /\n")

    assert admit(extra) == ("artifact_set_invalid", None)
    assert admit(missing) == ("artifact_set_invalid", None)
    assert admit(symlink) == ("artifact_not_regular", None)
    assert admit(hardlink) == ("artifact_not_regular", None)
    assert admit(directory) == ("artifact_not_regular", None)
    assert admit(fifo) == ("artifact_not_regular", None)
    assert admit(empty) == ("artifact_missing", None)
    assert admit(malformed_glb) == ("artifact_format_invalid", None)
    assert admit(malformed_step) == ("artifact_format_invalid", None)
    profile["file_bytes"] = 100
    assert admit(lambda source: None, "per-file") == (None, "file_bytes")
    profile.update(file_bytes=1024, artifact_bytes=150)
    assert admit(lambda source: None, "combined") == (None, "artifact_bytes")


def test_malformed_and_failed_exports_register_nothing(tmp_path: Path) -> None:
    junk = "    bd.export_gltf = lambda shape, path, **kw: open(path, 'wb').write(b'junk') > 0"
    run = geometry(box_source(junk), tmp_path / "junk")
    assert run.classification == "invalid_result" and run.artifact_error == "artifact_format_invalid"
    assert run.artifacts == [] and run.result is None and not (tmp_path / "junk" / "admitted").exists()
    failed = geometry(box_source("    bd.export_stl = lambda *a, **kw: False"), tmp_path / "failed")
    assert failed.classification == "error" and "export failed" in failed.stderr
    assert not (tmp_path / "failed" / "admitted").exists()
    not_a_shape = geometry("def main(inputs):\n    return {'volume': 1}\n", tmp_path / "dict")
    assert not_a_shape.classification == "error" and "build123d shape" in not_a_shape.stderr


# --- Named resource failures ----------------------------------------------------------------------

def test_resource_limits_are_named_with_their_configured_value(tmp_path: Path, monkeypatch) -> None:
    memory = geometry(box_source("    blob = bytearray(6 * 1024 ** 3)"), tmp_path / "memory")
    assert (memory.classification, memory.limit, memory.limit_value) == ("limit", "memory_bytes", 4 * GB)

    big = "    open('/work/artifacts/model.stl', 'wb').write(b'x' * (65 * 1024 * 1024))"
    file_cap = geometry(box_source(big), tmp_path / "file")
    assert (file_cap.classification, file_cap.limit, file_cap.limit_value) == ("limit", "file_bytes", 64 * 1024 * 1024)

    scratch = "    [open(f'/work/s{n}', 'wb').write(b'x' * (60 * 1024 * 1024)) for n in range(6)]"
    scratch_cap = geometry(box_source(scratch), tmp_path / "scratch")
    assert (scratch_cap.limit, scratch_cap.limit_value) == ("scratch_bytes", 256 * 1024 * 1024)

    monkeypatch.setitem(runtime.PROFILES["geometry"], "artifact_bytes", 100_000)
    combined = geometry(BAFFLE_SOURCE, tmp_path / "combined", BAFFLE_INPUTS)
    assert (combined.classification, combined.limit, combined.limit_value) == ("limit", "artifact_bytes", 100_000)
    assert not (tmp_path / "combined" / "admitted").exists()

    monkeypatch.setitem(runtime.PROFILES["geometry"], "wall_seconds", 3)
    wall = geometry(box_source("    import time\n    time.sleep(30)"), tmp_path / "wall")
    assert (wall.classification, wall.limit, wall.limit_value) == ("timeout", "wall_seconds", 3)
    for record in (memory, file_cap, scratch_cap, wall):
        assert record.artifacts == [] and record.result is None


# --- 187 host-access cases under the geometry profile ---------------------------------------------

def test_host_access_stays_closed_under_the_geometry_profile(tmp_path: Path) -> None:
    canary = Path.home() / f".studio-189-canary-{os.getpid()}"
    canary.write_text("secret")
    body = f'''    import os, socket
    from pathlib import Path
    leaks = []
    for address in [('1.1.1.1', 443), ('127.0.0.1', 22)]:
        try:
            socket.create_connection(address, timeout=.2)
            leaks.append('network')
        except OSError:
            pass
    if Path({str(canary)!r}).exists():
        leaks.append('home')
    if os.getenv('STUDIO_HOST_SECRET'):
        leaks.append('env')
    for target in [{str(tmp_path / "escape")!r}, '/usr/escape']:
        try:
            Path(target).write_text('x')
            leaks.append('write')
        except OSError:
            pass
    if leaks:
        raise RuntimeError(leaks)'''
    os.environ["STUDIO_HOST_SECRET"] = "never-expose"
    try:
        run = geometry(box_source(body), tmp_path)
    finally:
        del os.environ["STUDIO_HOST_SECRET"]
        canary.unlink()
    assert run.classification == "ok", run.stderr
    assert not (tmp_path / "escape").exists()


def test_disabled_runtime_runs_nothing_and_creates_no_artifacts(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "never-created"
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "0")
    get_settings.cache_clear()
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n" + box_source()
    run = geometry(source, tmp_path)
    assert run.classification == "disabled" and not marker.exists() and not (tmp_path / "admitted").exists()


# --- Studio lifecycle, preview, generation and lineage --------------------------------------------

@pytest.fixture
def client():
    initialize_storage(seed_default=True)
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _variable(name: str, unit: str = "mm", **extra) -> dict:
    return {"name": name, "label": name.title(), "unit": unit, "description": f"Baffle {name}", **extra}


def _geometry_model(client: TestClient) -> tuple[str, str]:
    created = client.post("/workspaces/main/studio/models", json={
        "category": "geometry", "name": "Helical baffle", "engineering_question": "Which baffle fits the tube?",
        "inputs": [_variable("radius", min=1, max=200), _variable("pitch"), _variable("turns", "dimensionless"),
                   _variable("width"), _variable("thickness")]})
    assert created.status_code == 201, created.text
    model = created.json()
    assert model["category"] == "geometry" and "def build(radius: float" in model["source"]
    return model["id"], model["source"]


def test_geometry_documentation_uses_build_entry_without_running_source(client: TestClient) -> None:
    model_id, source = _geometry_model(client)
    stored = client.get(f"/workspaces/main/studio/models/{model_id}")
    assert stored.status_code == 200
    derived = stored.json()["documentation"]["derived"]
    assert derived["status"] == "ok"
    assert derived["functions"][0]["name"] == "build"
    preview = client.post("/studio/documentation/preview", json={"source": source, "entry": "build"})
    assert preview.status_code == 200
    assert preview.json() == derived
    assert client.post("/studio/documentation/preview", json={"source": source, "entry": "main"}).status_code == 422


def _cases(**bounds) -> dict:
    return {"cases": [{"inputs": BAFFLE_INPUTS, **bounds}]}


def test_golden_workflow_two_through_the_api(client: TestClient) -> None:
    categories = {item["id"]: item for item in client.get("/studio/categories").json()}
    assert categories["geometry"]["available"] is True
    model_id, scaffold = _geometry_model(client)
    path = f"/workspaces/main/studio/models/{model_id}"
    failing = client.post(path + "/test", json=_cases())
    assert failing.status_code == 200 and failing.json()["passed"] is False
    assert "NotImplementedError" in failing.json()["cases"][0]["errors"][0]

    assert client.put(path, json={"source": scaffold.replace(PLACEHOLDER, BAFFLE)}).status_code == 200
    too_tight = client.post(path + "/test", json=_cases(max_extent=10)).json()
    assert too_tight["passed"] is False and "Largest extent" in too_tight["cases"][0]["errors"][0]
    out_of_bounds = client.post(path + "/test", json={"cases": [{"inputs": {**BAFFLE_INPUTS, "radius": 500}}]}).json()
    assert out_of_bounds["cases"][0]["classification"] == "invalid_case"
    report = client.post(path + "/test", json=_cases(volume_min=3000, volume_max=4000, max_extent=60)).json()
    assert report["passed"] is True, report
    case = report["cases"][0]
    assert case["facts"]["valid"] and {item["name"] for item in case["artifacts"]} == set(runtime.ARTIFACT_NAMES)

    preview = client.get(f"{path}/previews/{case['preview_id']}/model.glb")
    assert preview.status_code == 200 and preview.content[:4] == b"glTF"
    assert preview.headers["content-type"] == "model/gltf-binary"
    assert preview.headers["x-content-type-options"] == "nosniff"
    step = client.get(f"{path}/previews/{case['preview_id']}/model.step")
    assert step.headers["content-disposition"].startswith("attachment") and step.content.startswith(b"ISO-10303-21;")
    assert client.get(f"{path}/previews/{case['preview_id']}/result.json").status_code == 404
    assert client.get(f"{path}/previews/not-a-preview/model.glb").status_code == 404

    published = client.post(path + "/publish")
    assert published.status_code == 200, published.text
    version_id = published.json()["id"]
    assert published.json()["category"] == "geometry"
    library = client.get("/workspaces/main/studio/library").json()
    assert any(item["id"] == version_id and item["category"] == "geometry" for item in library)

    parameter = client.post("/workspaces/main/parameters", json={"name": "Baffle radius", "value": "2", "unit": "cm"})
    assert parameter.status_code == 201, parameter.text
    bindings = {name: {"value": value, "unit": "dimensionless" if name == "turns" else "mm"}
                for name, value in BAFFLE_INPUTS.items()}
    bindings["radius"] = {"value": 2, "unit": "cm", "source_parameter_id": parameter.json()["id"]}
    created = client.post("/workspaces/main/runner-jobs", json={"model_version_id": version_id, "input_set": bindings})
    assert created.status_code == 201, created.text
    job = created.json()["runner_job"]
    assert job["timeout_seconds"] == 120 and job["max_artifact_bytes"] == 64 * 1024 * 1024
    ran = client.post(f"/runner-jobs/{job['id']}/run")
    assert ran.status_code == 200 and ran.json()["runner_job"]["status"] == "succeeded", ran.text
    output = ran.json()["output"]
    # The bound 2 cm radius arrives in the declared millimetres: the bounding box spans 2 * (20 + 1) mm (thickness is radial).
    assert output["geometry"]["bbox_max"][0] - output["geometry"]["bbox_min"][0] == pytest.approx(42, rel=0.01)
    run_id = ran.json()["simulation_run"]["id"]
    for item in output["artifacts"]:
        served = client.get(f"/workspaces/main/studio/geometry-runs/{run_id}/{item['name']}")
        assert served.status_code == 200 and hashlib.sha256(served.content).hexdigest() == item["sha256"]
        assert served.headers["x-content-type-options"] == "nosniff"
    artifacts = client.get(f"/workspaces/main/simulation-runs/{run_id}/artifacts").json()
    assert {item["role"] for item in artifacts} >= {"studio_geometry", "studio_geometry_facts"}

    run_path = f"/workspaces/main/simulation-runs/{run_id}"
    before = client.get(run_path).json()
    assert before["model_version_id"] == version_id and before["stale"] is False and before["superseded"] is False
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE parameters SET value = '2.5' WHERE id = ?", (parameter.json()["id"],))
        connection.commit()
    assert client.get(run_path).json()["stale"] is True

    with open_sqlite_connection() as connection:
        stored = connection.execute("""SELECT stored_path FROM artifacts WHERE source_ref = ? AND filename = 'model.glb'""",
                                    (f"simulation_run:{run_id}",)).fetchone()["stored_path"]
    Path(stored).chmod(0o644)
    Path(stored).write_bytes(b"glTF tampered")
    assert client.get(f"/workspaces/main/studio/geometry-runs/{run_id}/model.glb").status_code == 404


def test_geometry_generation_fails_closed_without_the_runtime(client: TestClient, monkeypatch) -> None:
    model_id, scaffold = _geometry_model(client)
    path = f"/workspaces/main/studio/models/{model_id}"
    client.put(path, json={"source": scaffold.replace(PLACEHOLDER, BAFFLE)})
    assert client.post(path + "/test", json=_cases()).json()["passed"] is True
    version_id = client.post(path + "/publish").json()["id"]
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "0")
    get_settings.cache_clear()
    bindings = {name: {"value": value, "unit": "dimensionless" if name == "turns" else "mm"}
                for name, value in BAFFLE_INPUTS.items()}
    denied = client.post("/workspaces/main/runner-jobs", json={"model_version_id": version_id, "input_set": bindings})
    assert denied.status_code == 400 and denied.json()["detail"]["code"] == "studio_runtime_unavailable"


def test_category_mismatch_and_limit_reported_in_test_report(client: TestClient, monkeypatch) -> None:
    model_id, scaffold = _geometry_model(client)
    path = f"/workspaces/main/studio/models/{model_id}"
    mismatch = client.post(path + "/test", json={"cases": [{"inputs": BAFFLE_INPUTS, "expected_outputs": {}}]})
    assert mismatch.status_code == 422
    assert client.put(path, json={"outputs": [_variable("volume")]}).status_code == 422
    monkeypatch.setitem(runtime.PROFILES["geometry"], "wall_seconds", 3)
    slow = scaffold.replace(PLACEHOLDER, "    import time\n    time.sleep(30)")
    client.put(path, json={"source": slow})
    case = client.post(path + "/test", json=_cases()).json()["cases"][0]
    assert case["classification"] == "timeout" and case["limit"] == "wall_seconds" and case["limit_value"] == 3
    assert "wall_seconds limit (3)" in case["errors"][0] and case["artifacts"] == []
    assert client.post(path + "/publish").status_code == 409
