"""Operator-facing run outcome, results view and KPI summary of a persisted draft run (spec 181).

Everything here is a deterministic projection of ``run.json`` and the draft document at the run's
revision. It adds no physics: values are DWSIM's or Jarvis evaluators' reported quantities, unit
conversions, and sums/ratios named in the KPI definitions. Unknown values stay null with a reason.
"""

from __future__ import annotations

import math
from typing import Any

LAST_ITERATE_LABEL = "Not converged — last iterate"
NOT_CONVERGED_REASON = "run not converged"
NO_CARBON_REASON = ("T1 does not model carbon (spec 170): carbon is assumed externally supplied "
                    "and non-limiting")
OUTCOME_LABELS = {"validated": "Validated (not solved)", "converged": "Converged",
                  "non_converged": "Not converged", "failed": "Failed", "cancelled": "Cancelled"}
# DWSIM accepts a Recycle once its error is below the compiled tolerance; the margin only absorbs the
# kg/h <-> kg/s round trip of the reported value.
_NATIVE_TOLERANCE_MARGIN = 1e-6


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    from app.modules.process_stack.draft import plain_text
    return plain_text(value)


# ---------------------------------------------------------------- outcome


def _native_recycle_failures(run: dict[str, Any], document: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Native Recycles whose reported mass-flow error exceeds the tolerance Jarvis compiled for them."""
    from app.modules.process_stack.draft_compiler import recycle_mass_flow_tolerance_kg_s
    from app.modules.process_stack.mixed_runtime import _native_mass_flow_error_kg_s

    recycle_tags = ({item["tag"] for item in document["objects"].values()
                     if item["kind"] == "unit" and item["type"] == "Recycle"} if document else None)
    fallback = (recycle_mass_flow_tolerance_kg_s(document)
                if document is not None and not run.get("mixed_solve") else None)
    failures = []
    for tag, result in sorted((run.get("units") or {}).items()):
        if not isinstance(result, dict) or result.get("owner") == "jarvis_bio":
            continue
        tolerance = _number(result.get("mass_flow_tolerance_kg_s"))
        if tolerance is None:
            if recycle_tags is None or tag not in recycle_tags or fallback is None:
                continue  # an older mixed run records no compiled tolerance per segment
            tolerance = fallback
        error = _native_mass_flow_error_kg_s(result)
        if error is not None and error > tolerance * (1.0 + _NATIVE_TOLERANCE_MARGIN):
            failures.append({"tag": tag, "stage": "native_recycle",
                             "error": f"Recycle mass-flow error {error:.6g} kg/s exceeds its tolerance "
                                      f"{tolerance:.6g} kg/s"})
    return failures


def _mixed_error_text(errors: Any) -> str | None:
    if not isinstance(errors, dict):
        return _text(errors)
    diffs = errors.get("materialization_diffs") or errors.get("diffs") or []
    if diffs and isinstance(diffs[0], dict):
        first = diffs[0]
        return _text(f"{len(diffs)} materialization difference(s); first {first.get('path')}: draft expects "
                     f"{first.get('expected')}, DWSIM holds {first.get('actual')}")
    return _text(errors.get("message")) or _text(errors.get("code"))


def _kinetics_failure(run: dict[str, Any]) -> str | None:
    for item in (run.get("solve") or {}).get("failed_objects") or []:
        code = str(item.get("code") or "")
        if code.startswith("KINETICS_"):
            return code
    return None


def run_outcome(run: dict[str, Any], document: dict[str, Any] | None = None) -> dict[str, Any]:
    """The single operator-facing outcome of one persisted draft run (spec 181 decision 1).

    ``document`` is the draft at the run's revision; it is needed only to bound native Recycle errors
    of runs recorded before Jarvis stored the compiled tolerance on the unit result.
    """
    status = str(run.get("status") or "")
    mixed_solve = run.get("mixed_solve") or {}
    solve = run.get("solve")
    detail = run.get("error_detail") or {}
    failing: list[dict[str, Any]] = []
    reason: str | None = None
    message: str | None = None
    if status == "validated":
        state = "validated"
    elif status == "completed":
        failing = _native_recycle_failures(run, document)
        state = "non_converged" if failing else "converged"
        if failing:
            reason = "NATIVE_RECYCLE_NOT_CONVERGED"
            message = "DWSIM reported success, but a native Recycle did not meet its mass-flow tolerance."
    elif status == "unconverged":
        state, reason = "non_converged", mixed_solve.get("reason") or status
    elif status == "failed" and isinstance(solve, dict) and (_kinetics_failure(run) or solve.get("ok") is not True
                                                              or solve.get("failed_objects")):
        kinetics_code = _kinetics_failure(run)
        state = "failed" if kinetics_code else "non_converged"
        reason = kinetics_code or "DWSIM_SOLVE_NOT_CONVERGED"
    elif status == "cancelled":
        state, reason = "cancelled", "cancelled"
    else:
        state = "failed"
        reason = (mixed_solve.get("reason") if status == "segment_failed" and mixed_solve.get("reason")
                  else status or "unknown")
    if not failing:
        failing = [{"tag": item.get("tag"), "stage": "dwsim_solve",
                    "error": _text(item.get("error")) or "not calculated"}
                   for item in ((solve or {}).get("failed_objects") or [])[:20] if isinstance(item, dict)]
        segment_error = _text(mixed_solve.get("message")) or _mixed_error_text(mixed_solve.get("errors"))
        failing += [{"tag": tag, "stage": mixed_solve.get("failed_segment"), "error": segment_error}
                    for tag in mixed_solve.get("failed_units") or []]
        if mixed_solve.get("failed_segment") and not mixed_solve.get("failed_units"):
            failing.append({"tag": None, "stage": mixed_solve["failed_segment"], "error": segment_error})
        if status == "check_failed":
            failing += [{"tag": item.get("object"), "stage": "dwsim_check", "error": _text(item.get("message"))}
                        for item in (run.get("dwsim_check") or {}).get("findings") or []
                        if item.get("severity") in {"blocker", "error"}][:20]
        if detail.get("step") and not failing:
            failing.append({"tag": None, "stage": detail["step"],
                            "error": _text(detail.get("dwsim_message") or run.get("error"))})
    if message is None:
        errors = (solve or {}).get("errors") or []
        message = (_text(mixed_solve.get("message")) or _text(run.get("error")) or _text(detail.get("dwsim_message"))
                   or (_text(errors[0]) if errors else None) or _text(mixed_solve.get("diagnosis")))
    history = mixed_solve.get("history") if isinstance(mixed_solve.get("history"), list) else None
    last_row = history[-1] if history else {}
    if message is None:
        message = _mixed_error_text(mixed_solve.get("errors"))
    if message is None and state == "non_converged" and history:
        message = (f"The mixed solve stopped ({reason}) after {len(history)} iterations; worst tear "
                   f"{last_row.get('worst_tear') or 'unknown'}.")
    has_values = bool(run.get("streams") or run.get("units"))
    if state == "converged":
        available = "converged"
    elif state in {"non_converged", "failed"} and has_values:
        available = "last_iterate"
    else:
        available = "none"
    if run.get("action") == "run" and run.get("solved_case_sha256"):
        artifact: str | None = f"runs/{run.get('run_id')}/solved.dwxmz"
    elif run.get("action") == "run" and mixed_solve and has_values:
        artifact = f"runs/{run.get('run_id')}/"
    else:
        artifact = None
    return {"state": state, "label": OUTCOME_LABELS[state], "reason": reason, "message": message,
            "failing": failing, "residual": _number(last_row.get("max_normalized_residual")),
            "iterations": len(history) if history is not None else None,
            "worst_tear": last_row.get("worst_tear"), "results_available": available, "artifact": artifact}


def outcome_of(run: dict[str, Any], document: dict[str, Any] | None = None) -> dict[str, Any]:
    """The stored outcome of a new run, or the outcome computed on read for an older one."""
    stored = run.get("outcome")
    return stored if isinstance(stored, dict) and stored.get("state") else run_outcome(run, document)


# ---------------------------------------------------------------- quantities


def quantity(value: Any, unit: str, label: str | None = None) -> dict[str, Any]:
    number = _number(value)
    if number is None:
        return missing("not_computed", "no finite value was reported")
    return {"value": number, "unit": unit, **({"label": label} if label else {})}


def missing(status: str, reason: str) -> dict[str, Any]:
    return {"value": None, "status": status, "reason": reason}


def _label(item: dict[str, Any], label: str | None) -> dict[str, Any]:
    if label and item.get("value") is not None:
        return {**item, "label": label}
    return item


# ---------------------------------------------------------------- view


def _material_streams(document: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted((item for item in document["objects"].values()
                   if item["kind"] == "stream" and item["type"] != "EnergyStream"), key=lambda item: item["tag"])


def _role(stream: dict[str, Any]) -> str:
    if stream.get("source") is None:
        return "feed"
    if stream.get("target") is None:
        return "product"
    return "internal"


def _unit_tag(document: dict[str, Any], endpoint: dict[str, Any] | None) -> str | None:
    if not endpoint:
        return None
    unit = document["objects"].get(endpoint.get("unit"))
    return unit["tag"] if unit else None


def _stream_view(stream: dict[str, Any], result: dict[str, Any] | None, culture: dict[str, Any] | None,
                 document: dict[str, Any], label: str | None, solved: bool) -> dict[str, Any]:
    from app.modules.process_stack.draft import _stream_display

    absent = (missing("not_computed", "DWSIM returned no result for this stream") if solved
              else missing("unavailable", "the run produced no result for this stream"))
    display = (result.get("display") or _stream_display(result)) if result else {}
    view: dict[str, Any] = {"role": _role(stream), "from": _unit_tag(document, stream.get("source")),
                            "to": _unit_tag(document, stream.get("target"))}
    for key in ("temperature", "pressure", "mass_flow", "molar_flow", "volumetric_flow"):
        item = display.get(key)
        if item is not None:
            view[key] = _label(quantity(item.get("value"), item.get("unit") or ""), label)
        elif key == "volumetric_flow" and result:
            view[key] = missing("unavailable", "DWSIM reported no volumetric flow for this stream")
        else:
            view[key] = dict(absent)
    vapor = result.get("vapor_fraction") if result else None
    view["vapor_fraction"] = (_label(quantity(vapor, "1"), label) if _number(vapor) is not None
                              else missing("unavailable", "no vapour phase fraction was reported")
                              if result else dict(absent))
    fractions = (result or {}).get("mass_fractions") or {}
    view["mass_fractions"] = {name: _label(quantity(fractions.get(name), "kg/kg"), label)
                              if name in fractions else missing("unavailable", "not reported by DWSIM")
                              for name in document.get("compounds", [])} if result else {}
    if culture is None:
        view["culture"] = None
    else:
        values = culture.get("values") or {}
        view["culture"] = {}
        for name, item in sorted(values.items()):
            shown = (item or {}).get("display")
            view["culture"][name] = (_label(quantity(shown.get("value"), shown.get("unit") or ""), label)
                                     if shown else missing("unavailable", (item or {}).get("reason")
                                                           or "not specified"))
        if culture.get("status") == "failed":
            view["culture_failure"] = _text(culture.get("message")) or culture.get("finding")
    view["owner"] = (result or {}).get("owner") or ("dwsim" if result else None)
    view["state_source"] = (result or {}).get("state_source")
    return view


def _reported_quantities(result: dict[str, Any], label: str | None) -> dict[str, Any]:
    quantities: dict[str, Any] = {}
    reported = result.get("reported")
    if not isinstance(reported, dict):
        return quantities
    for key, item in sorted(reported.items()):
        if not isinstance(item, dict) or "value" not in item:
            continue
        unit = item.get("units") if item.get("units") is not None else item.get("unit")
        if item.get("value") is None:
            quantities[key] = missing("not_computed", "reported without a value")
            continue
        number = _number(item.get("value"))
        if number is None:
            continue  # text-valued DWSIM properties are not quantities
        quantities[key] = _label({"value": number, "unit": str(unit or ""),
                                  **({"label": item["label"]} if item.get("label") else {})}, label)
    return quantities


def _unit_view(unit: dict[str, Any], result: dict[str, Any] | None, document: dict[str, Any],
               label: str | None) -> dict[str, Any]:
    streams = _material_streams(document)
    inlets = sorted((item for item in streams if (item.get("target") or {}).get("unit") == unit["id"]),
                    key=lambda item: item["target"]["port"])
    outlets = sorted((item for item in streams if (item.get("source") or {}).get("unit") == unit["id"]),
                     key=lambda item: item["source"]["port"])
    view: dict[str, Any] = {"type": unit["type"], "calculated": (result or {}).get("calculated"),
                            "error": _text((result or {}).get("error")),
                            "quantities": _reported_quantities(result or {}, label),
                            "inlets": [item["tag"] for item in inlets], "outlets": [item["tag"] for item in outlets]}
    if result and result.get("owner"):
        view["owner"] = result["owner"]
    if result and result.get("fidelity"):
        view["fidelity"] = result["fidelity"]
    return view


def _boundary(document: dict[str, Any], run: dict[str, Any], solved: bool) -> dict[str, Any]:
    streams = run.get("streams") or {}
    feeds = [item for item in _material_streams(document) if _role(item) == "feed"]
    products = [item for item in _material_streams(document) if _role(item) == "product"]

    def total(group: list[dict[str, Any]], compound: str | None = None) -> dict[str, Any]:
        if not solved:
            return missing("unavailable", NOT_CONVERGED_REASON)
        amount = 0.0
        for stream in group:
            result = streams.get(stream["tag"]) or {}
            flow = _number(result.get("mass_flow_kg_s"))
            if flow is None:
                return missing("not_computed", f"no mass flow for {stream['tag']}")
            if compound is not None:
                fraction = _number((result.get("mass_fractions") or {}).get(compound))
                if fraction is None:
                    return missing("not_computed", f"no {compound} mass fraction for {stream['tag']}")
                flow *= fraction
            amount += flow
        return quantity(amount * 3600.0, "kg/h")

    compounds = document.get("compounds", [])
    return {"inputs": [item["tag"] for item in feeds], "outputs": [item["tag"] for item in products],
            "totals": {"mass_flow_in": total(feeds), "mass_flow_out": total(products),
                       "by_compound_in": {name: total(feeds, name) for name in compounds},
                       "by_compound_out": {name: total(products, name) for name in compounds}}}


def _balances(run: dict[str, Any]) -> dict[str, Any]:
    mixed_balances = (run.get("mixed_solve") or {}).get("balances")
    if isinstance(mixed_balances, dict) and mixed_balances:
        mass = mixed_balances.get("carrier_mass")
        culture = {key: value for key, value in mixed_balances.items() if key != "carrier_mass"} or None
    else:
        mass = run.get("mass_balance")
        culture = None
    unit_balances = {tag: result["unit_balances"] for tag, result in sorted((run.get("units") or {}).items())
                     if isinstance(result, dict) and result.get("unit_balances")}
    unit_balances |= {tag: item["unit_balances"] for tag, item in sorted((run.get("culture") or {}).items())
                      if isinstance(item, dict) and item.get("unit_balances") and tag not in unit_balances}
    return {"mass": mass or {"status": "unavailable", "reason": "no mass balance was recorded"},
            "culture": culture, "unit_balances": unit_balances or None}


def build(run: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """Results view of one persisted run (spec 181 decision 3)."""
    from app.modules.process_stack.draft import result_findings

    outcome = outcome_of(run, document)
    available = outcome["results_available"]
    solved = available == "converged"
    view_label = "current" if solved else "last_iterate" if available == "last_iterate" else "not_solved"
    value_label = LAST_ITERATE_LABEL if view_label == "last_iterate" else None
    show = view_label != "not_solved"
    run_streams = run.get("streams") or {}
    run_units = run.get("units") or {}
    culture = run.get("culture") or {}
    streams = ({item["tag"]: _stream_view(item, run_streams.get(item["tag"]), culture.get(item["tag"]),
                                          document, value_label, solved)
                for item in _material_streams(document)} if show else {})
    units = ({item["tag"]: _unit_view(item, run_units.get(item["tag"]), document, value_label)
              for item in sorted((item for item in document["objects"].values() if item["kind"] == "unit"),
                                 key=lambda item: item["tag"])} if show else {})
    return {"run_id": run.get("run_id"), "draft_revision": run.get("draft_revision"), "outcome": outcome,
            "label": view_label, "value_label": value_label, "streams": streams, "units": units,
            "boundary": _boundary(document, run, solved), "balances": _balances(run) if show else
            {"mass": {"status": "unavailable", "reason": "the run produced no results"}, "culture": None,
             "unit_balances": None},
            "findings": result_findings(document, run if solved else None, run),
            "kpis": kpis(run, document, solved)}


# ---------------------------------------------------------------- KPIs


def _kpi(kpi_id: str, label: str, unit: str, definition: str, *, value: float | None = None,
         status: str = "available", reason: str | None = None, sources: list[str] | None = None) -> dict[str, Any]:
    number = _number(value)
    if status == "available" and number is None:
        status, reason = "unavailable", reason or "the source value is not finite"
    return {"id": kpi_id, "label": label, "value": number if status == "available" else None, "unit": unit,
            "status": status, "reason": None if status == "available" else reason, "definition": definition,
            "sources": sources or []}


def _pointer(*parts: str) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _biomass_rate(culture_item: dict[str, Any] | None, result: dict[str, Any] | None,
                  field: str = "biomass") -> tuple[float | None, str | None]:
    """kg/d of a culture field in a stream: concentration (kg/m³) × volumetric flow (m³/d), 167 basis."""
    if not culture_item:
        return None, "the stream carries no culture values"
    value = (culture_item.get("values") or {}).get(field) or {}
    concentration = _number((value.get("display") or {}).get("value"))
    density = _number(culture_item.get("density_kg_m3"))
    flow = _number((result or {}).get("mass_flow_kg_s"))
    if concentration is None:
        return None, value.get("reason") or f"culture {field} is not known on this stream"
    if density is None or density <= 0 or flow is None:
        return None, "the stream's mass flow or culture density is unavailable"
    return concentration * flow / density * 86400.0, None


def _units_of(document: dict[str, Any], unit_type: str) -> list[dict[str, Any]]:
    return sorted((item for item in document["objects"].values() if item["kind"] == "unit" and item["type"] == unit_type),
                  key=lambda item: item["tag"])


def kpis(run: dict[str, Any], document: dict[str, Any], solved: bool) -> list[dict[str, Any]]:
    """Deterministic KPI summary from authoritative run quantities only (spec 181 decision 4)."""
    streams = _material_streams(document)
    products = [item for item in streams if _role(item) == "product"]
    feeds = [item for item in streams if _role(item) == "feed"]
    pbrs = _units_of(document, "PhotobioreactorT1")
    separators = _units_of(document, "SpecifiedSeparator")
    run_streams = run.get("streams") or {}
    run_units = run.get("units") or {}
    culture = run.get("culture") or {}
    rows: list[dict[str, Any]] = []

    def add(kpi_id: str, label: str, unit: str, definition: str, compute: Any) -> None:
        if not solved:
            rows.append(_kpi(kpi_id, label, unit, definition, status="unavailable", reason=NOT_CONVERGED_REASON))
            return
        value, reason, sources = compute()
        rows.append(_kpi(kpi_id, label, unit, definition, value=value,
                         status="available" if reason is None else "unavailable", reason=reason, sources=sources))

    def product_rate() -> tuple[float | None, str | None, list[str]]:
        carrying = [item for item in products if culture.get(item["tag"])]
        if not carrying:
            return None, "no product stream carries culture values", []
        total = 0.0
        for item in carrying:
            rate, reason = _biomass_rate(culture.get(item["tag"]), run_streams.get(item["tag"]))
            if rate is None:
                return None, f"{item['tag']}: {reason}", []
            total += rate
        return total, None, [source for item in carrying for source in
                             (_pointer("culture", item["tag"], "values", "biomass"),
                              _pointer("streams", item["tag"], "mass_flow_kg_s"))]

    add("biomass_product_rate", "Biomass product rate", "kg/d",
        "Σ over product boundary streams of culture biomass (kg/m³) × volumetric flow (m³/d), 167 culture basis",
        product_rate)

    def reported(tag: str, key: str) -> Any:
        def compute() -> tuple[float | None, str | None, list[str]]:
            item = ((run_units.get(tag) or {}).get("reported") or {}).get(key) or {}
            value = _number(item.get("value"))
            if value is None:
                return None, "not reported by the PBR evaluator", []
            return value, None, [_pointer("units", tag, "reported", key)]
        return compute

    for unit in pbrs:
        tag = unit["tag"]
        add(f"pbr_volumetric_productivity:{tag}", f"{tag} volumetric productivity", "kg/(m³·d)",
            "PBR reported net volumetric biomass productivity", reported(tag, "volumetric_productivity"))
        add(f"pbr_net_biomass_production:{tag}", f"{tag} net biomass production", "kg/d",
            "PBR reported net biomass production rate", reported(tag, "net_biomass_production"))

        def projected(unit: dict[str, Any] = unit) -> tuple[float | None, str | None, list[str]]:
            params = unit.get("params") or {}
            geometry = {key: _number((params.get(key) or {}).get("si"))
                        for key in ("tube_count", "tube_inner_diameter", "tube_length")}
            absent = [key for key, value in geometry.items() if value is None]
            if absent:
                return None, "missing PBR geometry: " + ", ".join(absent), []
            area = geometry["tube_count"] * geometry["tube_inner_diameter"] * geometry["tube_length"]  # type: ignore[operator]
            production = _number(((run_units.get(unit["tag"]) or {}).get("reported") or {})
                                 .get("net_biomass_production", {}).get("value"))
            if production is None:
                return None, "net biomass production is not reported", []
            if area <= 0:
                return None, "projected tube area is zero", []
            return production / area, None, [_pointer("units", unit["tag"], "reported", "net_biomass_production")]

        add(f"pbr_projected_area_productivity:{tag}",
            f"{tag} productivity per projected tube area (not ground footprint)", "kg/(m²·d)",
            "net biomass production ÷ A_proj, A_proj = n_tubes · D_inner · L: the projected tube area normal to a "
            "beam perpendicular to the tube axis; not ground footprint", projected)
        add(f"culture_throughput:{tag}", f"{tag} culture throughput", "m³/h",
            "PBR reported carrier volumetric flow Q", reported(tag, "volumetric_flow_m3_h"))

    for stream in products:
        def concentration(stream: dict[str, Any] = stream) -> tuple[float | None, str | None, list[str]]:
            item = culture.get(stream["tag"])
            if not item:
                return None, "the stream carries no culture values", []
            value = (item.get("values") or {}).get("biomass") or {}
            shown = value.get("display")
            if not shown or _number(shown.get("value")) is None:
                return None, value.get("reason") or "culture biomass is not known on this stream", []
            return shown["value"], None, [_pointer("culture", stream["tag"], "values", "biomass")]

        add(f"outlet_biomass_concentration:{stream['tag']}", f"{stream['tag']} biomass concentration", "kg/m³",
            "culture biomass concentration of the product stream", concentration)

    for unit in separators:
        def recovery(unit: dict[str, Any] = unit) -> tuple[float | None, str | None, list[str]]:
            inlet = next((item for item in streams if (item.get("target") or {}).get("unit") == unit["id"]
                          and item["target"].get("port") == 0), None)
            concentrate = next((item for item in streams if (item.get("source") or {}).get("unit") == unit["id"]
                                and item["source"].get("port") == 0), None)
            if inlet is None or concentrate is None:
                return None, "the separator inlet or concentrate stream is not connected", []
            rate_in, reason_in = _biomass_rate(culture.get(inlet["tag"]), run_streams.get(inlet["tag"]))
            rate_out, reason_out = _biomass_rate(culture.get(concentrate["tag"]), run_streams.get(concentrate["tag"]))
            if rate_in is None or rate_out is None:
                return None, reason_in or reason_out, []
            if rate_in <= 0:
                return None, "no biomass reaches the separator inlet", []
            return rate_out / rate_in, None, [_pointer("culture", tag, "values", "biomass")
                                              for tag in (inlet["tag"], concentrate["tag"])]

        add(f"separator_biomass_recovery:{unit['tag']}", f"{unit['tag']} biomass recovery", "1",
            "biomass mass rate in the concentrate outlet ÷ biomass mass rate at the inlet, from solved stream culture",
            recovery)

    def water() -> tuple[float | None, str | None, list[str]]:
        if "Water" not in document.get("compounds", []):
            return None, "Water is not a draft compound", []
        total = 0.0
        for item in feeds:
            result = run_streams.get(item["tag"]) or {}
            flow, fraction = _number(result.get("mass_flow_kg_s")), _number((result.get("mass_fractions") or {}).get("Water"))
            if flow is None or fraction is None:
                return None, f"no mass flow or Water fraction for feed {item['tag']}", []
            total += flow * fraction * 3600.0
        return total, None, [_pointer("streams", item["tag"], "mass_fractions", "Water") for item in feeds]

    add("water_input", "Water input", "kg/h", "Σ over feed streams of mass flow × Water mass fraction", water)

    def nitrogen() -> tuple[float | None, str | None, list[str]]:
        carrying = [item for item in feeds if culture.get(item["tag"])]
        if not carrying:
            return None, "no feed carries culture nitrogen", []
        total = 0.0
        for item in carrying:
            rate, reason = _biomass_rate(culture.get(item["tag"]), run_streams.get(item["tag"]), "nitrogen")
            if rate is None:
                return None, f"{item['tag']}: {reason}", []
            total += rate
        return total, None, [_pointer("culture", item["tag"], "values", "nitrogen") for item in carrying]

    add("nitrogen_input", "Nitrogen input", "kg/d",
        "Σ over feed streams of culture nitrogen (kg/m³) × volumetric flow (m³/d)", nitrogen)

    def closure() -> tuple[float | None, str | None, list[str]]:
        carrier = ((run.get("mixed_solve") or {}).get("balances") or {}).get("carrier_mass")
        if isinstance(carrier, dict):
            residual, boundary = _number(carrier.get("residual")), _number(carrier.get("in"))
            source = _pointer("mixed_solve", "balances", "carrier_mass")
        else:
            balance = run.get("mass_balance") or {}
            if balance.get("status") != "calculated":
                return None, "the mass balance was not calculated", []
            residual = _number(balance.get("residual_kg_s"))
            inflows = [_number(value) for value in (balance.get("boundary_kg_s") or {}).values()]
            boundary = sum(value for value in inflows if value is not None and value > 0) if inflows else None
            source = _pointer("mass_balance")
        if residual is None or boundary is None or boundary <= 0:
            return None, "the mass balance was not calculated", []
        return abs(residual) / boundary, None, [source]

    add("mass_balance_closure", "Mass balance closure", "1", "|residual| ÷ boundary mass flow in", closure)
    for kpi_id, label, kpi_unit in (("co2_feed", "CO₂ feed", "kg/d"), ("co2_uptake", "CO₂ uptake", "kg/d"),
                                    ("co2_per_biomass", "CO₂ per biomass", "kg/kg")):
        rows.append(_kpi(kpi_id, label, kpi_unit, "not modelled in T1", status="unavailable", reason=NO_CARBON_REASON))
    return rows
