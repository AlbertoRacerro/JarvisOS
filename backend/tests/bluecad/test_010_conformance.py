"""BLUECAD spec-010 conformance tests — REVIEWER-OWNED.

Written by the reviewing tier before implementation (AGENTS.md
"Reviewer-owned conformance tests"). Implementation PRs must not add to,
modify, or delete this file.

Design principle: the AI loop's most important properties are the ones that
hold when everything is denied. Under safe defaults (paid AI disabled, no
keys, budget zero — AGENTS.md invariant 4) the loop MUST park immediately
with reason budget_blocked, spend nothing, execute no provider call, and
allow no promotion. These tests measure that through the public HTTP API and
the ai_jobs ledger, independent of implementation internals.

Contract pins (also stated in the spec-010 launch instructions):
- POST /workspaces/{id}/bluecad/candidates accepts {"brief_text": str, ...}.
- Candidate JSON exposes status / parked_reason / origin / attempts either at
  the top level or under a "candidate" key.

Skips cleanly while the loop module does not exist yet.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip(
    "app.modules.bluecad.loop", reason="spec 010 loop module not present yet"
)

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.delenv("SCALEWAY_API_KEY", raising=False)
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("SCALEWAY_MODEL", raising=False)

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)

    with TestClient(create_app()) as test_client:
        yield test_client

    get_settings.cache_clear()


def _workspace_id(client: TestClient) -> str:
    response = client.get("/workspaces")
    assert response.status_code == 200
    workspaces = response.json()
    assert workspaces, "seeded default workspace expected"
    first = workspaces[0]
    return first.get("id") or first.get("workspace_id")


def _candidate(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and "candidate" in payload:
        return payload["candidate"]
    assert isinstance(payload, dict)
    return payload


def _provider_call_count() -> int:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM ai_jobs WHERE provider_id IS NOT NULL"
        ).fetchone()
    return int(row[0])


def _create_candidate(client: TestClient, workspace_id: str) -> dict[str, Any]:
    response = client.post(
        f"/workspaces/{workspace_id}/bluecad/candidates",
        json={"brief_text": "6 m loop with two 90-degree bends and socket joints"},
    )
    assert response.status_code in (200, 201), response.text
    return _candidate(response.json())


def test_safe_defaults_park_with_budget_blocked_and_zero_spend(client: TestClient) -> None:
    """Invariant 4 + spec 010: with paid AI disabled the loop parks
    immediately — no retries, no provider execution, no artifacts."""
    workspace_id = _workspace_id(client)
    before = _provider_call_count()

    candidate = _create_candidate(client, workspace_id)

    assert candidate["status"] == "parked", candidate
    assert candidate["parked_reason"] == "budget_blocked", candidate
    assert candidate.get("origin", "ai") == "ai"
    assert _provider_call_count() == before, (
        "a budget-blocked candidate must not execute any provider call"
    )


def test_parked_candidate_is_retrievable_with_its_reason(client: TestClient) -> None:
    workspace_id = _workspace_id(client)
    created = _create_candidate(client, workspace_id)
    candidate_id = created.get("id") or created.get("candidate_id")
    assert candidate_id, created

    listing = client.get(f"/workspaces/{workspace_id}/bluecad/candidates")
    assert listing.status_code == 200
    ids = [
        (_candidate(item).get("id") or _candidate(item).get("candidate_id"))
        for item in listing.json()
    ]
    assert candidate_id in ids

    detail = client.get(f"/workspaces/{workspace_id}/bluecad/candidates/{candidate_id}")
    assert detail.status_code == 200
    fetched = _candidate(detail.json())
    assert fetched["status"] == "parked"
    assert fetched["parked_reason"] == "budget_blocked"


def test_promotion_of_non_valid_candidate_is_rejected(client: TestClient) -> None:
    """Only status=valid may be promoted; a parked candidate must be refused
    with a structured client error and no Decision may be created."""
    workspace_id = _workspace_id(client)
    created = _create_candidate(client, workspace_id)
    candidate_id = created.get("id") or created.get("candidate_id")

    decisions_before = client.get(f"/workspaces/{workspace_id}/decisions")
    count_before = len(decisions_before.json()) if decisions_before.status_code == 200 else None

    response = client.post(
        f"/workspaces/{workspace_id}/bluecad/candidates/{candidate_id}/promote"
    )
    assert 400 <= response.status_code < 500, (
        f"promoting a parked candidate must be a client error, got "
        f"{response.status_code}: {response.text}"
    )

    if count_before is not None:
        decisions_after = client.get(f"/workspaces/{workspace_id}/decisions")
        assert len(decisions_after.json()) == count_before, (
            "no Decision may be created by a rejected promotion"
        )

    detail = client.get(f"/workspaces/{workspace_id}/bluecad/candidates/{candidate_id}")
    assert _candidate(detail.json())["status"] == "parked", "status must be unchanged"


def test_repeated_briefs_never_accumulate_provider_calls(client: TestClient) -> None:
    """Fail-closed spend safety must hold under repetition: N blocked
    candidates still mean zero provider executions in the ledger."""
    workspace_id = _workspace_id(client)
    before = _provider_call_count()
    for _ in range(3):
        candidate = _create_candidate(client, workspace_id)
        assert candidate["status"] == "parked"
    assert _provider_call_count() == before
