from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

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


_KERNEL_UNAVAILABLE = _kernel_unavailable_reason()
requires_kernel = pytest.mark.skipif(_KERNEL_UNAVAILABLE is not None, reason=_KERNEL_UNAVAILABLE or "build123d unavailable")


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


def _ai_job_count() -> int:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT COUNT(*) FROM ai_jobs WHERE provider_id = 'scaleway'").fetchone()
    return int(row[0])


def _artifact_sha256(artifact_id: str) -> str:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT sha256 FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    return str(row[0])


@requires_kernel
def test_happy_path_valid_candidate_records_attempt_and_artifacts() -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_spec()])

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="single tube"),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert candidate.parked_reason is None
    assert len(candidate.attempts) == 1
    attempt = candidate.attempts[0]
    assert attempt.proposal_ai_job_id is not None
    assert attempt.proposal_outcome == "ok"
    assert attempt.build_outcome == "ok"
    assert attempt.validation_verdict == "pass"
    assert candidate.spec_artifact_id is not None
    assert candidate.report_artifact_id is not None
    assert candidate.glb_artifact_id is not None
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        evidence = connection.execute("SELECT kind, verdict, candidate_id, attempt_id FROM evidence_records").fetchall()
    assert [dict(row) for row in evidence] == [
        {"kind": "validation_v0", "verdict": "pass", "candidate_id": candidate.id, "attempt_id": attempt.id}
    ]
    assert _ai_job_count() == 1


@requires_kernel
def test_repair_prompt_contains_previous_validation_report_detail() -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_bad_volume_spec(), _spec()])

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="repair volume"),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "valid"
    assert len(candidate.attempts) == 2
    assert candidate.attempts[0].validation_verdict == "fail"
    assert candidate.attempts[1].validation_verdict == "pass"
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        evidence = connection.execute(
            "SELECT kind, verdict, candidate_id, attempt_id FROM evidence_records ORDER BY created_at, id"
        ).fetchall()
    assert [dict(row) for row in evidence] == [
        {
            "kind": "validation_v0",
            "verdict": "fail",
            "candidate_id": candidate.id,
            "attempt_id": candidate.attempts[0].id,
        },
        {
            "kind": "validation_v0",
            "verdict": "pass",
            "candidate_id": candidate.id,
            "attempt_id": candidate.attempts[1].id,
        },
    ]
    assert len(adapter.prompts) == 2
    assert "T1_VOLUME_DECL" in adapter.prompts[1]


@requires_kernel
def test_exhaustion_caps_provider_calls_across_tier_ladder() -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter([_bad_volume_spec()] * 6)

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="never valid"),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "parked"
    assert candidate.parked_reason == "attempts_exhausted"
    assert [a.route_class for a in candidate.attempts] == ["external:cheap"] * 3 + ["external:reasoning"] * 3
    assert _ai_job_count() == 6


def test_malformed_outputs_are_recorded_without_building() -> None:
    _init()
    adapter = ScriptedFakeBluecadAdapter(["not json"] * 6)

    candidate = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="malformed", loop_config=BluecadLoopConfig(max_attempts_per_tier=3)),
        adapters={"scaleway": adapter},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert candidate.status == "parked"
    assert candidate.parked_reason == "malformed_repeated"
    assert len(candidate.attempts) == 6
    assert all(attempt.proposal_outcome == "malformed" for attempt in candidate.attempts)
    assert all(attempt.build_outcome is None for attempt in candidate.attempts)
    assert _ai_job_count() == 6


def test_budget_blocked_safe_default_records_zero_provider_calls() -> None:
    _init()

    candidate = create_bluecad_candidate("main", BluecadCandidateCreate(brief_text="blocked"))

    assert candidate.status == "parked"
    assert candidate.parked_reason == "budget_blocked"
    assert candidate.attempts == []
    assert _ai_job_count() == 0


@requires_kernel
def test_deterministic_outputs_for_same_scripted_sequence() -> None:
    _init()
    first = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="deterministic"),
        adapters={"scaleway": ScriptedFakeBluecadAdapter([_spec()])},
        bindings=_bindings(),
        force_external_allowed=True,
    )
    second = create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(brief_text="deterministic"),
        adapters={"scaleway": ScriptedFakeBluecadAdapter([_spec()])},
        bindings=_bindings(),
        force_external_allowed=True,
    )

    assert first.status == second.status == "valid"
    assert [(a.route_class, a.proposal_outcome, a.build_outcome, a.validation_verdict) for a in first.attempts] == [
        (a.route_class, a.proposal_outcome, a.build_outcome, a.validation_verdict) for a in second.attempts
    ]
    assert first.spec_artifact_id is not None
    assert second.spec_artifact_id is not None
    assert _artifact_sha256(first.spec_artifact_id) == _artifact_sha256(second.spec_artifact_id)


def test_loop_module_does_not_import_create_decision() -> None:
    loop_path = Path(__file__).parents[2] / "app" / "modules" / "bluecad" / "loop.py"
    assert "create_decision" not in loop_path.read_text(encoding="utf-8")


def test_promote_valid_candidate_creates_decision() -> None:
    _init()
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.modules.bluecad.ledger import create_candidate_record, mark_candidate_valid
    from app.modules.bluecad.models import BluecadLoopConfig

    candidate = create_candidate_record("main", "promotable", BluecadLoopConfig())
    mark_candidate_valid(candidate.id)

    with TestClient(create_app()) as client:
        before = client.get("/workspaces/main/decisions")
        assert before.status_code == 200
        response = client.post(f"/workspaces/main/bluecad/candidates/{candidate.id}/promote")
        assert response.status_code == 200, response.text
        promoted = response.json()
        assert promoted["promoted_decision_id"] is not None
        after = client.get("/workspaces/main/decisions")
        assert len(after.json()) == len(before.json()) + 1
