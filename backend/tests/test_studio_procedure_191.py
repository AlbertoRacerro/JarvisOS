"""Spec 191 acceptance: Studio procedure authoring compiled to a Schedule and run through the real dynamic engine.

No test sets JARVISOS_STUDIO_USER_CODE_ENABLED: a procedure is data and never touches the 187 runtime.
Criterion 2 compares with closed forms written here (hand arithmetic), not with the engine.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.modules.process_stack import draft, dynamic_engine
from app.modules.process_stack.draft_models import SetSchedule
from app.modules.runner.safety import model_implementation_root
from app.modules.studio import procedure
from app.modules.studio_runtime.runtime import status
from tests.test_culture_campaigns_185 import O2_SAT, _ring

NOTICE = "Simulation only. Actions change modelled tank inventories. This does not command any equipment. Unqualified."
TANK = 0.05
DRAW = {"name": "draw_fraction", "label": "Draw fraction", "unit": "percent",
        "description": "Share of the loop volume drawn", "min": 0, "max": 100}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JARVISOS_STUDIO_USER_CODE_ENABLED", raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _dark_ring(events: list[dict[str, Any]] | None = None, *, days: float = 0.25, k_d: float = 0.0,
               x0: float = 0.4, n0: float = 0.05) -> tuple[str, str]:
    initial = {tag: {"X": x0, "N": n0, "O2": O2_SAT} for tag in ("PBR", "TankA")}
    snapshot = _ring(tube_length=1.0, tank_volume=TANK, days=days, cadence_s=1800, events=events or [], dark=True,
                     k_d=k_d, initial=initial)
    return snapshot.payload["workspace_id"], snapshot.payload["draft_id"]


def _golden_steps(fraction: Any = None) -> list[dict[str, Any]]:
    return [
        {"label": "Draw and separate", "time_s": 3600, "actions": [
            {"type": "draw", "tank": "TankA", "loop_fraction": fraction or {"$param": "draw_fraction"}},
            {"type": "separate", "recovery": 0.95, "concentration_factor": 20.0, "return_to": "TankA"}]},
        {"label": "Refill", "time_s": 3600, "actions": [
            {"type": "refill", "tank": "TankA", "to_volume_m3": TANK, "medium": {"N": 0.5, "O2": O2_SAT}}]},
        {"label": "Dose", "time_s": 3600, "actions": [{"type": "dose", "tank": "TankA", "nitrogen_kg": 0.001}]},
    ]


def _create(client: TestClient, ws: str, steps: list[dict[str, Any]], parameters: list[dict[str, Any]] | None = None,
            name: str = "Cycle") -> dict[str, Any]:
    response = client.post(f"/workspaces/{ws}/studio/models", json={
        "category": "procedure", "name": name, "engineering_question": "What does the cycle harvest?",
        "parameters": [DRAW] if parameters is None else parameters, "steps": steps})
    assert response.status_code == 201, response.text
    return response.json()


def _case(draft_id: str, values: dict[str, float] | None = None, *, schedule_id: str = "campaign",
          scenario: str = "run", **extra: Any) -> dict[str, Any]:
    return {"name": "case", "parameter_values": {"draw_fraction": 20} if values is None else values,
            "draft_id": draft_id, "scenario_id": scenario, "schedule_id": schedule_id, **extra}


def _test(client: TestClient, ws: str, model_id: str, *cases: dict[str, Any]) -> dict[str, Any]:
    response = client.post(f"/workspaces/{ws}/studio/models/{model_id}/test", json={"cases": list(cases)})
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client: TestClient, ws: str, model_id: str, draft_id: str) -> dict[str, Any]:
    assert _test(client, ws, model_id, _case(draft_id))["passed"]
    response = client.post(f"/workspaces/{ws}/studio/models/{model_id}/publish")
    assert response.status_code == 200, response.text
    return response.json()


def _apply(client: TestClient, ws: str, version_id: str, draft_id: str, values: dict[str, float] | None = None,
           **extra: Any):
    return client.post(f"/workspaces/{ws}/studio/procedure/apply", json={
        "version_id": version_id, "draft_id": draft_id, "scenario_id": "run", "schedule_id": "campaign",
        "parameter_values": {"draw_fraction": 20} if values is None else values, **extra})


def _applied(client: TestClient, ws: str, version_id: str, draft_id: str, values: dict[str, float] | None = None,
             **extra: Any) -> dict[str, Any]:
    response = _apply(client, ws, version_id, draft_id, values, overwrite=True, **extra)
    assert response.status_code == 200, response.text
    return response.json()


def _code(response: Any) -> str:
    return response.json()["detail"]["code"]


def _wait(client: TestClient, ws: str, draft_id: str, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        payload = client.get(f"/workspaces/{ws}/studio/procedure/drafts/{draft_id}/runs/{job_id}").json()
        if payload["job"]["status"] in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.2)
    raise AssertionError("dynamic run did not finish")


def _run(client: TestClient, ws: str, link_id: str) -> dict[str, Any]:
    response = client.post(f"/workspaces/{ws}/studio/procedure/links/{link_id}/run")
    assert response.status_code == 202, response.text
    return response.json()


# ---------------------------------------------------------------- 2. independent closed forms


def test_golden_185_case_compiles_to_the_fixture_and_matches_hand_arithmetic_after_every_event(client: TestClient) -> None:
    ws, d = _dark_ring()
    model = _create(client, ws, _golden_steps())
    x0, n0, o0, t_event = 0.4, 0.05, O2_SAT, 3600.0
    # Hand arithmetic (185 dark, zero-loss case): the loop is the PBR tubes plus the tank.
    v_pbr = 10 * math.pi * 0.05 ** 2 / 4 * 1.0
    v_loop = v_pbr + TANK
    v_draw = 0.2 * v_loop
    v_conc = v_draw * 0.95 / 20.0
    v1 = TANK - v_draw + (v_draw - v_conc)
    x1 = (x0 * (TANK - v_draw) + 0.05 * v_draw * x0) / v1
    expected = [{"V": v1, "X": x1, "N": n0, "O2": o0},
                {"V": TANK, "X": x1 * v1 / TANK, "N": (n0 * v1 + 0.5 * v_conc) / TANK, "O2": o0}]
    expected.append({**expected[1], "N": expected[1]["N"] + 0.001 / TANK})
    snapshot = dynamic_engine.prepare(ws, d, "run")
    assert {u["tag"]: u["volume_m3"] for u in snapshot.payload["units"]}["PBR"] == pytest.approx(v_pbr, rel=1e-12)
    channel = {"V": "TankA_V_m3", "X": "TankA_X", "N": "TankA_N", "O2": "TankA_O2"}
    expectations = [{"kind": "series", "channel": channel[name], "time_s": t_event, "phase": "post", "event_order": order,
                     "value": want[name], "tolerance": 1e-12, "relative": True}
                    for order, want in enumerate(expected) for name in channel]
    expectations += [{"kind": "series", "channel": "PBR_X", "time_s": t_event, "phase": "post", "event_order": 2,
                      "value": x0, "tolerance": 1e-12, "relative": True}, {"kind": "balances_closed"}]
    report = _test(client, ws, model["id"], _case(d, simulate=True, expect=expectations))
    (case,) = report["cases"]
    assert case["passed"], case["errors"]
    fixture = {"id": "campaign", "events": [
        {"type": "actions", "time_s": t_event, "actions": [
            {"type": "draw", "tank": "TankA", "loop_fraction": 0.2},
            {"type": "separate", "recovery": 0.95, "concentration_factor": 20.0, "return_to": "TankA"}]},
        {"type": "actions", "time_s": t_event, "actions": [
            {"type": "refill", "tank": "TankA", "to_volume_m3": TANK, "medium": {"N": 0.5, "O2": O2_SAT}}]},
        {"type": "actions", "time_s": t_event, "actions": [{"type": "dose", "tank": "TankA", "nitrogen_kg": 0.001}]}]}
    assert case["compiled"] == fixture
    assert case["compiled_sha256"] == procedure.compiled_sha256(fixture)
    assert report["notice"] == NOTICE and all(item["passed"] for item in case["expectations"])
    assert len(case["expectations"]) == 14


def test_conditioned_step_fires_at_the_analytic_crossing_time(client: TestClient) -> None:
    x0, x_low, k_d = 0.4, 0.3, 0.01
    ws, d = _dark_ring(days=3, k_d=k_d, x0=x0)
    steps = [{"label": "Dose on low biomass", "time_s": 0, "condition": {
        "observed": "PBR.loop_X", "direction": "below", "threshold": {"$param": "x_low"}, "hysteresis": 0,
        "cooldown_s": 0, "max_fires": 1}, "actions": [{"type": "dose", "tank": "TankA", "nitrogen_kg": 1e-9}]}]
    model = _create(client, ws, steps, [{"name": "x_low", "label": "Low biomass", "unit": "g/L",
                                         "description": "Trigger level", "min": 0.01, "max": 1}])
    analytic = math.log(x0 / x_low) / (k_d / 3600.0)  # dark first-order loss: X = x0 exp(-k t)
    case = _case(d, {"x_low": 0.3}, simulate=True, expect=[
        {"kind": "fire_time", "fire_index": 1, "value": analytic, "tolerance": 1e-6, "relative": True}])
    (result,) = _test(client, ws, model["id"], case)["cases"]
    assert result["passed"], (result["errors"], result["message"])
    assert result["compiled"]["events"][0]["condition"]["threshold"] == pytest.approx(0.3, rel=1e-12)
    assert result["prepare"]["state_triggered_events"] == 1 and result["simulation"]["state_fires"]


# ---------------------------------------------------------------- 3. determinism


def test_compile_is_byte_identical_for_equal_inputs(client: TestClient) -> None:
    ws, d = _dark_ring()
    document, _ = _document(ws, d)
    contract = [DRAW]
    steps = _golden_steps()
    reordered = json.loads(json.dumps(steps))
    reordered = [dict(reversed(list(step.items()))) for step in reordered]
    texts = []
    for candidate in (steps, steps, reordered):
        schedule = procedure.compile_procedure({"parameters": contract, "steps": candidate}, {"draw_fraction": 20},
                                               document, "campaign")
        texts.append(procedure.canonical_json(procedure.schedule_value(schedule)))
    assert texts[0] == texts[1] == texts[2]
    model = _create(client, ws, steps)
    first = _test(client, ws, model["id"], _case(d))["cases"][0]
    second = _test(client, ws, model["id"], _case(d))["cases"][0]
    assert first["compiled_sha256"] == second["compiled_sha256"] == procedure.sha256_text(texts[0])
    assert json.dumps(first["compiled"], sort_keys=True) == json.dumps(second["compiled"], sort_keys=True)


def _document(ws: str, draft_id: str) -> tuple[dict[str, Any], str]:
    directory = draft.draft_dir(ws, draft_id)
    head = draft._head(directory)
    return draft.load_revision(directory, head["revision"])["document"], head["revision"]


# ---------------------------------------------------------------- 4. guards


def test_time_triggered_overdraw_is_refused_at_test_and_apply_with_the_host_code(client: TestClient) -> None:
    ws, d = _dark_ring()
    steps = [{"label": "Too big", "time_s": 3600, "actions": [{"type": "draw", "tank": "TankA", "volume_m3": 1.0}]}]
    model = _create(client, ws, steps, [])
    (case,) = _test(client, ws, model["id"], _case(d, {}))["cases"]
    assert not case["passed"] and case["code"] == "EVENT_VOLUME_INVALID"
    assert case["compiled_sha256"] is not None
    assert client.post(f"/workspaces/{ws}/studio/models/{model['id']}/publish").status_code == 409
    # Apply refuses the same way (a version published by other means); the draft is untouched.
    revision = _document(ws, d)[1]
    version = _force_publish(ws, model["id"], steps)
    refused = _apply(client, ws, version, d, {}, overwrite=True)
    assert refused.status_code == 422 and _code(refused) == "EVENT_VOLUME_INVALID"
    assert _document(ws, d)[1] == revision


def _force_publish(ws: str, model_id: str, steps: list[dict[str, Any]]) -> str:
    """Publish a model whose Test refused (the gate is Test; here we need a published version that Apply must refuse)."""
    from app.core.database import open_sqlite_connection
    from app.modules.studio import service

    with open_sqlite_connection() as connection:
        report = {"source_sha256": service._draft_row(connection, ws, model_id)["source_sha256"],
                  "contract_sha256": service._draft_row(connection, ws, model_id)["contract_sha256"],
                  "passed": True, "cases": [{"passed": True}]}
        connection.execute("UPDATE studio_drafts SET test_report_json = ? WHERE model_spec_id = ?",
                           (json.dumps(report), model_id))
        connection.commit()
    return service.publish(ws, model_id)["id"]


def test_runtime_overdraw_fails_with_the_runtime_code_in_test_and_in_a_run(client: TestClient) -> None:
    ws, d = _dark_ring(days=3, k_d=0.01)
    steps = [{"label": "Overdraw on trigger", "time_s": 0, "condition": {
        "observed": "PBR.loop_X", "direction": "below", "threshold": 0.3, "hysteresis": 0, "cooldown_s": 0,
        "max_fires": 1}, "actions": [{"type": "draw", "tank": "TankA", "volume_m3": 0.06}]}]
    model = _create(client, ws, steps, [])
    (case,) = _test(client, ws, model["id"], _case(d, {}, simulate=True))["cases"]
    assert case["prepare"]["ok"] and case["simulation"]["status"] == "failed"
    assert case["code"] == "EVENT_VOLUME_INVALID_RUNTIME" and not case["passed"]
    version = _force_publish(ws, model["id"], steps)
    link = _applied(client, ws, version, d, {})
    job = _run(client, ws, link["id"])
    finished = _wait(client, ws, d, job["job_id"])
    assert finished["job"]["status"] == "failed" and finished["job"]["error"]["code"] == "EVENT_VOLUME_INVALID_RUNTIME"
    results = client.get(f"/workspaces/{ws}/studio/procedure/drafts/{d}/runs/{job['job_id']}/results").json()
    assert results["procedure_link"]["version_id"] == version and results["status"] == "failed"


def _draw_step(**changes: Any) -> dict[str, Any]:
    action = {"type": "draw", "tank": "TankA", "loop_fraction": {"$param": "draw_fraction"}, **changes}
    return {"label": "Step", "time_s": 3600, "actions": [action]}


_REFUSALS = {
    "missing_tank": (_draw_step(tank="Missing"), DRAW, {"draw_fraction": 20}, "EVENT_TARGET_INVALID"),
    "unobservable": ({**_draw_step(), "condition": {"observed": "Nowhere.loop_X", "direction": "below", "threshold": 0.3}},
                     DRAW, {"draw_fraction": 20}, "UNSUPPORTED_MEASUREMENT"),
    "out_of_bounds": (_draw_step(), DRAW, {"draw_fraction": 150}, "studio_procedure_param_out_of_bounds"),
    "unbound": (_draw_step(), DRAW, {}, "studio_procedure_param_unbound"),
    "unknown": (_draw_step(), DRAW, {"draw_fraction": 20, "other": 1}, "studio_procedure_param_unknown"),
    "unit_mismatch": (_draw_step(), {**DRAW, "unit": "m"}, {"draw_fraction": 1}, "studio_procedure_unit_mismatch"),
    "invalid_schedule": (_draw_step(loop_fraction=2.0), DRAW, {"draw_fraction": 20}, "studio_procedure_schedule_invalid"),
    "not_integer": ({**_draw_step(loop_fraction=0.1), "every_s": 3600, "count": {"$param": "draw_fraction"}}, DRAW,
                    {"draw_fraction": 2.5}, "studio_procedure_not_integer"),
}


@pytest.mark.parametrize("name", sorted(_REFUSALS))
def test_typed_refusals_at_test_and_apply(client: TestClient, name: str) -> None:
    step, parameter, values, code = _REFUSALS[name]
    ws, d = _dark_ring()
    model = _create(client, ws, [step], [parameter])
    (case,) = _test(client, ws, model["id"], _case(d, values))["cases"]
    assert not case["passed"] and case["code"] == code, case
    version = _force_publish(ws, model["id"], [step])
    before = _document(ws, d)[1]
    refused = _apply(client, ws, version, d, values, overwrite=True)
    assert refused.status_code == 422 and _code(refused) == code
    assert _document(ws, d)[1] == before


def test_structure_is_validated_on_save(client: TestClient) -> None:
    ws, _ = _dark_ring()
    step = {"label": "Bad", "time_s": 1, "actions": [{"type": "draw", "tank": "TankA", "loop_fraction": {"$param": "nope"}}]}
    response = client.post(f"/workspaces/{ws}/studio/models", json={
        "category": "procedure", "name": "x", "engineering_question": "q", "parameters": [], "steps": [step]})
    assert response.status_code == 422 and _code(response) == "studio_procedure_param_undeclared"
    for bad in ({"label": "x", "time_s": 1, "actions": [{"type": "open_valve", "tank": "TankA"}]},
                {"label": "x", "time_s": 1, "actions": [{"type": "dose", "tank": {"$param": "t"}, "nitrogen_kg": 1}]},
                {"label": "x", "time_s": 1, "every_s": 1, "condition": {"observed": "PBR.X"},
                 "actions": [{"type": "dose", "tank": "TankA", "nitrogen_kg": 1}]},
                {"label": "x", "time_s": 1, "extra": 1, "actions": [{"type": "dose", "tank": "TankA", "nitrogen_kg": 1}]}):
        response = client.post(f"/workspaces/{ws}/studio/models", json={
            "category": "procedure", "name": "x", "engineering_question": "q",
            "parameters": [{**DRAW, "name": "t"}], "steps": [bad]})
        assert response.status_code == 422, response.text
    categories = {item["id"]: item for item in client.get("/studio/categories").json()}
    assert categories["procedure"]["available"] and categories["procedure"]["notice"] == NOTICE


# ---------------------------------------------------------------- 5. pinning


def test_applied_schedule_is_pinned_hand_edits_are_flagged_and_updates_are_explicit(client: TestClient) -> None:
    ws, d = _dark_ring(days=2)
    model = _create(client, ws, _golden_steps())
    v1 = _publish(client, ws, model["id"], d)
    refused = _apply(client, ws, v1["id"], d)
    assert refused.status_code == 409 and _code(refused) == "studio_procedure_schedule_exists"
    link = _applied(client, ws, v1["id"], d)
    assert link["state"] == "applied" and link["version_label"] == "Cycle v1" and link["parameter_values"] == {"draw_fraction": 20.0}
    assert link["source_sha256"] == v1["source_sha256"] and link["contract_sha256"] == v1["contract_sha256"]
    assert link["notice"] == NOTICE and not link["update_available"]
    schedule = _document(ws, d)[0]["schedules"]["campaign"]
    assert procedure.compiled_sha256(schedule) == link["compiled_sha256"]
    first_run = _run(client, ws, link["id"])
    assert first_run["procedure_link"]["version_id"] == v1["id"]
    _wait(client, ws, d, first_run["job_id"])

    # A hand edit of the applied schedule is flagged and blocks Run and a plain re-apply.
    revision = _document(ws, d)[1]
    draft.patch(ws, d, revision, [SetSchedule(op="set_schedule", id="campaign", value={"events": []})])
    flagged = client.get(f"/workspaces/{ws}/studio/procedure/links/{link['id']}").json()
    assert flagged["state"] == "modified" and flagged["state_text"] == "Modified, no longer the pinned procedure."
    blocked = client.post(f"/workspaces/{ws}/studio/procedure/links/{link['id']}/run")
    assert blocked.status_code == 409 and _code(blocked) == "studio_procedure_link_modified"
    assert _apply(client, ws, v1["id"], d).status_code == 409
    assert _applied(client, ws, v1["id"], d)["state"] == "applied"

    # Publishing v2 changes neither the applied schedule nor past runs.
    assert client.post(f"/workspaces/{ws}/studio/models/{model['id']}/revise").status_code == 200
    changed = _golden_steps()
    changed[0]["actions"][1]["recovery"] = 0.9
    assert client.put(f"/workspaces/{ws}/studio/models/{model['id']}", json={"steps": changed}).status_code == 200
    v2 = _publish(client, ws, model["id"], d)
    assert v2["version_label"] == "Cycle v2"
    after = client.get(f"/workspaces/{ws}/studio/procedure/links/{link['id']}").json()
    assert after["state"] == "applied" and after["version_id"] == v1["id"] and after["update_available"]
    assert after["latest_version_id"] == v2["id"] and after["compiled_sha256"] == link["compiled_sha256"]
    assert _document(ws, d)[0]["schedules"]["campaign"] == schedule
    old = client.get(f"/workspaces/{ws}/studio/procedure/drafts/{d}/runs/{first_run['job_id']}").json()
    assert old["procedure_link"]["version_id"] == v1["id"]

    updated = client.post(f"/workspaces/{ws}/studio/procedure/links/{link['id']}/update", json={})
    assert updated.status_code == 200, updated.text
    assert updated.json()["version_id"] == v2["id"] and updated.json()["compiled_sha256"] != link["compiled_sha256"]
    assert not updated.json()["update_available"] and updated.json()["id"] == link["id"]
    old = client.get(f"/workspaces/{ws}/studio/procedure/drafts/{d}/runs/{first_run['job_id']}").json()
    assert old["procedure_link"]["version_id"] == v1["id"]


def test_a_tampered_artifact_is_refused(client: TestClient) -> None:
    ws, d = _dark_ring()
    model = _create(client, ws, _golden_steps())
    version = _publish(client, ws, model["id"], d)
    path = model_implementation_root(ws) / version["id"] / "studio_procedure.json"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("0.95", "0.5"), encoding="utf-8")
    before = _document(ws, d)[1]
    refused = _apply(client, ws, version["id"], d, overwrite=True)
    assert refused.status_code == 409 and _code(refused) == "studio_artifact_hash_mismatch"
    assert _document(ws, d)[1] == before
    revise = client.post(f"/workspaces/{ws}/studio/models/{model['id']}/revise")
    assert revise.status_code == 409
    path.write_text(original, encoding="utf-8")
    assert _apply(client, ws, version["id"], d, overwrite=True).status_code == 200


# ---------------------------------------------------------------- 6. boundary, 7. compatibility


def test_procedures_work_with_the_runtime_disabled_and_touch_no_runtime_code(client: TestClient) -> None:
    assert status()["available"] is False
    ws, d = _dark_ring()
    model = _create(client, ws, _golden_steps())
    version = _publish(client, ws, model["id"], d)
    link = _applied(client, ws, version["id"], d)
    assert _wait(client, ws, d, _run(client, ws, link["id"])["job_id"])["job"]["status"] == "succeeded"
    root = Path(procedure.__file__).parent
    for name in ("procedure.py", "procedure_service.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "studio_runtime" not in source and "RunRequest" not in source
    assert {"draw", "separate", "refill", "dose", "inoculate"} == set(procedure._ACTIONS)
    library = client.get(f"/workspaces/{ws}/studio/library").json()
    assert library[0]["category"] == "procedure" and library[0]["implementation_kind"] == "studio_procedure_v1"


def test_authoring_and_testing_never_change_the_target_draft(client: TestClient) -> None:
    ws, d = _dark_ring()
    directory = draft.draft_dir(ws, d)
    head_before = (directory / "head.json").read_bytes()
    drafts_before = draft.list_drafts(ws)
    model = _create(client, ws, _golden_steps())
    _test(client, ws, model["id"], _case(d), _case(d, simulate=True))
    _test(client, ws, model["id"], _case(d, {"draw_fraction": 500}))
    bench = client.get(f"/workspaces/{ws}/studio/procedure/bench/drafts").json()
    assert (directory / "head.json").read_bytes() == head_before
    assert draft.list_drafts(ws) == drafts_before and len(list(draft.drafts_root(ws).iterdir())) == 1
    assert [row["draft_id"] for row in bench] == [d] and bench[0]["tanks"] == ["TankA"]
    assert bench[0]["scenarios"][0]["id"] == "run" and bench[0]["notice"] == NOTICE


# ---------------------------------------------------------------- 7. the minimal bench end to end


def test_bench_creates_a_scenario_applies_runs_and_shows_different_results_for_different_values(client: TestClient) -> None:
    ws, d = _dark_ring(days=0.25)
    from app.modules.environment import profiles as environment

    profile = environment.list_profiles(ws)[0]
    created = client.post(f"/workspaces/{ws}/studio/procedure/bench/drafts/{d}/scenarios", json={
        "scenario_id": "bench", "profile_id": profile["profile_id"], "days": 0.25, "output_cadence_s": 1800,
        "circulation": {"Pump": 5e-4}, "par_scale": 0.0,
        "initial": {tag: {"X": 0.4, "N": 0.05, "O2": O2_SAT} for tag in ("PBR", "TankA")}})
    assert created.status_code == 201, created.text
    assert {item["id"] for item in created.json()["scenarios"]} == {"run", "bench"}
    again = client.post(f"/workspaces/{ws}/studio/procedure/bench/drafts/{d}/scenarios", json={
        "scenario_id": "bench", "profile_id": profile["profile_id"]})
    assert again.status_code == 409 and _code(again) == "studio_procedure_scenario_exists"

    model = _create(client, ws, _golden_steps()[:2])
    version = _publish(client, ws, model["id"], d)
    v_pbr = 10 * math.pi * 0.05 ** 2 / 4
    harvested = {}
    for fraction in (20, 10):
        response = client.post(f"/workspaces/{ws}/studio/procedure/apply", json={
            "version_id": version["id"], "draft_id": d, "scenario_id": "bench", "schedule_id": "procedure",
            "parameter_values": {"draw_fraction": fraction}})
        assert response.status_code == 200, response.text
        link = response.json()
        assert link["state"] == "applied" and link["scenario_id"] == "bench"
        started = _run(client, ws, link["id"])
        finished = _wait(client, ws, d, started["job_id"])
        assert finished["job"]["status"] == "succeeded" and finished["procedure_link"]["parameter_values"] == {
            "draw_fraction": float(fraction)}
        results = client.get(f"/workspaces/{ws}/studio/procedure/drafts/{d}/runs/{started['job_id']}/results").json()
        assert results["balances"]["asserted"] is True and results["notice"] == NOTICE
        events = [entry for entry in results["event_log"] if entry["type"] == "actions"]
        assert len(events) == 2 and all(entry["balance"]["closed"] for entry in events)
        harvested[fraction] = results["campaign"]["ledger"]["harvested"]["biomass_kg"]
        series = client.get(f"/workspaces/{ws}/process/drafts/{d}/dynamic/runs/{started['job_id']}/series",
                            params={"channels": "TankA_V_m3"})
        assert series.status_code == 200 and series.json()["returned_points"] > 0
    # Dark and lossless: the harvest is recovery * fraction * loop volume * X0, exactly.
    for fraction in (20, 10):
        assert harvested[fraction] == pytest.approx(0.95 * fraction / 100 * (v_pbr + TANK) * 0.4, rel=1e-9)
    assert harvested[20] == pytest.approx(2 * harvested[10], rel=1e-9)
    runs = client.get(f"/workspaces/{ws}/studio/procedure/drafts/{d}/runs").json()
    assert [run["procedure_link"]["parameter_values"]["draw_fraction"] for run in runs] == [10.0, 20.0]
    links = client.get(f"/workspaces/{ws}/studio/procedure/links", params={"draft_id": d}).json()
    assert len(links) == 1 and links[0]["parameter_values"] == {"draw_fraction": 10.0}
    bench = client.get(f"/workspaces/{ws}/studio/procedure/bench/drafts").json()
    assert bench[0]["schedules"][-1]["link"]["state"] == "applied"


# ---------------------------------------------------------------- 6/7 gap closers (review)


def test_no_action_type_or_dynamic_route_exists_outside_185_and_186(client: TestClient) -> None:
    from typing import get_args

    from app.modules.process_stack import dynamic_models

    union = get_args(get_args(dynamic_models.Action)[0])
    engine_types = {get_args(model.model_fields["type"].annotation)[0] for model in union}
    assert engine_types == set(procedure._ACTIONS) == {"draw", "separate", "refill", "dose", "inoculate"}
    paths = {route.path for route in client.app.routes if "/studio/procedure" in getattr(route, "path", "")}
    assert all(path.startswith("/workspaces/{workspace_id}/studio/procedure/") for path in paths) and len(paths) == 10
    # Execution goes through the existing dynamic job owner: no procedure-specific dynamic or campaign route exists.
    assert not [path for path in paths if "/dynamic" in path or "/campaign" in path or "/process/" in path]


def test_draft_without_an_applied_procedure_keeps_document_and_snapshot_digest(client: TestClient) -> None:
    ws, d = _dark_ring()
    document_before, revision_before = _document(ws, d)
    text_before = json.dumps(document_before, sort_keys=True)
    digest_before = dynamic_engine.prepare(ws, d, "run").digest
    model = _create(client, ws, _golden_steps())
    _test(client, ws, model["id"], _case(d, simulate=True))
    _publish(client, ws, model["id"], d)
    client.get(f"/workspaces/{ws}/studio/procedure/bench/drafts")
    document_after, revision_after = _document(ws, d)
    assert revision_after == revision_before and json.dumps(document_after, sort_keys=True) == text_before
    assert dynamic_engine.prepare(ws, d, "run").digest == digest_before
