"""Spec 336: the coordinator's plan in Up next — pure projection and the read-only route."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.coding import control_room as cr
from app.modules.coding import relay_monitor, runtime_routes
from app.modules.coding import runtime_truth as rt

REPOSITORY = "AlbertoRacerro/JarvisOS"

HEADER = (
    "# Spec status\n\n## Registry\n\n"
    "| Spec | Status | Implementation PR | Name | Depends on | Description |\n"
    "| --- | --- | --- | --- | --- | --- |\n"
)


def registry_text(*rows: str) -> str:
    return HEADER + "\n".join(rows) + "\n"


def row(spec: str, status: str, name: str) -> str:
    return f"| {spec} | {status} | — | {name} | — | d |"


@pytest.fixture
def registry() -> dict[str, cr.RegistryRow]:
    rows, warnings = cr.parse_registry(
        registry_text(
            row("001", "merged", "FIRST-THING-1"),
            row("002", "ready", "SECOND-THING-1"),
            row("003", "planned", "Third thing"),
        )
    )
    assert warnings == []
    return rows


def step(title: str = "Passo generico", state: str = "next", note: str | None = None, specs: list[str] | None = None) -> dict[str, object]:
    result: dict[str, object] = {"title": title, "state": state, "specs": specs if specs is not None else []}
    if note is not None:
        result["note"] = note
    return result


def plan_payload(steps: list[dict[str, object]] | None = None, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "plan.v1",
        "updated_at": "2026-11-20T10:00:00+00:00",
        "goal": "Portare a termine il percorso di prova",
        "steps": steps if steps is not None else [step()],
    }
    payload.update(overrides)
    return payload


def plan_text(steps: list[dict[str, object]] | None = None, **overrides: object) -> str:
    return json.dumps(plan_payload(steps, **overrides), ensure_ascii=False)


# --- pure projection: criterion 1 ----------------------------------------------


def test_valid_plan_keeps_file_order_and_joins_registry(registry: dict[str, cr.RegistryRow]) -> None:
    text = plan_text(
        [
            step("Primo passo", "done", note="Nota del primo passo", specs=["001"]),
            step("Secondo passo", "in_progress", specs=["002", "999"]),
            step("Terzo passo", "next", specs=["003"]),
            step("Quarto passo", "later"),
        ]
    )
    plan, warnings = cr.plan_projection(text, registry)
    assert warnings == []
    assert plan["status"] == "available"
    assert plan["goal"] == "Portare a termine il percorso di prova"
    assert plan["updated_at"] == "2026-11-20T10:00:00+00:00"
    steps = plan["steps"]
    assert [s["title"] for s in steps] == ["Primo passo", "Secondo passo", "Terzo passo", "Quarto passo"]
    assert [s["state"] for s in steps] == ["done", "in_progress", "next", "later"]
    assert steps[0]["note"] == "Nota del primo passo"
    assert steps[0]["specs"] == [{"spec_id": "001", "name": "FIRST-THING-1", "title": "First Thing", "status": "merged"}]
    assert steps[1]["specs"] == [
        {"spec_id": "002", "name": "SECOND-THING-1", "title": "Second Thing", "status": "ready"},
        {"spec_id": "999", "name": None, "title": None, "status": None},
    ]
    assert steps[2]["specs"][0]["status"] == "planned"
    assert steps[3]["note"] is None and steps[3]["specs"] == []


def test_absent_plan_is_not_a_warning(registry: dict[str, cr.RegistryRow]) -> None:
    assert cr.plan_projection(None, registry) == ({"status": "absent"}, [])


def test_empty_registry_leaves_spec_titles_and_statuses_null() -> None:
    plan, warnings = cr.plan_projection(plan_text([step(specs=["001"])]), {})
    assert warnings == []
    assert plan["status"] == "available"
    assert plan["steps"][0]["specs"] == [{"spec_id": "001", "name": None, "title": None, "status": None}]


# --- pure projection: criterion 2 ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "",
        "[]",
        '"plan"',
        "null",
        "3",
    ],
)
def test_malformed_or_non_object_json_is_invalid(text: str, registry: dict[str, cr.RegistryRow]) -> None:
    assert cr.plan_projection(text, registry) == ({"status": "invalid"}, ["plan_invalid"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "plan.v2"},
        {"schema": None},
        {"goal": None},
        {"goal": 5},
        {"goal": ""},
        {"goal": "   "},
        {"updated_at": None},
        {"updated_at": ""},
        {"steps": None},
        {"steps": {"title": "x"}},
    ],
)
def test_wrong_shape_is_invalid(overrides: dict[str, object], registry: dict[str, cr.RegistryRow]) -> None:
    payload = plan_payload()
    for key, value in overrides.items():
        if value is None and key in {"schema", "goal", "updated_at", "steps"}:
            payload.pop(key)
        else:
            payload[key] = value
    assert cr.plan_projection(json.dumps(payload), registry) == ({"status": "invalid"}, ["plan_invalid"])


@pytest.mark.parametrize(
    "bad_step",
    [
        "not a step",
        {"state": "next", "specs": []},
        {"title": "", "state": "next", "specs": []},
        {"title": "   ", "state": "next", "specs": []},
        {"title": 7, "state": "next", "specs": []},
        {"title": "Passo", "specs": []},
        {"title": "Passo", "state": "finished", "specs": []},
        {"title": "Passo", "state": "Done", "specs": []},
        {"title": "Passo", "state": None, "specs": []},
        {"title": "Passo", "state": "next"},
        {"title": "Passo", "state": "next", "specs": "001"},
        {"title": "Passo", "state": "next", "specs": [123]},
        {"title": "Passo", "state": "next", "specs": [""]},
        {"title": "Passo", "state": "next", "specs": [], "note": 7},
    ],
)
def test_bad_step_is_invalid(bad_step: object, registry: dict[str, cr.RegistryRow]) -> None:
    text = plan_text([step(), bad_step])  # type: ignore[list-item]
    assert cr.plan_projection(text, registry) == ({"status": "invalid"}, ["plan_invalid"])


@pytest.mark.parametrize(
    ("payload", "valid"),
    [
        (plan_payload([step()] * 12), True),
        (plan_payload([step()] * 13), False),
        (plan_payload(goal="x" * 160), True),
        (plan_payload(goal="x" * 161), False),
        (plan_payload([step(title="x" * 160)]), True),
        (plan_payload([step(title="x" * 161)]), False),
        (plan_payload([step(note="x" * 400)]), True),
        (plan_payload([step(note="x" * 401)]), False),
        (plan_payload([step(specs=["001"] * 8)]), True),
        (plan_payload([step(specs=["001"] * 9)]), False),
    ],
)
def test_limits_are_boundaries_not_truncation(
    payload: dict[str, object], valid: bool, registry: dict[str, cr.RegistryRow]
) -> None:
    plan, warnings = cr.plan_projection(json.dumps(payload), registry)
    if valid:
        assert plan["status"] == "available" and warnings == []
        assert len(plan["steps"]) == len(payload["steps"])
    else:
        assert (plan, warnings) == ({"status": "invalid"}, ["plan_invalid"])


def test_file_size_limit_counts_bytes(registry: dict[str, cr.RegistryRow]) -> None:
    base = plan_text()
    padded = base + " " * (cr.PLAN_MAX_BYTES - len(base.encode("utf-8")))
    assert len(padded.encode("utf-8")) == cr.PLAN_MAX_BYTES
    assert cr.plan_projection(padded, registry)[0]["status"] == "available"
    assert cr.plan_projection(padded + " ", registry) == ({"status": "invalid"}, ["plan_invalid"])
    # Fewer characters than the limit, but more bytes: still invalid.
    wide = plan_payload(updated_at="à" * 33_000)
    text = json.dumps(wide, ensure_ascii=False)
    assert len(text) < cr.PLAN_MAX_BYTES < len(text.encode("utf-8"))
    assert cr.plan_projection(text, registry) == ({"status": "invalid"}, ["plan_invalid"])


# --- route: criteria 1-2 against a temporary control root -----------------------


class _FakeProjection:
    def __init__(self, registry: dict[str, cr.RegistryRow]) -> None:
        self.registry = registry

    def project(self, target_ref: str) -> dict[str, object]:
        return {
            "observed_at": "2026-11-20T00:00:00+00:00",
            "source": {"git_ref": f"refs/remotes/origin/{target_ref}", "sha": "a" * 40, "status_path": "x"},
            "recent_work": {
                "status": "available",
                "limit": 20,
                "items": [{"sha": "b" * 40, "pr_number": 1, "spec_ids": ["001"], "maintenance": False}],
            },
            "upcoming": {
                "status": "available",
                "authorized": [{"spec_id": "002", "status": "ready", "title": "Second Thing"}],
                "planned_ready": [],
                "planned_waiting": [],
                "blocked": [],
            },
            "warnings": [],
        }


def _startup() -> rt.RuntimeSnapshot:
    return rt.RuntimeSnapshot(
        root_identity="a" * 64,
        observed_at="2026-11-20T00:00:00+00:00",
        git_available=True,
        git_sha="a" * 40,
        head_state="branch",
        branch="master",
        dirty_state="clean",
        provenance="process_start_observation",
        failure_code=None,
    )


def _client(monkeypatch: pytest.MonkeyPatch, control: Path, registry: dict[str, cr.RegistryRow]) -> TestClient:
    monkeypatch.setattr(relay_monitor, "control_root", lambda: control)
    monkeypatch.setattr(runtime_routes, "_control_room_projection", lambda: _FakeProjection(registry))
    monkeypatch.setattr(runtime_routes, "_repository_service", lambda: object())

    class FakeRuntime:
        def __init__(self, _service: object) -> None:
            pass

        def inspect(self, **_: object) -> dict[str, object]:
            return {"alignment": "aligned", "remote": {"resolved_sha": "a" * 40}}

    monkeypatch.setattr(runtime_routes, "RuntimeTruthService", FakeRuntime)
    app = FastAPI()
    app.include_router(runtime_routes.router)
    app.state.runtime_startup_snapshot = _startup()
    return TestClient(app)


def _get(client: TestClient) -> dict[str, object]:
    response = client.get("/api/coding/control-room", params={"repository": REPOSITORY, "target_ref": "master"})
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def control(tmp_path: Path) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    return root


def test_route_serves_valid_plan_joined_to_the_projection_registry(
    monkeypatch: pytest.MonkeyPatch, control: Path, registry: dict[str, cr.RegistryRow]
) -> None:
    (control / "plan.json").write_text(
        plan_text([step("Primo passo", "done", specs=["001"]), step("Secondo passo", "next", specs=["999"])]),
        encoding="utf-8",
    )
    body = _get(_client(monkeypatch, control, registry))
    assert body["plan"]["status"] == "available"
    steps = body["plan"]["steps"]
    assert [s["title"] for s in steps] == ["Primo passo", "Secondo passo"]
    assert steps[0]["specs"][0]["title"] == "First Thing" and steps[0]["specs"][0]["status"] == "merged"
    assert steps[1]["specs"][0]["title"] is None and steps[1]["specs"][0]["status"] is None
    assert body["warnings"] == []
    assert body["recent_work"]["status"] == "available" and body["recent_work"]["items"][0]["pr_number"] == 1
    assert body["upcoming"]["authorized"][0]["spec_id"] == "002"
    assert set(body) == {
        "repository", "target_ref", "runtime", "plan", "observed_at", "source", "recent_work", "upcoming", "warnings",
    }


def test_route_missing_plan_is_absent_without_warning(
    monkeypatch: pytest.MonkeyPatch, control: Path, registry: dict[str, cr.RegistryRow]
) -> None:
    body = _get(_client(monkeypatch, control, registry))
    assert body["plan"] == {"status": "absent"}
    assert body["warnings"] == []
    assert body["recent_work"]["status"] == "available"
    assert body["upcoming"]["status"] == "available"
    assert not (control / "plan.json").exists()


def test_route_missing_control_root_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registry: dict[str, cr.RegistryRow]
) -> None:
    body = _get(_client(monkeypatch, tmp_path / "does-not-exist", registry))
    assert body["plan"] == {"status": "absent"}
    assert body["warnings"] == []


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        plan_text(schema="plan.v2"),
        plan_text([step(state="finished")]),
        plan_text([step()] * 13),
        plan_text(goal="x" * 161),
        plan_text([step(note="x" * 401)]),
        plan_text([step(specs=["001"] * 9)]),
    ],
)
def test_route_invalid_plan_warns_without_breaking_the_rest(
    monkeypatch: pytest.MonkeyPatch, control: Path, registry: dict[str, cr.RegistryRow], content: str
) -> None:
    (control / "plan.json").write_text(content, encoding="utf-8")
    body = _get(_client(monkeypatch, control, registry))
    assert body["plan"] == {"status": "invalid"}
    assert body["warnings"] == ["plan_invalid"]
    assert body["recent_work"]["status"] == "available" and body["recent_work"]["items"][0]["pr_number"] == 1
    assert body["upcoming"]["authorized"][0]["spec_id"] == "002"


def test_route_oversized_plan_is_invalid_not_truncated(
    monkeypatch: pytest.MonkeyPatch, control: Path, registry: dict[str, cr.RegistryRow]
) -> None:
    base = plan_text()
    exact = base + " " * (cr.PLAN_MAX_BYTES - len(base.encode("utf-8")))
    (control / "plan.json").write_text(exact, encoding="utf-8")
    assert _get(_client(monkeypatch, control, registry))["plan"]["status"] == "available"
    (control / "plan.json").write_text(exact + " ", encoding="utf-8")
    body = _get(_client(monkeypatch, control, registry))
    assert body["plan"] == {"status": "invalid"}
    assert body["warnings"] == ["plan_invalid"]


def test_route_refuses_symlink_leaving_the_control_root(
    monkeypatch: pytest.MonkeyPatch, control: Path, tmp_path: Path, registry: dict[str, cr.RegistryRow]
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(plan_text(), encoding="utf-8")
    (control / "plan.json").symlink_to(outside)
    body = _get(_client(monkeypatch, control, registry))
    assert body["plan"] == {"status": "invalid"}
    assert body["warnings"] == ["plan_invalid"]
    assert outside.read_text(encoding="utf-8") == plan_text()


def test_route_accepts_symlink_inside_the_control_root(
    monkeypatch: pytest.MonkeyPatch, control: Path, registry: dict[str, cr.RegistryRow]
) -> None:
    (control / "real-plan.json").write_text(plan_text(), encoding="utf-8")
    (control / "plan.json").symlink_to(control / "real-plan.json")
    body = _get(_client(monkeypatch, control, registry))
    assert body["plan"]["status"] == "available"
    assert body["warnings"] == []


def test_route_unreadable_plan_is_invalid(
    monkeypatch: pytest.MonkeyPatch, control: Path, registry: dict[str, cr.RegistryRow]
) -> None:
    (control / "plan.json").mkdir()
    body = _get(_client(monkeypatch, control, registry))
    assert body["plan"] == {"status": "invalid"}
    assert body["warnings"] == ["plan_invalid"]
    (control / "plan.json").rmdir()
    (control / "plan.json").write_bytes(b"\xff\xfe\x00plan")
    body = _get(_client(monkeypatch, control, registry))
    assert body["plan"] == {"status": "invalid"}
    assert body["warnings"] == ["plan_invalid"]


def test_route_never_writes_the_plan(
    monkeypatch: pytest.MonkeyPatch, control: Path, registry: dict[str, cr.RegistryRow]
) -> None:
    content = plan_text()
    (control / "plan.json").write_text(content, encoding="utf-8")
    before = sorted(path.name for path in control.iterdir())
    _get(_client(monkeypatch, control, registry))
    assert sorted(path.name for path in control.iterdir()) == before
    assert (control / "plan.json").read_text(encoding="utf-8") == content
    (control / "plan.json").write_text("{broken", encoding="utf-8")
    _get(_client(monkeypatch, control, registry))
    assert (control / "plan.json").read_text(encoding="utf-8") == "{broken"
    assert sorted(path.name for path in control.iterdir()) == before
