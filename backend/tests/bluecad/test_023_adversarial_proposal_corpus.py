"""Offline hostile model-output corpus for the existing BLUECAD proposal loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.modules.bluecad.loop as loop_module
from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.modules.ai.execution import ProviderBinding
from app.modules.bluecad.ledger import ScriptedFakeBluecadAdapter
from app.modules.bluecad.models import BluecadCandidateCreate, BluecadLoopConfig
from app.modules.bluecad.spec import SpecValidationError

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = json.loads((Path(__file__).parent / "corpus" / "adversarial_proposals.json").read_text(encoding="utf-8"))


def _responses() -> list[tuple[str, str]]:
    valid = json.loads((FIXTURES / "minimal_single_tube.json").read_text(encoding="utf-8"))
    non_finite = json.loads(json.dumps(valid))
    non_finite["parts"][0]["params"]["length"] = float("nan")
    return [(item["name"], item["response"]) for item in CORPUS] + [
        ("non_finite_valid_shape", json.dumps(non_finite)),
        ("deep_nesting", '{"parts":' + "[" * 1200 + "0" + "]" * 1200 + "}"),
        ("oversized", "{" + " " * 262_144 + "}"),
    ]


def test_oversized_output_stops_before_json_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_extraction(_text: str) -> str:
        pytest.fail("oversized output reached JSON extraction")

    monkeypatch.setattr(loop_module, "_extract_single_json_object", unexpected_extraction)
    with pytest.raises(SpecValidationError, match="proposal text limit"):
        loop_module.parse_geometry_spec_response("{" + " " * 262_144 + "}")


@pytest.mark.parametrize(
    ("name", "response"),
    [pytest.param(name, response, id=name) for name, response in _responses()],
)
def test_hostile_output_is_malformed_and_loop_is_bounded(
    name: str, response: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SpecValidationError):
        loop_module.parse_geometry_spec_response(response)

    initialize_storage(seed_default=True)
    adapter = ScriptedFakeBluecadAdapter([response, response])
    binding = ProviderBinding(
        "external:cheap", "scaleway", "scripted", False, 4000,
        execution_class="synthetic", context_window_tokens=8192,
    )

    def unexpected_build(*_args: object, **_kwargs: object) -> None:
        pytest.fail(f"{name} reached geometry build")

    monkeypatch.setattr(loop_module, "_build_and_register", unexpected_build)
    candidate = loop_module.create_bluecad_candidate(
        "main",
        BluecadCandidateCreate(
            brief_text=f"adversarial corpus {name}",
            loop_config=BluecadLoopConfig(max_attempts_per_tier=2, tier_ladder=["external:cheap"]),
        ),
        adapters={"scaleway": adapter},
        bindings={"external:cheap": binding},
        force_external_allowed=True,
    )

    assert candidate.status == "parked"
    expected_outcome = "provider_error" if name == "empty" else "malformed"
    assert candidate.parked_reason == ("attempts_exhausted" if name == "empty" else "malformed_repeated")
    assert len(adapter.prompts) == 2
    assert [attempt.proposal_outcome for attempt in candidate.attempts] == [expected_outcome] * 2
    assert all(attempt.build_outcome is None and attempt.spec_artifact_id is None for attempt in candidate.attempts)
    assert candidate.spec_artifact_id is None
    assert candidate.promoted_decision_id is None
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
