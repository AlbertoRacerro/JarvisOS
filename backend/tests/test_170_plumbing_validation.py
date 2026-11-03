"""Spec 170 capability 6: instant Photobioreactor findings."""

from __future__ import annotations

import math

import pytest

from app.modules.bio_models import service as bio_models
from app.modules.process_stack import culture, draft, pbr_validation
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
from tests.plumbing_170_support import PBR_VALUES, PIN, codes, find, pbr_document, pbr_quantities

FULL = {"biomass": (0.0, "kg/m3"), "nitrogen": (0.05, "kg/m3"), "oxygen": (0.0, "kg/m3"), "salinity": (35.0, "g/kg")}
REQUIRES = {"PBR_REQUIRES_CULTURE_INLET", "PBR_REQUIRES_BIOMASS", "PBR_REQUIRES_NITROGEN", "PBR_REQUIRES_OXYGEN"}


def _without(name: str) -> dict:
    return {key: value for key, value in FULL.items() if key != name}


def test_explicit_zero_biomass_and_oxygen_are_valid_inputs() -> None:
    findings = draft.validate_document(pbr_document(culture={**FULL, "nitrogen": (0.0, "kg/m3")}))
    assert not codes(findings) & REQUIRES
    assert not [item for item in findings if item["severity"] == "blocker" and item["code"].startswith("PBR_")
                and item["code"] != "PBR_REQUIRES_MODEL_CARD"]


@pytest.mark.parametrize("missing,code", [("biomass", "PBR_REQUIRES_BIOMASS"), ("nitrogen", "PBR_REQUIRES_NITROGEN"),
                                          ("oxygen", "PBR_REQUIRES_OXYGEN")])
def test_absent_culture_field_blocks_only_that_field(missing: str, code: str) -> None:
    findings = draft.validate_document(pbr_document(culture=_without(missing)))
    hits = find(findings, code)
    assert len(hits) == 1 and hits[0]["severity"] == "blocker" and hits[0]["object"] == "PBR"
    assert hits[0]["field"] == missing and hits[0]["source"] == "jarvis"
    assert (codes(findings) & REQUIRES) - {"PBR_REQUIRES_CULTURE_INLET"} == {code}
    assert "zero is valid" in hits[0]["message"]


def test_inlet_without_culture_is_blocked() -> None:
    findings = draft.validate_document(pbr_document(culture={}))
    assert "PBR_REQUIRES_CULTURE_INLET" in codes(findings)
    assert not codes(findings) & {"PBR_REQUIRES_BIOMASS", "PBR_REQUIRES_NITROGEN", "PBR_REQUIRES_OXYGEN"}


def _two_feed_mixer(second: dict) -> dict:
    ops = [
        AddStream(op="add_stream", id="f1", tag="F1", x=0, y=0), AddStream(op="add_stream", id="f2", tag="F2", x=0, y=50),
        AddUnit(op="add_unit", id="m", type="Mixer", tag="M", x=100, y=0),
        AddUnit(op="add_unit", id="pbr", type="PhotobioreactorT1", tag="PBR", x=200, y=0),
        AddStream(op="add_stream", id="mid", tag="Mid", x=150, y=0), AddStream(op="add_stream", id="out", tag="Out", x=300, y=0),
        Connect(op="connect", stream="f1", end="target", unit="m", port=0),
        Connect(op="connect", stream="f2", end="target", unit="m", port=1),
        Connect(op="connect", stream="mid", end="source", unit="m", port=0),
        Connect(op="connect", stream="mid", end="target", unit="pbr", port=0),
        Connect(op="connect", stream="out", end="source", unit="pbr", port=0),
    ]
    for stream, culture_values in (("f1", FULL), ("f2", second)):
        ops.append(SetStreamCulture(op="set_stream_culture", stream=stream, culture={
            name: DraftQuantity(value=value, unit=unit) for name, (value, unit) in culture_values.items()}))
    return draft.apply_ops(draft.empty_document("mix"), ops)


def test_unknown_field_propagated_through_a_mixer_blocks_the_pbr_inlet() -> None:
    # F2 does not specify nitrogen, so the mixed stream's nitrogen is unknown (167), not zero.
    findings = culture.culture_findings(_two_feed_mixer(_without("nitrogen")))
    assert [item["field"] for item in find(findings, "PBR_REQUIRES_NITROGEN")] == ["nitrogen"]
    assert not find(findings, "PBR_REQUIRES_BIOMASS") and not find(findings, "PBR_REQUIRES_OXYGEN")
    assert not codes(culture.culture_findings(_two_feed_mixer(FULL))) & REQUIRES


def test_non_culture_mixer_inlet_dilutes_as_zero_and_stays_known() -> None:
    document = _two_feed_mixer(FULL)
    document["objects"]["f2"]["spec"].pop("culture")  # F2 becomes a plain medium feed
    assert not codes(culture.culture_findings(document)) & REQUIRES


def test_consumed_tear_inherits_only_fields_every_culture_feed_specifies() -> None:
    ops = [
        AddStream(op="add_stream", id="feed", tag="Feed", x=0, y=0),
        AddUnit(op="add_unit", id="m", type="Mixer", tag="M", x=100, y=0),
        AddUnit(op="add_unit", id="pbr", type="PhotobioreactorT1", tag="PBR", x=200, y=0),
        AddUnit(op="add_unit", id="s", type="Splitter", tag="S", x=300, y=0),
        AddUnit(op="add_unit", id="r", type="Recycle", tag="R", x=300, y=100),
        AddStream(op="add_stream", id="a", tag="A", x=150, y=0), AddStream(op="add_stream", id="b", tag="B", x=250, y=0),
        AddStream(op="add_stream", id="prod", tag="Prod", x=350, y=0), AddStream(op="add_stream", id="ret", tag="Ret", x=300, y=50),
        AddStream(op="add_stream", id="tear", tag="Tear", x=200, y=100),
        Connect(op="connect", stream="feed", end="target", unit="m", port=0),
        Connect(op="connect", stream="a", end="source", unit="m", port=0), Connect(op="connect", stream="a", end="target", unit="pbr", port=0),
        Connect(op="connect", stream="b", end="source", unit="pbr", port=0), Connect(op="connect", stream="b", end="target", unit="s", port=0),
        Connect(op="connect", stream="prod", end="source", unit="s", port=0),
        Connect(op="connect", stream="ret", end="source", unit="s", port=1), Connect(op="connect", stream="ret", end="target", unit="r", port=0),
        Connect(op="connect", stream="tear", end="source", unit="r", port=0), Connect(op="connect", stream="tear", end="target", unit="m", port=1),
    ]
    for values, expected in ((FULL, set()), (_without("oxygen"), {"PBR_REQUIRES_OXYGEN"})):
        document = draft.apply_ops(draft.empty_document("loop"), [
            *ops, SetStreamCulture(op="set_stream_culture", stream="feed", culture={
                name: DraftQuantity(value=value, unit=unit) for name, (value, unit) in values.items()})])
        assert codes(culture.culture_findings(document)) & REQUIRES == expected


def test_model_card_blocker_without_a_pin_needs_no_library() -> None:
    findings = draft.validate_document(pbr_document(), "ws-1")
    hit = find(findings, "PBR_REQUIRES_MODEL_CARD")
    assert len(hit) == 1 and hit[0]["severity"] == "blocker" and hit[0]["field"] == "model"


def test_model_findings_come_from_bio_models_and_receive_workspace_tag_and_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    returned = [
        {"severity": "blocker", "code": "PBR_MODEL_CARD_SYMBOL_MISSING", "message": "Missing k_X.", "object": "PBR",
         "field": "model", "source": "jarvis"},
        {"severity": "info", "code": "PBR_PARAMETER_UNVERIFIED", "message": "Candidate: mu_max.", "object": "PBR",
         "field": "model", "source": "jarvis"},
    ]

    def fake(workspace_id, unit_tag, pin):
        calls.append((workspace_id, unit_tag, pin))
        return list(returned)

    monkeypatch.setattr(bio_models, "pbr_model_findings", fake, raising=False)
    document = pbr_document(model=True)
    findings = draft.validate_document(document, "ws-1")
    assert calls == [("ws-1", "PBR", PIN)]
    assert {"PBR_MODEL_CARD_SYMBOL_MISSING", "PBR_PARAMETER_UNVERIFIED"} <= codes(findings)
    assert "PBR_REQUIRES_MODEL_CARD" not in codes(findings)
    # Without a workspace the library cannot be consulted and the call is skipped.
    calls.clear()
    draft.validate_document(document)
    assert calls == []


def test_a_failing_model_library_degrades_to_one_unavailable_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args):
        raise RuntimeError("library offline")

    monkeypatch.setattr(bio_models, "pbr_model_findings", boom, raising=False)
    findings = draft.validate_document(pbr_document(model=True), "ws-1")
    assert [item["severity"] for item in find(findings, "PBR_MODEL_CARD_UNAVAILABLE")] == ["blocker"]


def _hrt(findings: list[dict]) -> list[dict]:
    return find(findings, "PBR_HRT_OUT_OF_RANGE")


def _volume_m3() -> float:
    d, length, count = (PBR_VALUES[k][0] for k in ("tube_inner_diameter", "tube_length", "tube_count"))
    return count * math.pi / 4 * d**2 * length


def test_feed_basis_hrt_uses_the_feed_flow_and_pre_run_water_density_not_1000() -> None:
    density = pbr_validation._water_density(298.15, 100000.0)
    assert density is not None and 990 < density < 1000 and density != 1000.0
    for flow, expect in ((1.0, True), (0.02, False), (1e-6, True)):
        findings = draft.validate_document(pbr_document(flow_kg_s=flow))
        hrt_days = _volume_m3() * density / flow / 86400
        assert bool(_hrt(findings)) == (not 0.1 <= hrt_days <= 100) == expect, hrt_days
        if expect:
            hit = _hrt(findings)[0]
            assert hit["severity"] == "warning" and hit["message"].startswith("Feed basis:")
            assert f"{hrt_days:.3g} d" in hit["message"] and f"{density:.0f} kg/m3" in hit["message"]


def test_feed_basis_hrt_boundaries_are_inclusive() -> None:
    density = pbr_validation._water_density(298.15, 100000.0)
    assert density is not None
    for hrt, nudge in ((0.1, 1 - 1e-9), (100.0, 1 + 1e-9)):  # just inside each end of [0.1, 100] d
        flow = _volume_m3() * density / (hrt * 86400) * nudge
        assert not _hrt(draft.validate_document(pbr_document(flow_kg_s=flow)))
    for hrt, nudge in ((0.1, 1 + 1e-6), (100.0, 1 - 1e-6)):  # just outside
        flow = _volume_m3() * density / (hrt * 86400) * nudge
        assert _hrt(draft.validate_document(pbr_document(flow_kg_s=flow)))


def test_hrt_and_temperature_checks_are_skipped_when_the_inlet_is_not_a_feed() -> None:
    ops = [
        SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
        AddStream(op="add_stream", id="feed", tag="Feed", x=0, y=0),
        AddUnit(op="add_unit", id="h", type="Heater", tag="H", x=50, y=0),
        AddUnit(op="add_unit", id="pbr", type="PhotobioreactorT1", tag="PBR", x=100, y=0),
        AddStream(op="add_stream", id="mid", tag="Mid", x=75, y=0), AddStream(op="add_stream", id="out", tag="Out", x=150, y=0),
        Connect(op="connect", stream="feed", end="target", unit="h", port=0),
        Connect(op="connect", stream="mid", end="source", unit="h", port=0),
        Connect(op="connect", stream="mid", end="target", unit="pbr", port=0),
        Connect(op="connect", stream="out", end="source", unit="pbr", port=0),
        SetStreamSpec(op="set_stream_spec", stream="feed", pressure=DraftQuantity(value=1, unit="bar"),
                      temperature=DraftQuantity(value=280, unit="K"), mass_flow=DraftQuantity(value=1e-6, unit="kg/s"),
                      composition={"Water": 1.0}, composition_basis="mass"),
        SetStreamCulture(op="set_stream_culture", stream="feed", culture={
            name: DraftQuantity(value=value, unit=unit) for name, (value, unit) in FULL.items()}),
        SetUnitParams(op="set_unit_params", unit="pbr", values=pbr_quantities()),
    ]
    document = draft.apply_ops(draft.empty_document("h"), ops)
    findings = draft.validate_document(document)
    assert not _hrt(findings) and not find(findings, "PBR_TEMPERATURE_DECLARED_DIFFERS")
    assert pbr_validation.pbr_feed_basis(document) == {}


def test_feed_basis_projection_gives_q_and_hrt_on_the_same_basis_as_the_finding() -> None:
    density = pbr_validation._water_density(298.15, 100000.0)
    assert density is not None
    basis = pbr_validation.pbr_feed_basis(pbr_document(flow_kg_s=0.5))["PBR"]
    assert basis["basis"] == "feed" and basis["density_kg_m3"] == density
    assert basis["volume_m3"] == pytest.approx(_volume_m3())
    assert basis["volume_flow_m3_h"] == pytest.approx(0.5 / density * 3600)
    assert basis["hrt_d"] == pytest.approx(_volume_m3() * density / 0.5 / 86400)
    assert pbr_validation.pbr_feed_basis(pbr_document(params=False)) == {}


def test_declared_temperature_difference_is_info_only_above_five_kelvin() -> None:
    for feed_k, expected in ((298.15, False), (303.0, False), (303.2, True), (293.0, True)):
        findings = draft.validate_document(pbr_document(feed_temperature_k=feed_k, flow_kg_s=0.02))
        hits = find(findings, "PBR_TEMPERATURE_DECLARED_DIFFERS")
        assert bool(hits) == expected, feed_k
        if hits:
            assert hits[0]["severity"] == "info" and hits[0]["field"] == "temperature_mean"
