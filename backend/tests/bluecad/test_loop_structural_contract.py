from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

import app.modules.bluecad.loop as loop_module
from app.core.database import open_sqlite_connection
from app.modules.ai.execution import ProviderBinding
from app.modules.bluecad.ledger import ScriptedFakeBluecadAdapter
from app.modules.bluecad.loop import SimulationStageOutcome, create_bluecad_candidate
from app.modules.bluecad.models import BluecadCandidateCreate, BluecadLoopConfig

FIXTURES = Path(__file__).parent / "fixtures"


def _kernel_unavailable_reason() -> str | None:
    if importlib.util.find_spec("build123d") is None:
        return "build123d is not installed"
    try:
        import build123d  # noqa: F401
    except ImportError as exc:
        return f"build123d cannot be imported: {exc}"
    return None


requires_kernel = pytest.mark.skipif(
    _kernel_unavailable_reason() is not None,
    reason=_kernel_unavailable_reason() or "build123d unavailable",
)


def _init() -> None:
    from app.core.bootstrap import initialize_storage

    initialize_storage(seed_default=True)


def _bindings() -> dict[str, ProviderBinding]:
    return {
        "external:cheap": ProviderBinding(
            "external:cheap",
            "scaleway",
            "scripted",
            False,
            4000,
            execution_class="synthetic",
            context_window_tokens=8192,
        )
    }


def _spec() -> str:
    return (FIXTURES / "minimal_single_tube.json").read_text(encoding="utf-8")


def _analysis_spec(*, criteria: bool = True) -> dict[str, Any]:
    return {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "a1",
        "analysis_type": "static",
        "material": {
            "name": "steel",
            "E": 200000.0,
            "nu": 0.3,
            "rho": 7.8e-9,
            "yield_strength": 250.0,
        },
        "bcs": [{"port_label": "run1.port_a", "kind": "fixed"}],
        "loads": [
            {
                "port_label": "run1.port_b",
                "type": "force_total",
                "force": [1.0, 0.0, 0.0],
            }
        ],
        "mesh": {"target_size": 5.0},
        "pass_criteria": (
            [{"metric": "max_von_mises", "op": "<=", "value": 50.0}]
            if criteria
            else []
        ),
    }


def _config(*, enabled: bool = True) -> BluecadLoopConfig:
    return BluecadLoopConfig(
        max_attempts_per_tier=1,
        tier_ladder=["external:cheap"],
        analysis_spec=_analysis_spec(),
        structural_repair=enabled,
        max_structural_repairs=1,
    )


def _mesh_pass() -> dict[str, Any]:
    return {
        "schema_version": "bluecad_mesh_result_v0_1",
        "verdict": "pass",
        "errors": [],
        "attempts": [
            {
                "attempt_no": 1,
                "target_size": 5.0,
                "counts": {"elements_total": 2, "nodes_total": 4},
                "errors": [],
            }
        ],
        "artifacts": {
            "mesh_inp": {"path": "mesh.inp", "sha256": "abc", "bytes": 12}
        },
    }


def _fem_fail_criteria() -> dict[str, Any]:
    return {
        "schema_version": "bluecad_result_summary_v0_1",
        "verdict": "pass",
        "errors": [],
        "solver": {"tool_id": "calculix", "version": "fake", "returncode": 0},
        "max_displacement": {"node_id": 1, "value": 0.1},
        "max_von_mises": {"element_id": 1, "node_id": 1, "value": 100.0},
        "artifacts": {},
    }


def _patch_criteria_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        loop_module,
        "mesh_analysis_spec",
        lambda *_args, **_kwargs: _mesh_pass(),
    )
    monkeypatch.setattr(
        loop_module,
        "solve_static_analysis",
        lambda *_args, **_kwargs: _fem_fail_criteria(),
    )


def _candidate_pointers(candidate_id: str) -> tuple[str | None, str | None, str | None]:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT spec_artifact_id, glb_artifact_id, report_artifact_id FROM bluecad_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
    assert row is not None
    return (
        row["spec_artifact_id"],
        row["glb_artifact_id"],
        row["report_artifact_id"],
    )


@pytest.mark.parametrize(
    "status",
    [
        "skipped",
        "no_criteria",
        "setup_error",
        "mesh_failed",
        "mesh_error",
        "mesh_evidence_error",
        "solve_error",
        "fem_evidence_error",
        "criteria_error",
        "criteria_passed",
    ],
)
@requires_kernel
def test_only_exact_criteria_failed_status_can_trigger_repair(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_spec()])
    monkeypatch.setattr(
        loop_module,
        "_run_simulation_stage",
        lambda *_args, **_kwargs: SimulationStageOutcome(status),
    )

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text=f"non-trigger {status}", loop_config=_config()),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert len(candidate.attempts) == 1
    assert len(adapter.prompts) == 1


@requires_kernel
def test_disabled_structural_repair_accepts_empty_criteria_and_stays_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_spec()])
    analysis_spec = _analysis_spec(criteria=False)
    monkeypatch.setattr(
        loop_module,
        "mesh_analysis_spec",
        lambda *_args, **_kwargs: _mesh_pass(),
    )
    monkeypatch.setattr(
        loop_module,
        "solve_static_analysis",
        lambda *_args, **_kwargs: _fem_fail_criteria(),
    )

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(
            brief_text="empty criteria disabled",
            loop_config=BluecadLoopConfig(
                max_attempts_per_tier=1,
                tier_ladder=["external:cheap"],
                analysis_spec=analysis_spec,
                structural_repair=False,
            ),
        ),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert len(candidate.attempts) == 1
    assert len(adapter.prompts) == 1


class _StatusInspectingAdapter(ScriptedFakeBluecadAdapter):
    def complete(self, request):
        if self.prompts:
            with open_sqlite_connection() as connection:
                row = connection.execute(
                    "SELECT status, spec_artifact_id, glb_artifact_id, report_artifact_id FROM bluecad_candidates"
                ).fetchone()
            assert row is not None
            assert row["status"] == "valid"
            assert row["spec_artifact_id"] is not None
            assert row["glb_artifact_id"] is not None
            assert row["report_artifact_id"] is not None
        return super().complete(request)


@requires_kernel
def test_structural_attempt_stays_valid_and_never_revalidates_or_parks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapter = _StatusInspectingAdapter([_spec(), _spec()])
    _patch_criteria_failure(monkeypatch)
    calls = {"mark": 0, "park": 0}
    original_mark = loop_module.mark_candidate_valid
    original_park = loop_module.park_candidate

    def mark(candidate_id: str) -> None:
        calls["mark"] += 1
        original_mark(candidate_id)

    def park(candidate_id: str, reason: str, notes: str | None = None) -> None:
        calls["park"] += 1
        original_park(candidate_id, reason, notes)

    monkeypatch.setattr(loop_module, "mark_candidate_valid", mark)
    monkeypatch.setattr(loop_module, "park_candidate", park)

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="lifecycle", loop_config=_config()),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert calls == {"mark": 1, "park": 0}
    assert len(candidate.attempts) == 2


class _SecondCallProviderFailure(ScriptedFakeBluecadAdapter):
    def complete(self, request):
        if self.prompts:
            raise RuntimeError("structural provider failure")
        return super().complete(request)


@requires_kernel
def test_structural_provider_failure_returns_original_valid_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapter = _SecondCallProviderFailure([_spec()])
    _patch_criteria_failure(monkeypatch)

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="provider failure", loop_config=_config()),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert len(candidate.attempts) == 2
    assert candidate.attempts[1].proposal_outcome == "provider_error"
    assert candidate.spec_artifact_id == candidate.attempts[0].spec_artifact_id
    assert candidate.report_artifact_id == candidate.attempts[0].report_artifact_id


class _RemovingAdapter(ScriptedFakeBluecadAdapter):
    def __init__(self, responses: list[str], adapters: dict[str, Any]) -> None:
        super().__init__(responses)
        self.adapters = adapters

    def complete(self, request):
        response = super().complete(request)
        self.adapters.pop("scaleway", None)
        return response


@requires_kernel
def test_structural_config_failure_returns_original_valid_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapters: dict[str, Any] = {"sentinel": object()}
    adapter = _RemovingAdapter([_spec()], adapters)
    adapters["scaleway"] = adapter
    _patch_criteria_failure(monkeypatch)

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="config failure", loop_config=_config()),
        adapters=adapters,
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert len(candidate.attempts) == 2
    assert candidate.attempts[1].proposal_outcome == "blocked"
    assert candidate.spec_artifact_id == candidate.attempts[0].spec_artifact_id
    assert candidate.report_artifact_id == candidate.attempts[0].report_artifact_id


@requires_kernel
def test_structural_build_exception_preserves_all_candidate_pointers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_spec(), _spec()])
    _patch_criteria_failure(monkeypatch)
    original_build = loop_module.build_geometry_spec
    build_calls = 0

    def build(*args: Any, **kwargs: Any):
        nonlocal build_calls
        build_calls += 1
        if build_calls == 2:
            raise RuntimeError("speculative build failed")
        return original_build(*args, **kwargs)

    monkeypatch.setattr(loop_module, "build_geometry_spec", build)

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="build failure", loop_config=_config()),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert len(candidate.attempts) == 2
    assert candidate.attempts[1].build_outcome == "error"
    assert _candidate_pointers(candidate.id) == (
        candidate.attempts[0].spec_artifact_id,
        candidate.glb_artifact_id,
        candidate.attempts[0].report_artifact_id,
    )
