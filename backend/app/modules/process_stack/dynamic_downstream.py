"""Quasi-steady DWSIM sampling for a frozen dynamic Process snapshot."""

from __future__ import annotations

import copy
import hashlib
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.modules.process_stack import dwsim, mixed, mixed_runtime

DWSIM_TIMEOUT_S = 120.0
DWSIM_ITERATION_CAP = 12
_SOLVED_STREAM_FIELDS = (
    "mass_flow_kg_s", "molar_flow_mol_s", "mass_fractions", "temperature_K",
    "pressure_Pa", "vapor_fraction", "density_kg_m3", "enthalpy_kJ_kg",
)


def downstream_document(snapshot: Any) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """Freeze the DWSIM-owned downstream subgraph and its participating boundary streams."""
    payload = snapshot.payload
    document = payload["document"]
    objects = document["objects"]
    pbr_ids = {item["id"] for item in payload.get("topology", {}).get("units", [])}
    if not pbr_ids:
        pbr_ids = {item["unit"]["id"] for item in payload["units"]}
    selected: set[str] = set()
    boundary: dict[str, dict[str, Any]] = {}
    frontier = list(pbr_ids)
    while frontier:
        current = frontier.pop()
        for stream in objects.values():
            if stream.get("kind") != "stream":
                continue
            source = (stream.get("source") or {}).get("unit")
            target = (stream.get("target") or {}).get("unit")
            if source == current and target not in pbr_ids:
                if target is None:
                    continue
                unit = objects.get(target, {})
                if unit.get("type") in {"Mixer", "Splitter"}:
                    if target not in selected:
                        selected.add(target)
                        frontier.append(target)
                elif unit.get("type") in {"Heater", "Pump", "Flash", "Valve", "SpecifiedSeparator", "Recycle"}:
                    if target not in selected:
                        selected.add(target)
                        frontier.append(target)
    if not selected:
        return None, {}

    relevant: dict[str, dict[str, Any]] = {}
    for stream in objects.values():
        if stream.get("kind") != "stream":
            continue
        source = (stream.get("source") or {}).get("unit")
        target = (stream.get("target") or {}).get("unit")
        if source in selected or target in selected:
            item = copy.deepcopy(stream)
            if source in pbr_ids:
                item["source"] = None
                boundary[item["tag"]] = copy.deepcopy(stream)
            elif source not in selected:
                continue
            if target not in selected:
                item["target"] = None
            relevant[item["id"]] = item

    derived = {key: copy.deepcopy(value) for key, value in document.items() if key != "objects"}
    derived["objects"] = {uid: copy.deepcopy(objects[uid]) for uid in selected} | relevant
    return derived, boundary


def _state_feed(original: dict[str, Any], state: dict[str, Any], density: float,
                carrier_spec: dict[str, Any]) -> dict[str, Any]:
    feed = copy.deepcopy(original)
    flow = float(state.get("flow_m3_s", 0.0))
    concentrations = state.get("concentrations") or {
        "X": state.get("X_kg_m3"), "N": state.get("N_kg_m3"), "O2": state.get("O2_kg_m3")}
    spec = feed.setdefault("spec", {})
    for key in ("temperature", "pressure", "composition_basis", "composition"):
        if not spec.get(key) and key in carrier_spec:
            spec[key] = copy.deepcopy(carrier_spec[key])
    spec["mass_flow"] = {"si": flow * density, "value": flow * density, "unit": "kg/s"}
    culture = spec.setdefault("culture", {})
    for key, channel in (("biomass", "X"), ("nitrogen", "N"), ("oxygen", "O2")):
        value = concentrations.get(channel)
        if value is not None:
            culture[key] = {"si": float(value), "value": float(value), "unit": "kg/m3"}
    return feed


def _midpoint(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Average matching scalar tear fields; retain fields available on only one side."""
    result = copy.deepcopy(left)
    for tag, state in right.items():
        if tag not in result:
            result[tag] = copy.deepcopy(state)
            continue
        for key, value in state.items():
            if isinstance(value, dict) and isinstance(result[tag].get(key), dict):
                for field, field_value in value.items():
                    before = result[tag][key].get(field)
                    if isinstance(before, (int, float)) and isinstance(field_value, (int, float)):
                        result[tag][key][field] = (before + field_value) / 2
                    elif field not in result[tag][key]:
                        result[tag][key][field] = copy.deepcopy(field_value)
            elif isinstance(value, (int, float)) and isinstance(result[tag].get(key), (int, float)):
                result[tag][key] = (result[tag][key] + value) / 2
    return result


def _client_factory() -> Any:
    client, _path, _version = dwsim._client()
    return client


def build_sampler(
    snapshot: Any,
    *,
    client_factory: Callable[[], Any] | None = None,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    """Build once from the immutable snapshot; each sample gets a bounded DWSIM solve."""
    prepared = snapshot.payload.get("downstream")
    if prepared:
        derived, boundary_templates = prepared["document"], prepared["boundary_streams"]
    else:
        derived, boundary_templates = downstream_document(snapshot)
    if derived is None:
        return None
    factory = client_factory or _client_factory
    solve = runner or mixed_runtime.run
    payload = snapshot.payload
    topology = payload["topology"]
    stream_by_tag = {item["tag"]: item for item in topology["streams"]}
    participating_ids = {item["id"] for item in topology.get("units", [])}
    if not participating_ids:
        participating_ids = {item["unit"]["id"] for item in payload.get("units", [])}
    objects = payload["document"]["objects"]
    start_epoch = payload.get("start_epoch")

    def sample_timestamp(time_s: float) -> str | None:
        if start_epoch is None:
            return None
        return (datetime.fromtimestamp(float(start_epoch), UTC) + timedelta(seconds=float(time_s))
                ).isoformat().replace("+00:00", "Z")

    carrier_spec: dict[str, Any] = next((copy.deepcopy(stream.get("spec", {})) for stream in objects.values()
                         if stream.get("kind") == "stream" and stream.get("source") is None
                         and (stream.get("target") or {}).get("unit") in participating_ids), {})
    densities: dict[str, tuple[float, str]] = {}
    for tag, stream in stream_by_tag.items():
        flow = float(topology["flows"].get(stream["id"], 0.0))
        mass = (stream.get("spec", {}).get("mass_flow") or {}).get("si")
        if flow > 0 and mass is not None and float(mass) > 0:
            densities[tag] = (float(mass) / flow, "declared_mass_flow_over_volumetric_flow")
    fallback_density = next(iter(densities.values()), (1000.0, "water_carrier_default_1000_kg_m3"))
    fallback_assumed = not bool(densities)
    try:
        last_converged = mixed_runtime._seed(derived, mixed.partition(derived))
    except Exception:  # seed remains optional; DWSIM may still produce a valid run-specific seed
        last_converged = None

    def sample(boundary_state: dict[str, Any]) -> dict[str, Any]:
        nonlocal last_converged
        document = copy.deepcopy(derived)
        for tag, original in boundary_templates.items():
            source = boundary_state.get("streams", {}).get(tag)
            if source is None:
                continue
            density, density_source = densities.get(tag, fallback_density)
            feed = _state_feed(original, source, density, carrier_spec)
            feed["source"] = None
            document["objects"][feed["id"]] = feed
        if last_converged is None:
            try:
                last_converged = mixed_runtime._seed(document, mixed.partition(document))
            except Exception:  # DWSIM may still find a viable native seed from the boundary state
                pass
        started = time.monotonic()
        client = factory()
        try:
            enter = getattr(client, "__enter__", None)
            if enter:
                client = enter()
            expected = getattr(client, "expected_sha256", "")
            executable = getattr(client, "executable", Path("dwsim"))
            digest = expected or hashlib.sha256(str(executable).encode()).hexdigest()
            version = dwsim._version(Path(executable)) if Path(executable).is_file() else "injected"
            with tempfile.TemporaryDirectory(prefix="jarvis-dynamic-downstream-") as directory:
                result = solve(
                    document,
                    action="run",
                    client=client,
                    dwsim_version=version,
                    mcp_sha256=digest,
                    run_dir=Path(directory),
                    timeout_s=DWSIM_TIMEOUT_S,
                    max_iterations=DWSIM_ITERATION_CAP,
                    initial_tear=copy.deepcopy(last_converged) if last_converged is not None else None,
                    include_tear_state=True,
                )
        except Exception as exc:  # noqa: BLE001 - downstream failure cannot invalidate biology
            reason = "timeout" if "timeout" in type(exc).__name__.lower() or isinstance(exc, TimeoutError) else "solve_error"
            return {"status": "downstream_unconverged", "reason": reason,
                    "error_type": type(exc).__name__, "time_s": boundary_state["time_s"],
                    "time_utc": sample_timestamp(boundary_state["time_s"]),
                    "retry_skipped": {"reason": "initial solve raised before a retry guess was available"},
                    "density_assumed": fallback_assumed,
                    **({"density_assumption_reason": fallback_density[1]} if fallback_assumed else {})}
        finally:
            close = getattr(client, "__exit__", None)
            if close:
                close(None, None, None)
        if result.get("status") != "completed":
            first = result
            attempt = result.get("_last_attempt") or {}
            guess, failed = attempt.get("guess"), attempt.get("output")
            converged = last_converged or guess
            failed_attempt = failed or guess or converged
            retry_guess = (_midpoint(converged, failed_attempt)
                           if isinstance(converged, dict) and isinstance(failed_attempt, dict) else None)
            retry_skipped = None
            if retry_guess is not None and time.monotonic() - started < DWSIM_TIMEOUT_S:
                retry_client = factory()
                try:
                    retry_enter = getattr(retry_client, "__enter__", None)
                    if retry_enter:
                        retry_client = retry_enter()
                    with tempfile.TemporaryDirectory(prefix="jarvis-dynamic-downstream-retry-") as directory:
                        second = solve(
                            document, action="run", client=retry_client,
                            dwsim_version=version, mcp_sha256=digest, run_dir=Path(directory),
                            timeout_s=max(1.0, DWSIM_TIMEOUT_S - (time.monotonic() - started)),
                            max_iterations=DWSIM_ITERATION_CAP, initial_tear=retry_guess,
                            include_tear_state=True,
                        )
                    result = second
                except Exception as exc:  # noqa: BLE001
                    result = {"status": "failed",
                              "reason": "timeout" if isinstance(exc, TimeoutError) else "solve_error",
                              "error_type": type(exc).__name__}
                finally:
                    close = getattr(retry_client, "__exit__", None)
                    if close:
                        close(None, None, None)
            else:
                result = first
                retry_skipped = {"reason": "no_retry_guess" if retry_guess is None else "time_budget_exhausted"}
        if result.get("status") != "completed":
            solve_detail = result.get("mixed_solve", {})
            reason = ("timeout" if solve_detail.get("reason") == "wall_budget"
                      or result.get("reason") == "timeout"
                      or "timeout" in str(result.get("error_type", "")).lower()
                      else "segment_failed" if result.get("status") == "segment_failed"
                      else "non_convergence")
            residual = None
            history = solve_detail.get("history", [])
            if history:
                residual = history[-1].get("max_normalized_residual")
            diagnostics = {key: value for key, value in {
                "solve_status": result.get("status"),
                "solve_reason": solve_detail.get("reason") or result.get("reason"),
                "iterations": len(history) if history else None,
                "worst_tear": history[-1].get("worst_tear") if history else None,
                "failed_segment": solve_detail.get("failed_segment"),
                "failed_units": solve_detail.get("failed_units") or [
                    item.get("tag") for item in (result.get("solve") or {}).get("failed_objects", [])] or None,
                "message": str(solve_detail.get("message") or result.get("error")
                               or "; ".join(map(str, (result.get("solve") or {}).get("errors", []))) or "")[:500]
                or None,
            }.items() if value is not None}
            return {"status": "downstream_unconverged", "reason": reason,
                    "residual": residual, "diagnostics": diagnostics, "time_s": boundary_state["time_s"],
                    "time_utc": sample_timestamp(boundary_state["time_s"]),
                    **({"retry_skipped": retry_skipped} if retry_skipped is not None else {}),
                    "density_assumed": fallback_assumed,
                    **({"density_assumption_reason": fallback_density[1]} if fallback_assumed else {}),
                    **({"error_type": result["error_type"]} if result.get("error_type") else {})}
        if isinstance(result.get("_tear_state"), dict):
            last_converged = copy.deepcopy(result["_tear_state"])
        streams = result.get("streams", {})
        boundary_tags = {stream["tag"] for stream in boundary_templates.values()}
        # Boundary inputs are what Jarvis fed DWSIM from the CVODE state; solved outputs are what DWSIM
        # returned for every other stream of the downstream subgraph. A solved stream DWSIM did not report
        # stays None so a missing value is never confused with zero or with an echoed input.
        solved_streams: dict[str, Any] = {}
        for item in derived["objects"].values():
            if item.get("kind") != "stream" or item["tag"] in boundary_tags:
                continue
            solved = streams.get(item["tag"])
            if not isinstance(solved, dict):
                solved_streams[item["tag"]] = None
                continue
            fields = (_SOLVED_STREAM_FIELDS if item.get("type") != "EnergyStream"
                      else tuple(key for key, value in solved.items() if isinstance(value, (int, float))))
            solved_streams[item["tag"]] = {
                **{key: solved[key] for key in fields if key in solved},
                "provenance": "dwsim_solved", "owner": solved.get("owner", "dwsim")}
        selected_tags = {item["tag"] for item in derived["objects"].values() if item.get("kind") == "unit"}
        solved_units = {tag: mixed_runtime.finite_json({**value, "provenance": "dwsim_solved"})
                        for tag, value in (result.get("units") or {}).items() if tag in selected_tags}
        boundary_inputs: dict[str, Any] = {}
        for original in boundary_templates.values():
            tag = original["tag"]
            source = boundary_state.get("streams", {}).get(tag)
            if source is None:
                boundary_inputs[tag] = None
                continue
            fed = document["objects"][original["id"]]["spec"]
            boundary_inputs[tag] = {
                "provenance": "jarvis_t1_snapshot",
                "flow_m3_s": float(source.get("flow_m3_s", 0.0)),
                "mass_flow_kg_s": fed["mass_flow"]["si"],
                "concentrations_kg_m3": {key: value["si"] for key, value in fed.get("culture", {}).items()
                                         if isinstance(value, dict) and "si" in value},
                **({"temperature_K": fed["temperature"]["si"]} if "si" in (fed.get("temperature") or {}) else {}),
                **({"pressure_Pa": fed["pressure"]["si"]} if "si" in (fed.get("pressure") or {}) else {}),
            }
        density_sources = {tag: {"rho_kg_m3": densities.get(tag, fallback_density)[0],
                                 "source": densities.get(tag, fallback_density)[1],
                                 **({"density_assumed": True, "reason": fallback_density[1]}
                                    if tag not in densities else {})}
                           for tag in boundary_inputs}
        solve_detail = result.get("mixed_solve") or {}
        return {"status": "succeeded", "time_s": boundary_state["time_s"],
                "time_utc": sample_timestamp(boundary_state["time_s"]),
                "boundary_inputs": boundary_inputs, "streams": solved_streams, "units": solved_units,
                **({"culture": mixed_runtime.finite_json(result["culture"])} if result.get("culture") else {}),
                "solve": {key: value for key, value in {
                    "status": result.get("status"), "reason": solve_detail.get("reason"),
                    "iterations": len(solve_detail.get("history") or []) or None,
                    "balances": mixed_runtime.finite_json({
                        name: {key: row.get(key) for key in ("residual", "tolerance", "unit", "passed")}
                        for name, row in (solve_detail.get("balances") or {}).items() if isinstance(row, dict)}) or None,
                }.items() if value is not None},
                "boundary_density_sources": density_sources, "density_assumed": fallback_assumed,
                **({"density_assumption_reason": fallback_density[1]} if fallback_assumed else {})}

    return sample
