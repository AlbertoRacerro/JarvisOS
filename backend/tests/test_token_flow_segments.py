from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.token_flow_segments import (
    MAX_SEGMENT_BYTES,
    list_protected_segment_metadata,
    read_protected_segment,
    store_protected_segment,
)
from app.modules.ai.token_flow_service import (
    TokenFlowConflictError,
    TokenFlowError,
    create_flow,
)

BODY_A = "First bounded model output segment."
BODY_B = "Second bounded model output segment."
NOW = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)


@pytest.fixture
def initialized_database():
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def _workspace(workspace_id: str) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    with open_sqlite_connection() as connection:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO workspaces (id, name, slug, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (workspace_id, workspace_id, workspace_id, now, now),
        )
        connection.commit()


def _attempt(
    *,
    attempt_id: str,
    flow_id: str,
    index: int,
    body: str,
    task_kind: str = "synthesis",
    workspace_route: str = "local:fake",
    status: str = "success",
    finish_reason: str = "length",
    output_tokens: int = 7,
) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, requested_route_class,
                selected_route_class, provider_id, model_id, route_reason_json,
                output_digest, output_tokens, flow_id, flow_attempt_index,
                normalized_finish_reason
            ) VALUES (?, ?, ?, ?, ?, ?, 'fake', 'fake-v0', ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                utc_now(),
                status,
                task_kind,
                workspace_route,
                workspace_route,
                '{"decision_reason":"bound:local:fake","fallback_attempt_index":0}',
                canonical_digest({"text": body}),
                output_tokens,
                flow_id,
                index,
                finish_reason,
            ),
        )
        connection.commit()


def _flow_with_attempt(
    *, workspace_id: str | None = None, body: str = BODY_A
) -> tuple[str, str]:
    if workspace_id is not None:
        _workspace(workspace_id)
    flow = create_flow(
        task_kind="synthesis",
        requested_route_class="local:fake",
        workspace_id=workspace_id,
    )
    flow_id = str(flow["id"])
    attempt_id = "attempt-0"
    _attempt(attempt_id=attempt_id, flow_id=flow_id, index=0, body=body)
    return flow_id, attempt_id


def test_store_read_list_and_exact_replay_are_body_safe(
    initialized_database,
) -> None:
    flow_id, attempt_id = _flow_with_attempt(workspace_id="ws-segment")

    first = store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id=attempt_id,
        body_text=BODY_A,
        effective_sensitivity_level="S1",
        workspace_id="ws-segment",
        now=NOW,
    )
    replay = store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id=attempt_id,
        body_text=BODY_A,
        effective_sensitivity_level="S1",
        workspace_id="ws-segment",
        now=NOW + timedelta(minutes=1),
    )
    loaded = read_protected_segment(
        flow_id=flow_id,
        segment_index=0,
        workspace_id="ws-segment",
        expected_sensitivity_level="S1",
        now=NOW + timedelta(minutes=2),
    )
    listed = list_protected_segment_metadata(
        flow_id=flow_id,
        workspace_id="ws-segment",
        now=NOW + timedelta(minutes=2),
    )

    assert first == replay
    assert first.segment_index == 0
    assert first.body_digest == canonical_digest({"text": BODY_A})
    assert first.byte_count == len(BODY_A.encode("utf-8"))
    assert first.token_count == 7
    assert loaded.body_text == BODY_A
    assert BODY_A not in repr(loaded)
    assert tuple(asdict(item) for item in listed) == (asdict(first),)
    assert "body_text" not in asdict(first)


def test_store_requires_latest_successful_output_attempt(
    initialized_database,
) -> None:
    flow_id, first_attempt = _flow_with_attempt()
    _attempt(
        attempt_id="attempt-1",
        flow_id=flow_id,
        index=1,
        body=BODY_B,
    )

    with pytest.raises(TokenFlowConflictError, match="latest flow attempt"):
        store_protected_segment(
            flow_id=flow_id,
            originating_attempt_id=first_attempt,
            body_text=BODY_A,
            effective_sensitivity_level="S1",
            workspace_id=None,
            now=NOW,
        )


def test_segments_are_contiguous_and_bounded_by_flow_snapshot(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow_id, first_attempt = _flow_with_attempt()
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_flows SET max_direct_continuations_snapshot = 0 WHERE id = ?",
            (flow_id,),
        )
        connection.commit()

    first = store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id=first_attempt,
        body_text=BODY_A,
        effective_sensitivity_level="S0",
        workspace_id=None,
        now=NOW,
    )
    assert first.segment_index == 0

    _attempt(
        attempt_id="attempt-1",
        flow_id=flow_id,
        index=1,
        body=BODY_B,
    )
    with pytest.raises(TokenFlowConflictError, match="continuation snapshot"):
        store_protected_segment(
            flow_id=flow_id,
            originating_attempt_id="attempt-1",
            body_text=BODY_B,
            effective_sensitivity_level="S0",
            workspace_id=None,
            now=NOW,
        )


def test_second_attempt_receives_next_contiguous_segment_index(
    initialized_database,
) -> None:
    flow_id, first_attempt = _flow_with_attempt()
    first = store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id=first_attempt,
        body_text=BODY_A,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW,
    )
    _attempt(
        attempt_id="attempt-1",
        flow_id=flow_id,
        index=1,
        body=BODY_B,
    )
    second = store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id="attempt-1",
        body_text=BODY_B,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW + timedelta(minutes=1),
    )

    assert (first.segment_index, second.segment_index) == (0, 1)
    assert [item.originating_attempt_id for item in list_protected_segment_metadata(
        flow_id=flow_id,
        workspace_id=None,
        now=NOW + timedelta(minutes=2),
    )] == [first_attempt, "attempt-1"]


def test_cross_workspace_and_cross_flow_attempts_fail_closed(
    initialized_database,
) -> None:
    flow_id, attempt_id = _flow_with_attempt(workspace_id="ws-a")

    with pytest.raises(TokenFlowConflictError, match="workspace"):
        store_protected_segment(
            flow_id=flow_id,
            originating_attempt_id=attempt_id,
            body_text=BODY_A,
            effective_sensitivity_level="S1",
            workspace_id="ws-b",
            now=NOW,
        )

    _workspace("ws-b")
    other = create_flow(
        task_kind="synthesis",
        requested_route_class="local:fake",
        workspace_id="ws-b",
    )
    with pytest.raises(TokenFlowConflictError, match="belong to the flow"):
        store_protected_segment(
            flow_id=str(other["id"]),
            originating_attempt_id=attempt_id,
            body_text=BODY_A,
            effective_sensitivity_level="S1",
            workspace_id="ws-b",
            now=NOW,
        )


def test_body_digest_sensitivity_and_size_are_fail_closed(
    initialized_database,
) -> None:
    flow_id, attempt_id = _flow_with_attempt()

    with pytest.raises(TokenFlowConflictError, match="does not match originating output"):
        store_protected_segment(
            flow_id=flow_id,
            originating_attempt_id=attempt_id,
            body_text="forged output",
            effective_sensitivity_level="S1",
            workspace_id=None,
            now=NOW,
        )
    with pytest.raises(TokenFlowError, match="sensitivity"):
        store_protected_segment(
            flow_id=flow_id,
            originating_attempt_id=attempt_id,
            body_text=BODY_A,
            effective_sensitivity_level="public",
            workspace_id=None,
            now=NOW,
        )
    with pytest.raises(TokenFlowError, match="byte limit"):
        store_protected_segment(
            flow_id=flow_id,
            originating_attempt_id=attempt_id,
            body_text="x" * (MAX_SEGMENT_BYTES + 1),
            effective_sensitivity_level="S1",
            workspace_id=None,
            now=NOW,
        )


def test_persisted_body_policy_and_guard_tampering_is_detected(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow_id, attempt_id = _flow_with_attempt()
    store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id=attempt_id,
        body_text=BODY_A,
        effective_sensitivity_level="S2",
        workspace_id=None,
        now=NOW,
    )

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_flow_segments SET body_text = 'tampered' WHERE flow_id = ?",
            (flow_id,),
        )
        connection.commit()
    with pytest.raises(TokenFlowConflictError, match="digest evidence"):
        read_protected_segment(
            flow_id=flow_id,
            segment_index=0,
            workspace_id=None,
            expected_sensitivity_level="S2",
            now=NOW,
        )

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_flow_segments SET body_text = ?, body_digest = ? WHERE flow_id = ?",
            (BODY_A, canonical_digest({"text": BODY_A}), flow_id),
        )
        connection.execute(
            "UPDATE ai_jobs SET route_reason_json = '{\"decision_reason\":\"changed\"}' "
            "WHERE id = ?",
            (attempt_id,),
        )
        connection.commit()
    with pytest.raises(TokenFlowConflictError, match="policy binding"):
        read_protected_segment(
            flow_id=flow_id,
            segment_index=0,
            workspace_id=None,
            expected_sensitivity_level="S2",
            now=NOW,
        )

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_jobs SET route_reason_json = "
            "'{\"decision_reason\":\"bound:local:fake\",\"fallback_attempt_index\":0}' "
            "WHERE id = ?",
            (attempt_id,),
        )
        connection.execute(
            "UPDATE ai_flows SET max_direct_continuations_snapshot = 3 WHERE id = ?",
            (flow_id,),
        )
        connection.commit()
    with pytest.raises(TokenFlowConflictError, match="continuation guard"):
        read_protected_segment(
            flow_id=flow_id,
            segment_index=0,
            workspace_id=None,
            expected_sensitivity_level="S2",
            now=NOW,
        )


def test_expired_segment_body_is_unreadable_but_safe_metadata_remains_visible(
    initialized_database,
) -> None:
    flow_id, attempt_id = _flow_with_attempt()
    stored = store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id=attempt_id,
        body_text=BODY_A,
        effective_sensitivity_level="S3",
        workspace_id=None,
        now=NOW,
    )

    after_expiry = NOW + timedelta(hours=25)
    with pytest.raises(TokenFlowConflictError, match="expired"):
        read_protected_segment(
            flow_id=flow_id,
            segment_index=0,
            workspace_id=None,
            expected_sensitivity_level="S3",
            now=after_expiry,
        )
    metadata = list_protected_segment_metadata(
        flow_id=flow_id,
        workspace_id=None,
        now=after_expiry,
    )
    assert metadata[0].id == stored.id
    assert metadata[0].expired is True


def test_exact_replay_survives_flow_terminalization(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow_id, attempt_id = _flow_with_attempt()
    stored = store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id=attempt_id,
        body_text=BODY_A,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW,
    )
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_flows SET state = 'partial_terminal' WHERE id = ?",
            (flow_id,),
        )
        connection.commit()

    replay = store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id=attempt_id,
        body_text=BODY_A,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW + timedelta(minutes=1),
    )
    assert replay == stored
