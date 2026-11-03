from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.database import open_sqlite_connection
from app.modules.ai.execution import ProviderBinding
from app.modules.bluecad.ledger import ScriptedFakeBluecadAdapter
from app.modules.bluecad.loop import create_bluecad_candidate
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
        route: ProviderBinding(
            route,
            "scaleway",
            "scripted",
            False,
            4000,
            execution_class="synthetic",
            context_window_tokens=8192,
        )
        for route in ["external:cheap", "external:reasoning"]
    }


def _spec(name: str = "minimal_single_tube.json") -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _bad_volume_spec() -> str:
    payload = json.loads(_spec())
    payload["declared"]["total_volume_mm3"]["value"] = 1.0
    return json.dumps(payload)


def _analysis_spec(limit: float = 50.0) -> dict[str, Any]:
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
        "pass_criteria": [
            {"metric": "max_von_mises", "op": "<=", "value": limit}
        ],
    }


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


def _fem(value: float = 100.0) -> dict[str, Any]:
    return {
        "schema_version": "bluecad_result_summary_v0_1",
        "verdict": "pass",
        "errors": [],
        "solver": {"tool_id": "calculix", "version": "fake", "returncode": 0},
        "max_displacement": {"node_id": 1, "value": 0.1},
        "max_von_mises": {"element_id": 1, "node_id": 1, "value": value},
        "artifacts": {},
    }


def _config(**updates: Any) -> BluecadLoopConfig:
    values: dict[str, Any] = {
        "max_attempts_per_tier": 1,
        "tier_ladder": ["external:cheap"],
        "analysis_spec": _analysis_spec(),
        "structural_repair": True,
        "max_structural_repairs": 1,
    }
    values.update(updates)
    return BluecadLoopConfig(**values)


def _candidate_row(candidate_id: str) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT status, spec_artifact_id, glb_artifact_id, report_artifact_id, promoted_decision_id FROM bluecad_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def test_structural_config_defaults_and_preconditions() -> None:
    defaults = BluecadLoopConfig()
    assert defaults.structural_repair is False
    assert defaults.max_structural_repairs == 1

    for value in (-1, 4):
        with pytest.raises(ValidationError):
            BluecadLoopConfig(max_structural_repairs=value)

    with pytest.raises(ValidationError):
        BluecadLoopConfig(structural_repair=True)

    empty = _analysis_spec()
    empty["pass_criteria"] = []
    with pytest.raises(ValidationError):
        BluecadLoopConfig(analysis_spec=empty, structural_repair=True)

    BluecadLoopConfig(analysis_spec=empty, structural_repair=False)


@requires_kernel
def test_exact_criteria_failure_triggers_repair_and_commits_pointers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_spec(), _spec()])
    fem_values = iter([100.0, 20.0])
    monkeypatch.setattr(
        "app.modules.bluecad.loop.mesh_analysis_spec",
        lambda *_args, **_kwargs: _mesh_pass(),
    )
    monkeypatch.setattr(
        "app.modules.bluecad.loop.solve_static_analysis",
        lambda *_args, **_kwargs: _fem(next(fem_values)),
    )

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="repair structure", loop_config=_config()),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert len(candidate.attempts) == 2
    assert candidate.spec_artifact_id == candidate.attempts[1].spec_artifact_id
    assert candidate.report_artifact_id == candidate.attempts[1].report_artifact_id
    assert candidate.spec_artifact_id != candidate.attempts[0].spec_artifact_id
    assert len(adapter.prompts) == 2
    assert "already geometrically valid" in adapter.prompts[1]
    assert "EVIDENCE_SIGHT_V0" in adapter.prompts[1]
    detail = json.loads(candidate.attempts[1].error_detail_json or "{}")
    assert detail["attempt_kind"] == "structural_repair"
    assert detail["prompt_version"] == "bluecad_ai_loop_v3_structural_v0_1"
    assert detail["evidence_digest"].startswith("sha256:")
    assert _candidate_row(candidate.id)["promoted_decision_id"] is None


@requires_kernel
def test_invalid_structural_repair_preserves_original_status_and_pointers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_spec(), _bad_volume_spec()])
    monkeypatch.setattr(
        "app.modules.bluecad.loop.mesh_analysis_spec",
        lambda *_args, **_kwargs: _mesh_pass(),
    )
    monkeypatch.setattr(
        "app.modules.bluecad.loop.solve_static_analysis",
        lambda *_args, **_kwargs: _fem(100.0),
    )

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="invalid repair", loop_config=_config()),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert len(candidate.attempts) == 2
    assert candidate.attempts[1].validation_verdict == "fail"
    assert candidate.spec_artifact_id == candidate.attempts[0].spec_artifact_id
    assert candidate.report_artifact_id == candidate.attempts[0].report_artifact_id
    assert candidate.spec_artifact_id != candidate.attempts[1].spec_artifact_id


@requires_kernel
def test_tier3_error_does_not_trigger_structural_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_spec()])
    monkeypatch.setattr(
        "app.modules.bluecad.loop.mesh_analysis_spec",
        lambda *_args, **_kwargs: _mesh_pass(),
    )
    monkeypatch.setattr(
        "app.modules.bluecad.loop.solve_static_analysis",
        lambda *_args, **_kwargs: _fem(100.0),
    )

    def fail_tier3(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("criteria unavailable")

    monkeypatch.setattr("app.modules.bluecad.loop.append_tier3_checks", fail_tier3)

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="criteria error", loop_config=_config()),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert len(candidate.attempts) == 1
    assert len(adapter.prompts) == 1


@requires_kernel
def test_structural_budget_is_separate_and_exhaustion_keeps_original_pointers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_spec(), _spec(), _spec()])
    monkeypatch.setattr(
        "app.modules.bluecad.loop.mesh_analysis_spec",
        lambda *_args, **_kwargs: _mesh_pass(),
    )
    monkeypatch.setattr(
        "app.modules.bluecad.loop.solve_static_analysis",
        lambda *_args, **_kwargs: _fem(100.0),
    )

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(
            brief_text="bounded repair",
            loop_config=_config(max_structural_repairs=2),
        ),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert len(candidate.attempts) == 3
    assert [attempt.attempt_no for attempt in candidate.attempts] == [1, 2, 3]
    assert candidate.spec_artifact_id == candidate.attempts[0].spec_artifact_id
    assert candidate.report_artifact_id == candidate.attempts[0].report_artifact_id
    assert len(adapter.prompts) == 3


@requires_kernel
def test_malformed_structural_attempt_retains_precall_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_spec(), "not json"])
    monkeypatch.setattr(
        "app.modules.bluecad.loop.mesh_analysis_spec",
        lambda *_args, **_kwargs: _mesh_pass(),
    )
    monkeypatch.setattr(
        "app.modules.bluecad.loop.solve_static_analysis",
        lambda *_args, **_kwargs: _fem(100.0),
    )

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="malformed repair", loop_config=_config()),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert candidate.attempts[1].proposal_outcome == "malformed"
    detail = json.loads(candidate.attempts[1].error_detail_json or "{}")
    assert detail["attempt_kind"] == "structural_repair"
    assert detail["prompt_version"] == "bluecad_ai_loop_v3_structural_v0_1"
    assert detail["evidence_digest"].startswith("sha256:")
