"""Spec 190 recycle fixture: three identical sandboxed mixer-splitters in a loop with a closed-form answer.

Each unit mixes inlets `a` and `b` and sends the share `forward` (percent) of the mix to `fwd`, the rest to `purge`.
Fresh feed F and the recycle R enter a DWSIM Mixer; Mix -> U1.a; U1.fwd -> U2.a; U2.fwd -> U3.a; U3.fwd -> Recycle
block -> Mixer (the 183 tear). Each unit's `b` inlet takes a side feed S1..S3 (a unit needs every declared inlet).
With f = forward/100: M1 = F + R + S1, M2 = f*M1 + S2, M3 = f*M2 + S3, R = f*M3, hence
R = (f^3*(F + S1) + f^2*S2 + f*S3) / (1 - f^3) and Purge_i = (1-f)*M_i. All feeds carry the same culture
concentration and splitting does not change composition, so every stream of the loop has that concentration. The
purges sum to F + S1 + S2 + S3, closing the whole-graph balance.
"""

from __future__ import annotations

from typing import Any

from app.modules.process_stack.draft_models import (
    AddStream,
    AddUnit,
    Connect,
    DraftQuantity,
    SetStreamCulture,
    SetStreamSpec,
    SetThermo,
    SetUnitParams,
)

Q = DraftQuantity

LOOP_CONTRACT = {
    "category": "process_unit", "name": "Mixer splitter",
    "engineering_question": "What recycle flow does the loop settle at?",
    "inlets": [{"name": "a", "label": "Inlet A"}, {"name": "b", "label": "Inlet B"}],
    "outlets": [{"name": "fwd", "label": "Forward"}, {"name": "purge", "label": "Purge"}],
    "parameters": [{"name": "forward", "label": "Forward share", "unit": "percent",
                    "description": "Share of the mixed stream sent forward", "min": 0, "max": 100}],
    "culture_generation": [],
}

LOOP_BODY = '''    a, b = inlets["a"], inlets["b"]
    share = params["forward"] / 100.0
    ma, mb = a["mass_flow_kg_s"], b["mass_flow_kg_s"]
    m = ma + mb
    names = set(a["mass_fractions"]) | set(b["mass_fractions"])
    fractions = {n: (ma * a["mass_fractions"].get(n, 0.0) + mb * b["mass_fractions"].get(n, 0.0)) / m for n in names}
    total = sum(fractions.values())
    fractions = {n: v / total for n, v in fractions.items()}
    culture = {}
    for field in set(a["culture"]) | set(b["culture"]):
        x, y = a["culture"].get(field), b["culture"].get(field)
        culture[field] = None if (x is None or y is None or field == "ph") else (ma * x + mb * y) / m
    base = {"temperature_K": (ma * a["temperature_K"] + mb * b["temperature_K"]) / m,
            "pressure_Pa": min(a["pressure_Pa"], b["pressure_Pa"]), "mass_fractions": fractions, "culture": culture}
    return {"outlets": {"fwd": {**base, "mass_flow_kg_s": share * m}, "purge": {**base, "mass_flow_kg_s": (1 - share) * m}}}'''

LOOP_CASE = {
    "inlets": {"a": {"mass_flow_kg_s": 1.0, "temperature_K": 300.0, "pressure_Pa": 200000.0,
                     "mass_fractions": {"Water": 1.0}, "culture": {"biomass": 0.5, "nitrogen": 0.05}},
               "b": {"mass_flow_kg_s": 0.5, "temperature_K": 300.0, "pressure_Pa": 200000.0,
                     "mass_fractions": {"Water": 1.0}, "culture": {"biomass": 0.5, "nitrogen": 0.05}}},
    "params": {"forward": 50.0},
}

FEED_KG_S = 0.02
SIDE_KG_S = 0.005
FEED_NITROGEN = 0.05
FEED_BIOMASS = 0.5
UNIT_TAGS = ("Loop1", "Loop2", "Loop3")


def _feed(sid: str, tag: str, x: int, y: int, flow: float) -> list[Any]:
    return [
        AddStream(op="add_stream", id=sid, tag=tag, x=x, y=y),
        SetStreamSpec(op="set_stream_spec", stream=sid, pressure=Q(value=2.0, unit="bar"),
                      temperature=Q(value=300.0, unit="K"), mass_flow=Q(value=flow, unit="kg/s"),
                      composition={"Water": 1.0}, composition_basis="mass"),
        SetStreamCulture(op="set_stream_culture", stream=sid, culture={
            "biomass": Q(value=FEED_BIOMASS, unit="kg/m3"), "nitrogen": Q(value=FEED_NITROGEN, unit="kg/m3"),
            "salinity": Q(value=35.0, unit="g/kg")}),
    ]


def loop_ops(version_id: str, *, forward: float = 50.0) -> list[Any]:
    ids = ("u1", "u2", "u3")
    ops: list[Any] = [SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
                      AddUnit(op="add_unit", id="mix", type="Mixer", tag="Mixer", x=100, y=0),
                      AddUnit(op="add_unit", id="rec", type="Recycle", tag="Rec", x=400, y=-200)]
    for i, uid in enumerate(ids, start=1):
        ops += [AddUnit(op="add_unit", id=uid, type="StudioUnit", tag=f"Loop{i}", x=200 * i + 100, y=0,
                        version_id=version_id),
                SetUnitParams(op="set_unit_params", unit=uid, values={"forward": Q(value=forward, unit="percent")}),
                AddStream(op="add_stream", id=f"purge{i}", tag=f"Purge{i}", x=200 * i + 100, y=100),
                Connect(op="connect", stream=f"purge{i}", end="source", unit=uid, port=1),
                *_feed(f"side{i}", f"Side{i}", 200 * i + 100, -100, SIDE_KG_S),
                Connect(op="connect", stream=f"side{i}", end="target", unit=uid, port=1)]
    ops += [*_feed("fresh", "Fresh", 0, 0, FEED_KG_S), Connect(op="connect", stream="fresh", end="target", unit="mix", port=0),
            AddStream(op="add_stream", id="mixed", tag="Mixed", x=150, y=0),
            Connect(op="connect", stream="mixed", end="source", unit="mix", port=0),
            Connect(op="connect", stream="mixed", end="target", unit="u1", port=0)]
    for i in (1, 2):
        ops += [AddStream(op="add_stream", id=f"link{i}", tag=f"Link{i}", x=200 * i + 200, y=0),
                Connect(op="connect", stream=f"link{i}", end="source", unit=ids[i - 1], port=0),
                Connect(op="connect", stream=f"link{i}", end="target", unit=ids[i], port=0)]
    ops += [AddStream(op="add_stream", id="recycle", tag="Recycle", x=700, y=-100),
            AddStream(op="add_stream", id="back", tag="Return", x=100, y=-100),
            Connect(op="connect", stream="recycle", end="source", unit="u3", port=0),
            Connect(op="connect", stream="recycle", end="target", unit="rec", port=0),
            Connect(op="connect", stream="back", end="source", unit="rec", port=0),
            Connect(op="connect", stream="back", end="target", unit="mix", port=1)]
    return ops


def closed_form(forward: float = 50.0) -> dict[str, float]:
    f, F, S = forward / 100.0, FEED_KG_S, SIDE_KG_S
    r = (f ** 3 * (F + S) + f * f * S + f * S) / (1.0 - f ** 3)
    m1 = F + r + S
    m2 = f * m1 + S
    m3 = f * m2 + S
    return {"Mixed": F + r, "Link1": f * m1, "Link2": f * m2, "Recycle": r, "Return": r,
            "Purge1": (1 - f) * m1, "Purge2": (1 - f) * m2, "Purge3": (1 - f) * m3}
