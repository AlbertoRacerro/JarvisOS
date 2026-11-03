"""Exact-head, real-DWSIM 10.2.9 acceptance for spec 180 typed reactor kinetics.

Creates an isolated data root and fresh Process drafts, then calls the actual FastAPI routes and
the configured DWSIM MCP. Acceptance values come from ``kinetics_reference.py --json`` (independent, no
product imports). Evidence is written on failure as well as success and holds measured values.

Example:
  JARVISOS_DWSIM_MCP_PATH=/path/to/dwsim-mcp \
  JARVISOS_180_EVIDENCE=/tmp/180-acceptance.json \
  backend/.venv/bin/python scripts/benchmarks/dwsim/kinetics_acceptance.py [--only A1,A4]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
OUTPUT = Path(os.environ.get("JARVISOS_180_EVIDENCE", "/tmp/jarvisos-180-real-dwsim.json"))
REFERENCE = Path(__file__).resolve().parent / "kinetics_reference.py"
COMPOUNDS = ["Water", "Ethylene oxide", "Ethylene glycol"]
EO, EG = "Ethylene oxide", "Ethylene glycol"
VQ_TOLERANCE = 2e-4   # relative on X against the variable-Q reference
CD_TOLERANCE = 0.015  # relative on X against the constant-density reference


def Q(value: float, unit: str) -> dict[str, Any]:  # noqa: N802 - short quantity builder
    return {"value": value, "unit": unit}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expect(condition: bool, label: str, detail: Any, report: dict[str, Any]) -> None:
    report["assertions"].append({"label": label, "passed": bool(condition), "detail": detail})
    if not condition:
        raise AssertionError(f"{label}: {json.dumps(detail, default=str)[:1500]}")


def _write(report: dict[str, Any]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _request(client: Any, method: str, path: str, body: dict[str, Any] | None = None,
             expect_error: bool = False) -> dict[str, Any]:
    response = client.request(method, path, json=body)
    if expect_error:
        return {"status_code": response.status_code, "body": response.json()}
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path}: HTTP {response.status_code}: {response.text[:1500]}")
    return response.json()


def _reaction(case: dict[str, Any]) -> dict[str, Any]:
    """The typed draft reaction for a reference case (parameters in display units)."""
    params = case["params"]
    law: dict[str, Any] = {"form": case["form"], "substrate": EO,
                           "v_max": Q(params["v_max_kmol_m3_h"], "kmol/[m3.h]"),
                           "k_s": Q(params["k_s_kmol_m3"], "kmol/m3"), "inhibitions": []}
    if case["form"] == "haldane":
        law["k_i"] = Q(params["k_i_kmol_m3"], "kmol/m3")
    for term in params.get("inhibitions", []):
        law["inhibitions"].append({"kind": term["kind"], "inhibitor": term["inhibitor"],
                                   "k_i": Q(term["k_i_kmol_m3"], "kmol/m3")})
    if params.get("temperature"):
        law["temperature"] = {"activation_energy": Q(params["temperature"]["activation_energy_J_mol"], "J/mol"),
                              "reference_temperature": Q(params["temperature"]["reference_temperature_K"], "K")}
    return {"name": f"EO hydration {case['id']}", "stoichiometry": {EO: -1, "Water": -1, EG: 1},
            "base_reactant": EO, "phase": "Liquid", "basis": "MolarConc", "rate_law": law,
            "provenance": {"kind": "synthetic", "note": "180 acceptance case " + case["id"]}}


def _ops(setup: dict[str, Any], reactors: list[tuple[str, str]], reaction: dict[str, Any] | None,
         *, energy: bool = True, mode: str = "isothermic") -> list[dict[str, Any]]:
    """Feed -> reactor(s) in series -> product; each reactor gets its own energy stream."""
    ops: list[dict[str, Any]] = [
        {"op": "set_thermo", "compounds": COMPOUNDS, "property_package": setup["property_package"]},
        {"op": "add_stream", "id": "feed", "tag": "Feed", "x": 30, "y": 100},
        {"op": "set_stream_spec", "stream": "feed", "temperature": Q(setup["temperature_K"], "K"),
         "pressure": Q(setup["pressure_kPa"], "kPa"), "mass_flow": Q(setup["mass_flow_kg_s"], "kg/s"),
         "composition_basis": "mass", "composition": {"Water": setup["mass_fractions"]["Water"],
                                                      EO: setup["mass_fractions"][EO], EG: 0.0}},
    ]
    upstream = "feed"
    for index, (unit_type, tag) in enumerate(reactors):
        uid = f"r{index}"
        ops += [{"op": "add_unit", "id": uid, "type": unit_type, "tag": tag, "x": 250 + 250 * index, "y": 100},
                {"op": "connect", "stream": upstream, "end": "target", "unit": uid, "port": 0}]
        values = {"volume": Q(setup["volume_m3"], "m3")}
        if unit_type == "PFR":
            values["length"] = Q(setup.get("pfr_length_m", 10.0), "m")
        ops.append({"op": "set_unit_params", "unit": uid, "mode": mode, "values": values})
        if energy:
            ops += [{"op": "add_stream", "id": f"q{index}", "tag": f"Q{index}", "stream_type": "energy",
                     "x": 250 + 250 * index, "y": 220},
                    {"op": "connect", "stream": f"q{index}", "end": "target", "unit": uid, "port": 0}]
        out = f"s{index}"
        ops += [{"op": "add_stream", "id": out, "tag": f"Out{index}", "x": 380 + 250 * index, "y": 100},
                {"op": "connect", "stream": out, "end": "source", "unit": uid, "port": 0}]
        if reaction is not None:
            ops.append({"op": "set_reactor_reaction", "unit": uid, "reaction_id": "r1", "reaction": reaction})
        upstream = out
    return ops


def _seed(client: Any, workspace_id: str, name: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    base = f"/workspaces/{workspace_id}/process/drafts"
    created = _request(client, "POST", base, {"name": name})
    patched = _request(client, "POST", f"{base}/{created['draft_id']}/patch",
                       {"expected_revision": created["revision"], "ops": ops})
    return {"base": base, "draft_id": created["draft_id"], "projection": patched}


def _patch(client: Any, seed: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    seed["projection"] = _request(client, "POST", f"{seed['base']}/{seed['draft_id']}/patch",
                                  {"expected_revision": seed["projection"]["revision"], "ops": ops})
    return seed


def _run(client: Any, seed: dict[str, Any], action: str = "run") -> dict[str, Any]:
    path = f"{seed['base']}/{seed['draft_id']}/revisions/{seed['projection']['revision']}/{action}"
    started = time.monotonic()
    response = _request(client, "POST", path, expect_error=True)
    response["elapsed_s"] = round(time.monotonic() - started, 3)
    return response


def _conversion(run: dict[str, Any], tag: str) -> float:
    return float(run["units"][tag]["kinetics"]["conversion"])


def _stream_conversion(run: dict[str, Any], inlet: str, outlet: str) -> float:
    def eo(tag: str) -> float:
        reported = run["streams"][tag]["reported"]
        mixture = next(p for p in reported["phases"] if p.get("name") == "Mixture")
        return float(reported["molar_flow_mol_s"]) * float(mixture["compounds"][EO]["mole_fraction"])
    return 1.0 - eo(outlet) / eo(inlet)


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1e-300)


def _from_reference(reference: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Translate ``kinetics_reference.py --json`` into draft setup and typed cases (no values are invented here)."""
    ref_setup = reference["setup"]
    setup = {"property_package": ref_setup["package"], "temperature_K": ref_setup["T_K"],
             "pressure_kPa": ref_setup["P_kPa"], "mass_flow_kg_s": ref_setup["mass_flow_kg_s"],
             "mass_fractions": {"Water": 0.7, EO: 0.3}, "volume_m3": ref_setup["V_m3"], "pfr_length_m": 10.0}
    if ref_setup["feed"] != "Water 70 / Ethylene oxide 30 wt %":
        raise SystemExit(f"unexpected reference feed {ref_setup['feed']!r}")
    cases = []
    for item in reference["cases"]:
        p = item["params"]
        form = "haldane" if item["form"] == "haldane" else "monod"
        params: dict[str, Any] = {"v_max_kmol_m3_h": p["V_max"], "k_s_kmol_m3": p["K_S"], "inhibitions": []}
        if form == "haldane":
            params["k_i_kmol_m3"] = p["K_I"]
        if item["form"] in {"monod_noncompetitive", "monod_competitive"}:
            params["inhibitions"].append({"kind": item["form"].split("_", 1)[1], "inhibitor": EG,
                                          "k_i_kmol_m3": p["K_i_EG"]})
        if item["form"] == "monod_temperature":
            params["temperature"] = {"activation_energy_J_mol": p["E_a_J_mol"], "reference_temperature_K": p["T_ref_K"]}
        known = {"monod", "haldane", "monod_noncompetitive", "monod_competitive", "monod_temperature"}
        if item["form"] not in known:
            raise SystemExit(f"unknown reference form {item['form']!r}")
        cases.append({"id": item["id"], "reactor": item["reactor"], "form": form, "params": params,
                      "feed_temperature_K": ref_setup["states"][item["state"]]["T_K"],
                      "X_variable_q": item["X_variable_q"], "X_constant_density": item["X_constant_density"]})
    return setup, cases


def _single_case(client: Any, workspace_id: str, setup: dict[str, Any], case: dict[str, Any],
                 report: dict[str, Any]) -> None:
    unit_type = case["reactor"]
    tag = f"{unit_type}-1"
    seed = _seed(client, workspace_id, f"180 {case['id']} {unit_type}",
                 _ops({**setup, "temperature_K": case.get("feed_temperature_K", setup["temperature_K"])},
                      [(unit_type, tag)], _reaction(case)))
    blockers = [f for f in seed["projection"]["findings"] if f["severity"] == "blocker"]
    _expect(not blockers, f"{case['id']} {unit_type}: no pre-Run blockers", blockers, report)
    script = seed["projection"]["kinetics_scripts"][tag]
    response = _run(client, seed)
    run = response["body"].get("run", {})
    record: dict[str, Any] = {"case": case, "http_status": response["status_code"], "elapsed_s": response["elapsed_s"],
                              "status": run.get("status"), "script": script,
                              "kinetics": (run.get("units") or {}).get(tag, {}).get("kinetics"),
                              "solve": run.get("solve"), "error": run.get("error"),
                              "error_detail": run.get("error_detail")}
    report["cases"][f"{case['id']}-{unit_type}"] = record
    _expect(run.get("status") == "completed", f"{case['id']} {unit_type}: run completed", record, report)
    kin = run["units"][tag]["kinetics"]
    _expect(kin["verified"] is True, f"{case['id']} {unit_type}: Jarvis verification passed", kin, report)
    x = _conversion(run, tag)
    x_streams = _stream_conversion(run, "Feed", "Out0")
    record.update(X=x, X_from_streams=x_streams, X_variable_q=case["X_variable_q"],
                  X_constant_density=case["X_constant_density"],
                  rel_vq=_rel(x, case["X_variable_q"]), rel_cd=_rel(x, case["X_constant_density"]))
    _expect(_rel(x, x_streams) < 1e-9, f"{case['id']} {unit_type}: verified X equals stream X", record, report)
    _expect(record["rel_vq"] <= VQ_TOLERANCE, f"{case['id']} {unit_type}: X within 2e-4 of variable-Q reference",
            {k: record[k] for k in ("X", "X_variable_q", "rel_vq")}, report)
    _expect(record["rel_cd"] <= CD_TOLERANCE, f"{case['id']} {unit_type}: X within 1.5 % of constant-density reference",
            {k: record[k] for k in ("X", "X_constant_density", "rel_cd")}, report)
    hidden = [key for key in run["units"][tag]["reported"] if key.endswith((": Extent", ": Rate", ": Heat"))]
    _expect(not hidden, f"{case['id']} {unit_type}: DWSIM per-reaction Extent/Rate/Heat not shown", hidden, report)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="comma-separated case ids (A1..A7); default all")
    parser.add_argument("--skip-extras", action="store_true", help="only the A-table cases")
    args = parser.parse_args()
    runtime = Path(os.environ["JARVISOS_DWSIM_MCP_PATH"]).resolve()
    if not runtime.is_file():
        raise SystemExit(f"DWSIM MCP not found: {runtime}")
    os.environ["JARVISOS_DWSIM_MCP_SHA256"] = _sha256(runtime)
    reference = json.loads(subprocess.check_output([sys.executable, str(REFERENCE), "--json"], text=True))
    setup, all_cases = _from_reference(reference)
    report: dict[str, Any] = {
        "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
        "dwsim_required": "10.2.9", "dwsim_mcp_sha256": _sha256(runtime), "reference": reference,
        "cases": {}, "extras": {}, "assertions": [], "complete": False,
    }
    only = {item.strip() for item in args.only.split(",") if item.strip()}
    try:
        with tempfile.TemporaryDirectory(prefix="jarvisos-180-accept-") as data_root:
            os.environ["JARVISOS_DATA_ROOT"] = data_root
            from fastapi.testclient import TestClient

            from app.core.config import get_settings
            from app.core.database import initialize_database
            from app.main import app
            from app.modules.process_stack import draft_compiler, kinetics

            get_settings.cache_clear()
            initialize_database()
            with TestClient(app) as client:
                workspace_id = _request(client, "POST", "/workspaces", {
                    "name": "180 real DWSIM acceptance", "slug": "accept-180-real"})["id"]
                cases = [case for case in all_cases if not only or case["id"] in only]
                for case in cases:
                    _single_case(client, workspace_id, setup, case, report)
                if args.skip_extras:
                    report["complete"] = True
                    return
                a1 = next(case for case in all_cases if case["id"] == "A1" and case["reactor"] == "CSTR")
                # Two CSTRs in series sharing one reaction: one definition, two native ids, both verified.
                series = _seed(client, workspace_id, "180 shared series",
                               _ops(setup, [("CSTR", "CSTR-1"), ("CSTR", "CSTR-2")], _reaction(a1)))
                scripts = series["projection"]["kinetics_scripts"]
                _expect({scripts["CSTR-1"]["native_reaction_id"], scripts["CSTR-2"]["native_reaction_id"]}
                        == {"r1__CSTR-1", "r1__CSTR-2"}, "shared reaction compiles to two native ids", scripts, report)
                response = _run(client, series)
                run = response["body"].get("run", {})
                report["extras"]["series"] = {"status": run.get("status"), "elapsed_s": response["elapsed_s"],
                                              "kinetics": {tag: (run.get("units") or {}).get(tag, {}).get("kinetics")
                                                           for tag in ("CSTR-1", "CSTR-2")}}
                _expect(run.get("status") == "completed" and all(
                    run["units"][tag]["kinetics"]["verified"] for tag in ("CSTR-1", "CSTR-2")),
                    "two CSTRs sharing a reaction solve and verify", report["extras"]["series"], report)
                first = _conversion(run, "CSTR-1")
                _expect(_rel(first, a1["X_variable_q"]) <= VQ_TOLERANCE, "series first CSTR matches A1",
                        {"X": first, "ref": a1["X_variable_q"]}, report)
                # Mixed draft through 168: Jarvis SpecifiedSeparator downstream of a CSTR is not
                # culture-capable (CSTR refuses culture), so the mixed case is a CSTR inside a DWSIM
                # segment of a recycle-free mixed draft only if the registry admits it; record the outcome.
                report["extras"]["mixed"] = _mixed_case(client, workspace_id, setup, a1, report)
                # Determinism: compile twice identical; three solves identical.
                document = _document(series)
                first_render = json.dumps(draft_compiler.expected(document), sort_keys=True)
                _expect(first_render == json.dumps(draft_compiler.expected(document), sort_keys=True),
                        "compile twice gives identical expected()", None, report)
                single = _seed(client, workspace_id, "180 determinism", _ops(setup, [("CSTR", "CSTR-1")], _reaction(a1)))
                xs = []
                for _ in range(3):
                    run = _run(client, single)["body"]["run"]
                    xs.append(run["units"]["CSTR-1"]["kinetics"]["conversion"])
                report["extras"]["determinism"] = xs
                _expect(len(set(xs)) == 1, "three solves are bit-identical", xs, report)
                # Refusals before any DWSIM call: missing energy stream; refused mode.
                no_energy = _seed(client, workspace_id, "180 no energy",
                                  _ops(setup, [("CSTR", "CSTR-1")], _reaction(a1), energy=False))
                codes = {f["code"] for f in no_energy["projection"]["findings"] if f["severity"] == "blocker"}
                refused = _run(client, no_energy)
                report["extras"]["no_energy"] = {"codes": sorted(codes), "http": refused["status_code"]}
                _expect("REACTOR_ENERGY_STREAM_MISSING" in codes and refused["status_code"] == 422,
                        "missing CSTR energy stream blocks Run before DWSIM", report["extras"]["no_energy"], report)
                pfr_case = next(case for case in all_cases if case["reactor"] == "PFR")
                adiabatic = _seed(client, workspace_id, "180 refused mode",
                                  _ops(setup, [("PFR", "PFR-1")], _reaction(pfr_case), mode="adiabatic"))
                codes = {f["code"] for f in adiabatic["projection"]["findings"] if f["severity"] == "blocker"}
                refused = _run(client, adiabatic)
                report["extras"]["refused_mode"] = {"codes": sorted(codes), "http": refused["status_code"]}
                _expect("KINETICS_MODE_UNSUPPORTED" in codes and refused["status_code"] == 422,
                        "refused PFR mode blocks Run before DWSIM", report["extras"]["refused_mode"], report)
                # Silent DWSIM failure (fact 3): a script that raises makes DWSIM return the unreacted
                # feed with ok=true. Inject one (test-only monkeypatch of the template) and require that
                # Jarvis verification refuses the result instead of reporting X = 0 as current.
                broken = _seed(client, workspace_id, "180 silent failure", _ops(setup, [("CSTR", "CSTR-1")], _reaction(a1)))
                original_render = kinetics.render
                kinetics.render = lambda reaction: "r = undefined_name_180\n"  # type: ignore[assignment]
                try:
                    run = _run(client, broken)["body"]["run"]
                finally:
                    kinetics.render = original_render  # type: ignore[assignment]
                failed = (run.get("solve") or {}).get("failed_objects") or []
                report["extras"]["silent_failure"] = {"status": run.get("status"), "failed_objects": failed,
                                                      "kinetics": (run.get("units") or {}).get("CSTR-1", {}).get("kinetics")}
                _expect(run.get("status") == "failed" and any(item.get("code") == "KINETICS_VERIFICATION_FAILED"
                                                              for item in failed),
                        "a silently failing DWSIM script is refused by Jarvis verification",
                        report["extras"]["silent_failure"], report)
                report["kinetics_version"] = kinetics.KINETICS_VERSION
        report["complete"] = True
    except Exception as exc:  # noqa: BLE001 - evidence is written on failure too
        report["failure"] = f"{type(exc).__name__}: {exc}"[:3000]
        raise
    finally:
        _write(report)
        print(json.dumps({"complete": report["complete"], "failure": report.get("failure"),
                          "passed": sum(1 for a in report["assertions"] if a["passed"]),
                          "assertions": len(report["assertions"]), "evidence": str(OUTPUT)}))


def _document(seed: dict[str, Any]) -> dict[str, Any]:
    from app.modules.process_stack import draft

    projection = seed["projection"]
    directory = draft.draft_dir(projection["workspace_id"], projection["draft_id"])
    return draft.load_revision(directory, projection["revision"])["document"]


def _mixed_case(client: Any, workspace_id: str, setup: dict[str, Any], case: dict[str, Any],
                report: dict[str, Any]) -> dict[str, Any]:
    """Jarvis SpecifiedSeparator + CSTR through the 168 mixed solve (the reactor sits in a DWSIM segment)."""
    ops = _ops(setup, [("CSTR", "CSTR-1")], _reaction(case))
    # Culture cannot pass through a reactor (CSTR refuses culture), so the Jarvis separator treats a
    # separate culture feed while the CSTR sits in its own DWSIM segment of the same mixed draft.
    ops += [
        {"op": "add_stream", "id": "cfeed", "tag": "CultureFeed", "x": 450, "y": 300},
        {"op": "set_stream_spec", "stream": "cfeed", "temperature": Q(25, "degC"), "pressure": Q(2, "bar"),
         "mass_flow": Q(0.5, "kg/s"), "composition_basis": "mass", "composition": {"Water": 1.0}},
        {"op": "set_stream_culture", "stream": "cfeed", "culture": {
            "biomass": Q(1, "kg/m3"), "nitrogen": Q(0.05, "kg/m3"), "oxygen": Q(0.008, "kg/m3"),
            "salinity": Q(35, "g/kg")}},
        {"op": "add_unit", "id": "sep", "type": "SpecifiedSeparator", "tag": "Separator", "x": 600, "y": 300},
        {"op": "connect", "stream": "cfeed", "end": "target", "unit": "sep", "port": 0},
        {"op": "add_stream", "id": "top", "tag": "Concentrate", "x": 750, "y": 250},
        {"op": "connect", "stream": "top", "end": "source", "unit": "sep", "port": 0},
        {"op": "add_stream", "id": "bottom", "tag": "Clarified", "x": 750, "y": 350},
        {"op": "connect", "stream": "bottom", "end": "source", "unit": "sep", "port": 1},
        {"op": "set_unit_params", "unit": "sep", "mode": "specified", "values": {
            "biomass_recovery": Q(90, "percent"), "concentration_factor": Q(10, "dimensionless")}},
    ]
    seed = _seed(client, workspace_id, "180 mixed", ops)
    blockers = [f for f in seed["projection"]["findings"] if f["severity"] == "blocker"]
    result: dict[str, Any] = {"pre_run_blockers": blockers}
    if blockers:
        return result
    response = _run(client, seed)
    run = response["body"].get("run", {})
    result.update(status=run.get("status"), mixed=bool(run.get("mixed_solve")), elapsed_s=response["elapsed_s"],
                  kinetics=(run.get("units") or {}).get("CSTR-1", {}).get("kinetics"),
                  error=run.get("error"), error_detail=run.get("error_detail"))
    _expect(run.get("status") == "completed" and result["kinetics"] and result["kinetics"]["verified"],
            "mixed draft with a CSTR in a DWSIM segment solves and verifies through 168", result, report)
    return result


if __name__ == "__main__":
    main()
