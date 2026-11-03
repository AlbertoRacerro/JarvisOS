"""Spec 190 real-DWSIM acceptance (criteria 1 to 3 backend parts): real 187 sandbox, real DWSIM 10.2.9.

Skipped unless JARVISOS_DWSIM_MCP_PATH is set (qualification host).
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.modules.process_stack import draft, editor, mixed_runtime
from app.modules.process_stack.draft_models import SetUnitParams
from app.modules.studio_runtime.runtime import status
from tests.plumbing_170_support import new_workspace
from tests.studio_unit_190_recycle_support import (
    FEED_KG_S,
    LOOP_BODY,
    LOOP_CASE,
    LOOP_CONTRACT,
    SIDE_KG_S,
    UNIT_TAGS,
    closed_form,
    loop_ops,
)
from tests.studio_unit_190_support import GOLDEN_BODY, GOLDEN_CONTRACT, golden_case, publish_unit
from tests.test_studio_unit_190_process import Q, golden_ops

pytestmark = pytest.mark.skipif(not os.environ.get("JARVISOS_DWSIM_MCP_PATH"),
                                reason="set JARVISOS_DWSIM_MCP_PATH to opt in to the real DWSIM runtime")


@pytest.fixture
def studio(monkeypatch):
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "1")
    get_settings.cache_clear()
    assert status()["available"], "qualification host must run bubblewrap"
    workspace = new_workspace()
    with TestClient(create_app()) as client:
        client.workspace = workspace  # type: ignore[attr-defined]
        yield client
    get_settings.cache_clear()


def _draft(workspace: str, ops: list[Any]) -> dict[str, Any]:
    state = draft.create_draft(workspace, "studio unit dwsim")
    return draft.patch(workspace, state["draft_id"], state["revision"], ops)


def _run(workspace: str, state: dict[str, Any]) -> dict[str, Any]:
    return draft.execute(workspace, state["draft_id"], state["revision"], "run")["run"]


def _closed(run: dict[str, Any]) -> None:
    balances = run["mixed_solve"]["balances"]
    assert {"carrier_mass", "biomass", "nitrogen", "salinity"} <= set(balances), balances
    assert all(item["passed"] for item in balances.values()), balances


def _nitrogen(run: dict[str, Any], tag: str) -> float:
    return float(run["culture"][tag]["values"]["nitrogen"]["mass_specific"])


def test_golden_dosing_unit_with_downstream_heater_is_flashed_by_real_dwsim_and_responds_to_dose_split(studio):
    ws = studio.workspace
    _, version = publish_unit(studio, GOLDEN_BODY, GOLDEN_CONTRACT, workspace=ws, case=golden_case())
    state = _draft(ws, golden_ops(version, split=20.0))
    assert not [f for f in state["findings"] if f["severity"] == "blocker"], state["findings"]
    low = _run(ws, state)
    assert low["status"] == "completed", low.get("mixed_solve")
    assert "dwsim_flash" in low["streams"]["Unused"]["state_source"]  # terminal outlet: flashed by DWSIM
    for tag in ("Outlet", "Unused"):  # both outlets carry a DWSIM-reported state (phases, density)
        assert low["streams"][tag]["owner"] == "jarvis_bio"
        assert any(p.get("name") == "Liquid1" and p.get("density_kg_m3", 0) > 0
                   for p in low["streams"][tag]["reported"]["phases"])
    assert low["units"]["Heater"]["owner"] == "dwsim" and low["streams"]["Hot"]["owner"] == "dwsim"
    assert low["streams"]["Hot"]["temperature_K"] == pytest.approx(310.0, abs=0.05)
    assert low["streams"]["Hot"]["mass_flow_kg_s"] == pytest.approx(low["streams"]["Outlet"]["mass_flow_kg_s"], rel=1e-6)
    flows = {t: low["streams"][t]["mass_flow_kg_s"] for t in ("Culture", "Dose", "Outlet", "Unused")}
    assert flows["Outlet"] + flows["Unused"] == pytest.approx(flows["Culture"] + flows["Dose"], rel=1e-9)
    assert flows["Unused"] == pytest.approx(0.8 * flows["Dose"], rel=1e-9)
    _closed(low)
    provenance = low["units"]["Dosing"]["studio"]
    assert provenance["version_id"] == version and provenance["call_count"] == 1
    assert len(provenance["source_sha256"]) == len(provenance["contract_sha256"]) == 64
    state = draft.patch(ws, state["draft_id"], state["revision"], [SetUnitParams(
        op="set_unit_params", unit="dosing", values={"dose_split": Q(value=90.0, unit="percent")})])
    high = _run(ws, state)
    assert high["status"] == "completed", high.get("mixed_solve")
    _closed(high)
    assert _nitrogen(high, "Outlet") > _nitrogen(low, "Outlet")  # the dose is richer in nitrogen than the culture
    assert high["streams"]["Unused"]["mass_flow_kg_s"] == pytest.approx(0.1 * flows["Dose"], rel=1e-9)
    for run in (low, high):  # nitrogen mass over both outlets equals nitrogen mass in (no generation declared)
        mass_in = sum(run["streams"][t]["mass_flow_kg_s"] * _nitrogen(run, t) for t in ("Culture", "Dose"))
        mass_out = sum(run["streams"][t]["mass_flow_kg_s"] * _nitrogen(run, t) for t in ("Outlet", "Unused"))
        assert mass_out == pytest.approx(mass_in, rel=1e-9)


def _loop_run(studio) -> tuple[dict[str, Any], str, float]:
    ws = studio.workspace
    _, version = publish_unit(studio, LOOP_BODY, LOOP_CONTRACT, workspace=ws, case=LOOP_CASE)
    state = _draft(ws, loop_ops(version))
    assert not [f for f in state["findings"] if f["severity"] == "blocker"], state["findings"]
    started = time.monotonic()
    run = _run(ws, state)
    return run, version, time.monotonic() - started


def test_three_studio_units_in_a_recycle_converge_through_the_tear_solver_to_the_closed_form(studio):
    """Closed form (tests/studio_unit_190_recycle_support.py): with f = 0.5, F = 0.02 and S1 = S2 = S3 = 0.005 kg/s,
    R = (f^3(F+S1) + f^2 S2 + f S3)/(1 - f^3) = (0.003125 + 0.00125 + 0.0025)/0.875 = 0.0078571 kg/s,
    M1 = F + R + S1, M2 = f M1 + S2, M3 = f M2 + S3 and Purge_i = (1-f) M_i. All feeds share one culture
    concentration and splitting keeps composition, so every loop stream has that concentration."""
    run, version, elapsed = _loop_run(studio)
    solve = run["mixed_solve"]
    assert run["status"] == "completed" and solve["status"] == "completed", solve
    assert elapsed < 90.0 and solve["limits"]["wall_s"] == 90.0
    expected = closed_form()
    assert expected["Recycle"] == pytest.approx(0.0078571428, rel=1e-6)
    for tag, value in expected.items():
        assert run["streams"][tag]["mass_flow_kg_s"] == pytest.approx(value, rel=1e-3), tag
    total_in = FEED_KG_S + 3 * SIDE_KG_S
    assert sum(run["streams"][f"Purge{i}"]["mass_flow_kg_s"] for i in (1, 2, 3)) == pytest.approx(total_in, rel=1e-3)
    for tag in ("Mixed", "Link1", "Link2", "Recycle", "Return", "Purge1", "Purge3"):
        assert _nitrogen(run, tag) == pytest.approx(_nitrogen(run, "Fresh"), rel=1e-6), tag
    _closed(run)
    iterations = len(solve["history"])
    rows = {tag: run["units"][tag]["studio"] for tag in UNIT_TAGS}
    for tag, row in rows.items():
        assert row["version_id"] == version and row["call_count"] >= 2, (tag, row)
        assert row["call_count"] + row["cache_hits"] >= iterations, (tag, row, iterations)
        assert row["sandbox_seconds"] > 0 and row["runtime_classifications"] == ["ok"]
    print(f"RECYCLE_BUDGET wall_s={elapsed:.1f} iterations={iterations}",
          {tag: {k: row[k] for k in ("call_count", "cache_hits", "sandbox_seconds")} for tag, row in rows.items()})
    assert sum(row["sandbox_seconds"] for row in rows.values()) < 90.0


def test_a_budget_too_small_stops_with_the_typed_wall_budget_never_a_truncated_converged_solve(studio, tmp_path):
    ws = studio.workspace
    _, version = publish_unit(studio, LOOP_BODY, LOOP_CONTRACT, workspace=ws, case=LOOP_CASE)
    state = _draft(ws, loop_ops(version))
    document = draft.load_revision(draft.draft_dir(ws, state["draft_id"]), state["revision"])["document"]
    client, mcp_sha256, dwsim_version = editor._client()
    with client:
        record = mixed_runtime.run(document, action="run", client=client, dwsim_version=dwsim_version,
                                   mcp_sha256=mcp_sha256, run_dir=tmp_path, workspace_id=ws, timeout_s=1.0)
    solve = record["mixed_solve"]
    print("BUDGET_STOP", record["status"], solve.get("reason"), (solve.get("errors") or {}).get("code"))
    assert record["status"] != "completed" and solve["status"] == record["status"]
    assert solve["reason"] == "wall_budget" and solve["errors"]["code"] == "STUDIO_UNIT_TIMEOUT"
