"""Spec 170 capability 4: 168 integration (workspace context, generation allowance, eager evaluator)."""

from __future__ import annotations

import copy
import math
import time
from typing import Any

import pytest

from app.modules.bio_models import service as bio_models
from app.modules.process_stack import draft, editor, mixed, mixed_runtime, pbr_unit
from app.modules.process_stack.mixed_runtime import JarvisUnitContext, JarvisUnitEvaluation, SegmentFailure
from tests.plumbing_170_support import new_workspace, pbr_document, pbr_ops
from tests.test_process_mixed_168 import _state, _stream, _tear_document, _unit


def test_pbr_evaluator_is_registered_eagerly_from_the_pbr_module() -> None:
    assert mixed_runtime.JARVIS_EVALUATORS["PhotobioreactorT1"] is pbr_unit.evaluate_pbr
    assert mixed_runtime.pbr_unit is pbr_unit
    assert callable(pbr_unit.warm_imports)
    assert mixed_runtime.JARVIS_EVALUATORS["SpecifiedSeparator"] is mixed_runtime._evaluate_separator


def test_context_and_evaluation_gain_only_additive_defaulted_fields() -> None:
    context = JarvisUnitContext(1000.0, time.monotonic() + 5, {})
    assert context.workspace_id is None and context.validation is False
    evaluation = JarvisUnitEvaluation(outlets={}, result={})
    assert evaluation.culture_generation_allowance == {}
    assert JarvisUnitContext(1000.0, 0.0, {}, workspace_id="ws").workspace_id == "ws"


def _balance_document() -> tuple[dict[str, Any], dict[str, Any]]:
    document = _tear_document()
    document["objects"] = {key: value for key, value in document["objects"].items() if key in {"feed", "j", "j_s", "j_b"}}
    document["objects"]["feed"]["target"] = {"unit": "j", "port": 0}
    document["objects"]["j_s"]["target"] = None
    document["objects"]["j_b"]["target"] = None
    inlet = _state()
    inlet["culture"]["biomass"] = 1.0
    concentrate, clarified = copy.deepcopy(inlet), copy.deepcopy(inlet)
    concentrate["mass_flow_kg_s"], concentrate["culture"]["biomass"] = 0.5, 3.0
    clarified["mass_flow_kg_s"], clarified["culture"]["biomass"] = 0.5, 1.0
    return document, {"Feed": inlet, "Concentrate": concentrate, "Clarified": clarified}


def test_unit_balance_adds_the_declared_allowance_to_tolerance_and_reports_it() -> None:
    document, known = _balance_document()
    # in 1.0 + generated (1 - 1e-4) - out 2.0 leaves a -1e-4 residual that only an allowance can absorb.
    generation = {"Separator": {"biomass": 1.0 - 1e-4}}
    with pytest.raises(SegmentFailure) as refused:  # an unabsorbed residual fails the segment (168 rule)
        mixed_runtime._culture_balances(document, known, generation)
    strict = refused.value.detail["unit_balances"]["biomass"]
    assert not strict["passed"] and strict["generation_allowance"] == 0.0
    assert strict["tolerance"] == pytest.approx(1e-9 * 2.0 + 1e-12)
    relaxed = mixed_runtime._culture_balances(document, known, generation,
                                              {"Separator": {"biomass": 2e-4}})["Separator"]["biomass"]
    assert relaxed["passed"] and relaxed["generation_allowance"] == 2e-4
    assert relaxed["tolerance"] == pytest.approx(strict["tolerance"] + 2e-4)
    assert relaxed["residual"] == strict["residual"] and relaxed["generated"] == strict["generated"]
    # The allowance is per field: another field's allowance does not widen biomass.
    with pytest.raises(SegmentFailure):
        mixed_runtime._culture_balances(document, known, generation, {"Separator": {"nitrogen": 1.0}})


def test_unit_balance_without_allowances_keeps_every_pre_170_number() -> None:
    document, known = _balance_document()
    row = mixed_runtime._culture_balances(document, known, {"Separator": {"biomass": 1.0}})["Separator"]["biomass"]
    assert row["passed"] and row["residual"] == 0.0 and row["tolerance"] == 1e-9 * 2.0 + 1e-12
    assert set(row) == {"in", "out", "residual", "generated", "generation_allowance", "tolerance", "unit", "passed"}


def _whole_graph(allowance: float | None) -> dict[str, Any]:
    feed, product = _stream("f", "Feed"), _stream("p", "Product", source="u")
    document = {"objects": {"f": feed, "p": product, "u": _unit("u", "Unit", "Heater")}}
    state_in, state_out = _state(), _state()
    state_in["culture"]["biomass"], state_out["culture"]["biomass"] = 1.0, 2.0
    final: dict[str, Any] = {"known": {"Feed": state_in, "Product": state_out}, "units": {},
                             "culture_generation": {"Unit": {"biomass": 1.0 - 1e-4}}}
    if allowance is not None:
        final["culture_generation_allowance"] = {"Unit": {"biomass": allowance}, "Other": {"biomass": allowance}}
    return mixed_runtime._whole_graph_balances(document, final, {})["balances"]["biomass"]


def test_whole_graph_balance_sums_every_units_allowance_into_the_tolerance() -> None:
    strict = _whole_graph(None)
    assert not strict["passed"] and strict["generation_allowance"] == 0.0
    one_unit = _whole_graph(1e-4)  # two declaring units: allowance 2e-4 in total
    assert one_unit["generation_allowance"] == pytest.approx(2e-4) and one_unit["passed"]
    assert one_unit["tolerance"] == pytest.approx(strict["tolerance"] + 2e-4)
    assert one_unit["residual"] == strict["residual"] == pytest.approx(-1e-4)


@pytest.mark.parametrize("allowance", [{"biomass": -1e-9}, {"biomass": math.nan}, {"biomass": math.inf},
                                       {"unknown_field": 1.0}, {"biomass": "1e-6"}])
def test_invalid_allowances_are_refused_like_invalid_generation(allowance: dict[str, Any]) -> None:
    unit = pbr_document()["objects"]["pbr"]
    evaluation = JarvisUnitEvaluation(outlets={"outlet": {}}, result={}, culture_generation_allowance=allowance)
    with pytest.raises(SegmentFailure) as error:
        mixed_runtime._check_evaluation(unit, evaluation)
    assert error.value.detail == {"code": "JARVIS_GENERATION_INVALID"}


def test_valid_allowances_pass_the_evaluation_check() -> None:
    unit = pbr_document()["objects"]["pbr"]
    mixed_runtime._check_evaluation(unit, JarvisUnitEvaluation(
        outlets={"outlet": {}}, result={}, culture_generation_allowance={"biomass": 0.0, "oxygen": 1e-12}))


def _fake_pbr(captured: list[JarvisUnitContext], monkeypatch: pytest.MonkeyPatch) -> None:
    def evaluate(unit: dict, inlet: dict, context: JarvisUnitContext) -> JarvisUnitEvaluation:
        captured.append(context)
        return JarvisUnitEvaluation(outlets={"outlet": copy.deepcopy(inlet)}, result={"owner": "jarvis_bio"})

    monkeypatch.setitem(mixed_runtime.JARVIS_EVALUATORS, "PhotobioreactorT1", evaluate)


def test_validation_passes_the_workspace_to_the_evaluator_in_validation_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[JarvisUnitContext] = []
    _fake_pbr(captured, monkeypatch)
    document = pbr_document(model=True)
    states = mixed_runtime._validation_states(document, mixed.partition(document), "ws-9")
    assert [(item.workspace_id, item.validation) for item in captured] == [("ws-9", True)]
    assert "Product" in states
    mixed_runtime._validation_states(document, mixed.partition(document))
    assert captured[-1].workspace_id is None


def test_run_threads_the_workspace_into_the_mixed_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(document, *, action, client, dwsim_version, mcp_sha256, run_dir, workspace_id=None):
        seen.update(workspace_id=workspace_id, action=action)
        return {"status": "completed", "streams": {}, "units": {}, "process_fingerprint": "sha256:x"}

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    workspace_id = new_workspace()
    monkeypatch.setattr(bio_models, "pbr_model_findings", lambda *_a: [], raising=False)
    monkeypatch.setattr(editor, "_client", lambda: (FakeClient(), "a" * 64, "10.2.9"))
    monkeypatch.setattr(mixed_runtime, "run", fake_run)
    created = draft.create_draft(workspace_id, "PBR run")
    state = draft.patch(workspace_id, created["draft_id"], created["revision"], pbr_ops(model=True))
    outcome = draft.execute(workspace_id, state["draft_id"], state["revision"], "run")
    assert seen == {"workspace_id": workspace_id, "action": "run"}
    assert outcome["run"]["status"] == "completed"


def test_execute_validates_with_the_workspace_so_model_blockers_stop_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = new_workspace()
    blocker = {"severity": "blocker", "code": "PBR_MODEL_CARD_UNAVAILABLE", "message": "gone", "object": "PBR",
               "field": "model", "source": "jarvis"}
    monkeypatch.setattr(bio_models, "pbr_model_findings", lambda *_a: [blocker], raising=False)
    created = draft.create_draft(workspace_id, "PBR blocked")
    state = draft.patch(workspace_id, created["draft_id"], created["revision"], pbr_ops(model=True))
    with pytest.raises(draft.DraftError) as error:
        draft.execute(workspace_id, state["draft_id"], state["revision"], "run")
    assert error.value.code == "draft_invalid"
    assert "PBR_MODEL_CARD_UNAVAILABLE" in {item["code"] for item in error.value.detail["findings"]}


def test_startup_warms_the_pbr_imports_before_accepting_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    calls: list[int] = []
    monkeypatch.setattr(pbr_unit, "warm_imports", lambda: calls.append(1))
    with TestClient(app):
        assert calls == [1]
    assert calls == [1]
    # A failing warm-up is logged, never fatal.
    def boom() -> None:
        raise RuntimeError("cold import failed")

    monkeypatch.setattr(pbr_unit, "warm_imports", boom)
    with TestClient(app):
        pass
