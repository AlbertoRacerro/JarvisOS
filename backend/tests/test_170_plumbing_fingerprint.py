"""Spec 170 capability 7: the 168 layout-free fingerprint with a Photobioreactor."""

from __future__ import annotations

from typing import Any

import pytest

from app.modules.bio_models import service as bio_models
from app.modules.process_stack import draft, mixed, pbr_unit
from app.modules.process_stack.draft_models import DraftQuantity, Move, SetUnitModel, SetUnitParams
from tests.plumbing_170_support import OTHER_PIN_FOR_TESTS as OTHER_PIN
from tests.plumbing_170_support import pbr_document
from tests.test_process_mixed_168 import _stream, _tear_document, _unit

# Computed once with the pre-170 mixed.fingerprint (master e47e94cd); drafts without a PBR must not move.
GOLDEN_TEAR = "sha256:6660cf73693dc89414770e9c9186a0c2fae171864ad96134e3acc2d9d8682538"
GOLDEN_SEPARATOR = "sha256:91acda9421c0699c82969c6fbee969b1dd5d3fb5e0675b00f7678fb5f3c78a96"


def _separator_document() -> dict[str, Any]:
    return {"schema_version": 1, "name": "sep", "compounds": ["Water"], "property_package": "NRTL", "reactions": {},
            "objects": {"feed": _stream("feed", "Feed", target="j"), "j": _unit("j", "Sep", "SpecifiedSeparator"),
                        "c": _stream("c", "C", "j", None, 0), "k": _stream("k", "K", "j", None, 1)}}


def _fp(document: dict[str, Any], workspace_id: str | None = "ws") -> str:
    return mixed.fingerprint(document, mixed.partition(document), workspace_id)


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"digest": "s" * 64, "calls": [], "fail": False}

    def fake(workspace_id: str, card_id: str, revision: str, digest: str) -> dict[str, Any]:
        state["calls"].append((workspace_id, card_id, revision, digest))
        if state["fail"]:
            raise bio_models.BioModelError("PBR_MODEL_CARD_UNAVAILABLE", "gone", 422)
        return {"set": {"digest": state["digest"]}}

    monkeypatch.setattr(bio_models, "resolve_growth_model", fake, raising=False)
    return state


def test_documents_without_a_pbr_keep_their_golden_fingerprints(resolver: dict[str, Any]) -> None:
    assert _fp(_tear_document()) == GOLDEN_TEAR
    assert _fp(_separator_document()) == GOLDEN_SEPARATOR
    assert _fp(_separator_document(), None) == GOLDEN_SEPARATOR
    assert resolver["calls"] == [], "a draft without a PBR never resolves a model"


def test_fingerprint_binds_params_pin_set_digest_and_evaluator(resolver: dict[str, Any],
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    document = pbr_document(model=True)
    base = _fp(document)
    assert base == _fp(pbr_document(model=True)), "deterministic"
    assert resolver["calls"][0][0] == "ws" and resolver["calls"][0][1:] == (
        document["objects"]["pbr"]["model"]["card_id"], document["objects"]["pbr"]["model"]["card_revision"],
        document["objects"]["pbr"]["model"]["card_digest"])

    edited = draft.apply_ops(document, [SetUnitParams(op="set_unit_params", unit="pbr", values={
        "tube_length": DraftQuantity(value=101, unit="m")})])
    assert _fp(edited) != base, "a PBR parameter edit stales results"

    repinned = draft.apply_ops(document, [SetUnitModel(op="set_unit_model", unit="pbr", model=OTHER_PIN)])  # type: ignore[arg-type]
    assert _fp(repinned) != base, "re-pinning stales results"

    resolver["digest"] = "t" * 64
    assert _fp(document) != base, "a different resolved set digest under the same pin changes the fingerprint"
    resolver["digest"] = "s" * 64
    assert _fp(document) == base

    monkeypatch.setattr(pbr_unit, "MODEL_VERSION", "pbr_unit.v2")
    assert _fp(document) != base, "the evaluator model version is part of the fingerprint"
    monkeypatch.setattr(pbr_unit, "MODEL_VERSION", "pbr_unit.v1")
    monkeypatch.setattr(pbr_unit, "EVALUATOR_ID", "jarvis.other")
    assert _fp(document) != base, "the evaluator id is part of the fingerprint"


def test_layout_moves_do_not_change_the_pbr_fingerprint(resolver: dict[str, Any]) -> None:
    document = pbr_document(model=True)
    moved = draft.apply_ops(document, [Move(op="move", id="pbr", x=900, y=-40), Move(op="move", id="feed", x=-5, y=5)])
    assert _fp(moved) == _fp(document)


def test_unresolvable_pin_uses_a_stable_sentinel_that_still_changes_on_repin(resolver: dict[str, Any]) -> None:
    document = pbr_document(model=True)
    resolved = _fp(document)
    resolver["fail"] = True
    unresolved = _fp(document)
    assert unresolved == _fp(document) and unresolved != resolved
    repinned = draft.apply_ops(document, [SetUnitModel(op="set_unit_model", unit="pbr", model=OTHER_PIN)])  # type: ignore[arg-type]
    assert _fp(repinned) != unresolved, "the pin itself is in the document, so a re-pin changes it even when unresolved"
    assert _fp(document, None) == unresolved, "no workspace resolves to the same sentinel"
    unpinned = pbr_document()
    assert _fp(unpinned) == _fp(unpinned, None)
