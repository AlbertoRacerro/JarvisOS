"""Instant, Jarvis-side Photobioreactor (T1) findings that need more than the culture graph (spec 170 cap. 6).

Culture reachability and the biomass / nitrogen / oxygen requirements live with the other culture findings in
``culture.py``. This module owns the model-card, temperature and feed-basis HRT findings.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from app.modules.process_stack.draft_models import pbr_temperature_invalid, spec_for

HRT_MIN_DAYS = 0.1
HRT_MAX_DAYS = 100.0
TEMPERATURE_DIFFERS_K = 5.0
_STANDARD_TEMPERATURE_K = 298.15


def _finding(severity: str, code: str, tag: str, field: str, message: str) -> dict[str, Any]:
    return {"severity": severity, "code": code, "object": tag, "field": field, "message": message,
            "source": "jarvis"}


@lru_cache(maxsize=256)
def _water_density(temperature_k: float, pressure_pa: float) -> float | None:
    """Pre-run density at the feed state.

    The 167 culture basis is the solved DWSIM Mixture density under its seawater-as-water approximation, and a
    solved density does not exist before Run. For a feed stream that approximation is pure water at the feed T/P,
    so the CoolProp water density below is that basis evaluated without a DWSIM solve (and the finding labels it).
    """
    try:
        from CoolProp.CoolProp import PropsSI

        value = float(PropsSI("D", "T", temperature_k, "P", pressure_pa, "Water"))
    except Exception:  # noqa: BLE001 - an unavailable or out-of-range basis skips the screening check
        return None
    return value if math.isfinite(value) and value > 0 else None


def _model_findings(workspace_id: str | None, unit: dict[str, Any]) -> list[dict[str, Any]]:
    tag, pin = unit["tag"], unit.get("model") or None
    if pin is None:
        return [_finding("blocker", "PBR_REQUIRES_MODEL_CARD", tag, "model",
                         f"{tag} needs a pinned biological model card; choose one in the Biology tab.")]
    if workspace_id is None:
        return []  # without a workspace the model library cannot be consulted
    from app.modules.bio_models import service as bio_models

    try:
        return [{"object": tag, "field": "model", "source": "jarvis", **item}
                for item in bio_models.pbr_model_findings(workspace_id, tag, pin)]
    except Exception:  # noqa: BLE001 - a broken library must not break instant validation
        return [_finding("blocker", "PBR_MODEL_CARD_UNAVAILABLE", tag, "model",
                         "The pinned model card could not be resolved; re-pin a card in the Biology tab.")]


def _feed_flow_kg_s(document: dict[str, Any], feed: dict[str, Any]) -> float | None:
    from app.modules.process_stack import mixed_runtime

    try:
        spec = feed["spec"]
        fractions = mixed_runtime._mass_fractions_from_spec(spec, document["compounds"])
        flow = mixed_runtime._feed_mass_flow(spec, fractions)
    except Exception:  # noqa: BLE001 - an unreadable feed flow skips the screening check
        return None
    return flow if math.isfinite(flow) and flow > 0 else None


def _feed_basis(document: dict[str, Any], unit: dict[str, Any], inlet: dict[str, Any]) -> dict[str, float] | None:
    """Volume, flow and HRT of a PBR whose inlet is a feed stream; None when any input is missing."""
    spec = inlet.get("spec") or {}
    params = unit["params"]
    dims = [(params.get(key) or {}).get("si") for key in ("tube_inner_diameter", "tube_length", "tube_count")]
    temperature = (spec.get("temperature") or {}).get("si")
    pressure = (spec.get("pressure") or {}).get("si")
    if not (all(isinstance(value, (int, float)) and value > 0 for value in dims)
            and isinstance(temperature, (int, float)) and isinstance(pressure, (int, float))):
        return None
    flow = _feed_flow_kg_s(document, inlet)
    density = _water_density(float(temperature), float(pressure)) if flow is not None else None
    if flow is None or density is None:
        return None
    diameter, length, count = (float(value) for value in dims if value is not None)
    volume = count * math.pi / 4.0 * diameter ** 2 * length
    return {"volume_m3": volume, "mass_flow_kg_s": flow, "density_kg_m3": density,
            "volume_flow_m3_h": flow / density * 3600.0, "hrt_d": volume * density / flow / 86400.0}


def pbr_feed_basis(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pre-run Q and HRT per PBR tag, only for PBRs fed directly by a feed stream (Overview, labelled feed basis)."""
    objects = document["objects"]
    output: dict[str, dict[str, Any]] = {}
    for unit in objects.values():
        if unit["kind"] != "unit" or spec_for(unit).culture_rule != "pbr":
            continue
        inlet = next((item for item in objects.values() if item["kind"] == "stream" and item["type"] != "EnergyStream"
                      and (item.get("target") or {}).get("unit") == unit["id"]), None)
        if inlet is None or inlet.get("source") is not None:
            continue
        basis = _feed_basis(document, unit, inlet)
        if basis is not None:
            output[unit["tag"]] = {**basis, "basis": "feed"}
    return output


def pbr_findings(document: dict[str, Any], workspace_id: str | None = None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    objects = document["objects"]
    for unit in sorted((item for item in objects.values() if item["kind"] == "unit"
                        and spec_for(item).culture_rule == "pbr"), key=lambda item: item["tag"]):
        tag, params = unit["tag"], unit["params"]
        if pbr_temperature_invalid(params):
            findings.append(_finding("blocker", "PBR_TEMPERATURE_INVALID", tag, "temperature_amplitude",
                                     "The mean culture temperature minus the diel amplitude must stay above 0 K."))
        findings.extend(_model_findings(workspace_id, unit))
        inlet = next((item for item in objects.values() if item["kind"] == "stream" and item["type"] != "EnergyStream"
                      and (item.get("target") or {}).get("unit") == unit["id"]), None)
        if inlet is None or inlet.get("source") is not None:
            # From a unit or a recycle the solved inlet is only known after Run; the instant check is skipped.
            continue
        spec = inlet.get("spec") or {}
        temperature = (spec.get("temperature") or {}).get("si")
        mean = (params.get("temperature_mean") or {}).get("si")
        if isinstance(temperature, (int, float)) and isinstance(mean, (int, float)) and (
                abs(mean - temperature) > TEMPERATURE_DIFFERS_K):
            findings.append(_finding(
                "info", "PBR_TEMPERATURE_DECLARED_DIFFERS", tag, "temperature_mean",
                f"The declared mean culture temperature differs from the feed temperature by more than "
                f"{TEMPERATURE_DIFFERS_K:g} K. Tier 1 has no energy balance: growth uses the declared profile "
                "and the outlet temperature equals the inlet temperature."))
        basis = _feed_basis(document, unit, inlet)
        if basis is None:
            continue
        if not HRT_MIN_DAYS <= basis["hrt_d"] <= HRT_MAX_DAYS:
            findings.append(_finding(
                "warning", "PBR_HRT_OUT_OF_RANGE", tag, "tube_length",
                f"Feed basis: HRT is {basis['hrt_d']:.3g} d (volume {basis['volume_m3']:.3g} m3, feed "
                f"{basis['mass_flow_kg_s']:.3g} kg/s, density {basis['density_kg_m3']:.0f} kg/m3, the 167 "
                f"seawater-as-water basis at the feed state), outside {HRT_MIN_DAYS:g}-{HRT_MAX_DAYS:g} d. "
                "A solved basis is reported after Run."))
    return findings
