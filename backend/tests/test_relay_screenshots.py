"""Bubu screenshots reach the subscription model through Relay escalation, by explicit approval only."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.ai import thread_service
from app.modules.ai.cloud_escalation import text_digest
from app.modules.relay_gateway import service
from app.modules.relay_gateway.service import RelayEscalationApproval, RelayGatewayError
from tests.test_relay_escalation_161 import QUESTION, _turn
from tests.test_relay_gateway_157 import _wait, gateway  # noqa: F401 - shared fixture


def _png(seed: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + seed.encode() * 8


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _retain(workspace_id: str, interaction_id: str, *raws: bytes) -> list[str]:
    """Retain screenshots exactly as a Bubu turn does, and record them in the turn extras."""
    artifact_ids = thread_service._retain_images(workspace_id, interaction_id, [_data_url(raw) for raw in raws])
    assert len(artifact_ids) == len(raws)
    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO ai_thread_turn_extras "
            "(interaction_id, ui_context_json, image_artifact_ids_json, image_count, images_unsupported) "
            "VALUES (?, NULL, ?, ?, 1)", (interaction_id, json.dumps(artifact_ids), len(raws)))
        connection.commit()
    return artifact_ids


def _approval(text: str = QUESTION, digests: list[str] | None = None) -> RelayEscalationApproval:
    return RelayEscalationApproval(text=text, text_digest=text_digest(text), image_digests=digests or [])


def _task(gateway, index: int = -1) -> str:  # noqa: F811
    argv = gateway["launches"][index]["argv"]
    return argv[argv.index("--task") + 1]


def test_draft_lists_retained_screenshots_and_skips_missing_files(gateway) -> None:  # noqa: F811
    workspace_id, thread_id, interaction_id = _turn("shots-draft")
    plain = service.draft_relay_escalation(workspace_id, thread_id, interaction_id)
    assert (plain.image_count, plain.image_digests) == (0, [])
    first, second = _png("a"), _png("b")
    _retain(workspace_id, interaction_id, first, second, first)
    draft = service.draft_relay_escalation(workspace_id, thread_id, interaction_id)
    assert draft.status == "ready"
    assert (draft.image_count, draft.image_digests) == (2, [_digest(first), _digest(second)])
    with open_sqlite_connection() as connection:
        stored = connection.execute("SELECT stored_path FROM artifacts WHERE sha256 = ?",
                                    (hashlib.sha256(second).hexdigest(),)).fetchone()["stored_path"]
    Path(stored).unlink()
    after = service.draft_relay_escalation(workspace_id, thread_id, interaction_id)
    assert (after.image_count, after.image_digests) == (1, [_digest(first)])


def test_draft_never_lists_another_turns_screenshots(gateway) -> None:  # noqa: F811
    workspace_id, thread_id, interaction_id = _turn("shots-own")
    _, other_thread, other_interaction = _turn("shots-own")
    _retain(workspace_id, other_interaction, _png("other"))
    assert service.draft_relay_escalation(workspace_id, thread_id, interaction_id).image_count == 0
    assert service.draft_relay_escalation(workspace_id, other_thread, other_interaction).image_count == 1


def test_approval_validation_rejects_foreign_and_excess_digests(gateway) -> None:  # noqa: F811
    workspace_id, thread_id, interaction_id = _turn("shots-validate")
    _, _other_thread, other_interaction = _turn("shots-validate")
    mine, theirs = _png("mine"), _png("theirs")
    _retain(workspace_id, interaction_id, mine)
    _retain(workspace_id, other_interaction, theirs)
    with pytest.raises(RelayGatewayError, match="image_not_in_source_turn"):
        service.escalate_with_relay(workspace_id, thread_id, interaction_id, _approval(digests=[_digest(theirs)]))
    with pytest.raises(RelayGatewayError, match="image_not_in_source_turn"):
        service.escalate_with_relay(workspace_id, thread_id, interaction_id,
                                    _approval(digests=[_digest(mine), _digest(_png("invented"))]))
    with pytest.raises(ValidationError):
        _approval(digests=[_digest(_png(str(n))) for n in range(3)])
    with pytest.raises(ValidationError):
        _approval(digests=["not-a-digest"])
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM relay_runs").fetchone()[0] == 0
    assert gateway["launches"] == []


def test_empty_approval_sends_no_images_and_the_prompt_is_unchanged(gateway) -> None:  # noqa: F811
    workspace_id, thread_id, interaction_id = _turn("shots-none")
    _retain(workspace_id, interaction_id, _png("x"))
    run = service.escalate_with_relay(workspace_id, thread_id, interaction_id, _approval())
    done = _wait(workspace_id, thread_id, run.id)
    assert done.state == "completed"
    task = _task(gateway)
    assert "The operator attached" not in task and task.endswith(QUESTION)
    context = gateway["runtime"].root / "context" / (done.relay_workspace_id or "")
    assert not (context / "screenshots").exists() and "screenshots" not in json.loads(
        (context / "MANIFEST.json").read_text(encoding="utf-8"))
    with open_sqlite_connection() as connection:
        stored = connection.execute("SELECT prompt_digest FROM relay_runs WHERE id = ?", (run.id,)).fetchone()
    assert stored["prompt_digest"] == hashlib.sha256(QUESTION.encode("utf-8")).hexdigest()
    # The framed prompt without screenshots is byte-identical to the pre-screenshot wording.
    framed = service._framed_prompt(QUESTION, "/ws", "repository", advisory_only=True)
    assert framed == service._framed_prompt(QUESTION, "/ws", "repository", advisory_only=True, screenshots=[])
    assert "Open each" not in framed


def test_approved_screenshots_are_materialized_and_announced_in_the_prompt(gateway) -> None:  # noqa: F811
    workspace_id, thread_id, interaction_id = _turn("shots-send")
    first, second = _png("one"), _png("two")
    _retain(workspace_id, interaction_id, first, second)
    run = service.escalate_with_relay(workspace_id, thread_id, interaction_id,
                                      _approval(digests=[_digest(second), _digest(first)]))
    done = _wait(workspace_id, thread_id, run.id)
    assert done.state == "completed"
    shots = gateway["runtime"].root / "context" / (done.relay_workspace_id or "") / "screenshots"
    assert sorted(path.name for path in shots.iterdir()) == ["1.png", "2.png"]
    assert (shots / "1.png").read_bytes() == second and (shots / "2.png").read_bytes() == first
    assert stat.S_IMODE((shots / "1.png").stat().st_mode) == 0o600
    task = _task(gateway)
    assert ("The operator attached 2 screenshot(s) of their screen: /workspace/context/screenshots/1.png, "
            "/workspace/context/screenshots/2.png. Open each with your Read tool before answering; "
            "describe only what is relevant.") in task
    assert task.endswith(QUESTION)
    manifest = json.loads((shots.parent / "MANIFEST.json").read_text(encoding="utf-8"))
    assert [item["digest"] for item in manifest["screenshots"]] == [_digest(second), _digest(first)]


def test_old_screenshots_never_leak_into_a_later_run(gateway) -> None:  # noqa: F811
    workspace_id, thread_id, interaction_id = _turn("shots-leak")
    _retain(workspace_id, interaction_id, _png("leak"))
    first = _wait(workspace_id, thread_id, service.escalate_with_relay(
        workspace_id, thread_id, interaction_id, _approval(digests=[_digest(_png("leak"))])).id)
    context = gateway["runtime"].root / "context" / (first.relay_workspace_id or "")
    assert (context / "screenshots" / "1.png").is_file()
    later = _wait(workspace_id, thread_id, service.submit_relay_run(
        workspace_id, thread_id,
        service.RelayRunRequest(prompt="Review the local repository structure.", agent="claude",
                                cloud_safe_attested=True)).id)
    assert later.relay_workspace_id == first.relay_workspace_id
    assert not (context / "screenshots").exists()
    assert "The operator attached" not in _task(gateway)


def test_routes_expose_digests_and_refuse_foreign_or_excess_images(gateway) -> None:  # noqa: F811
    workspace_id, thread_id, interaction_id = _turn("shots-routes")
    mine = _png("route")
    _retain(workspace_id, interaction_id, mine)
    client = TestClient(create_app())
    base = f"/ai/threads/{thread_id}/interactions/{interaction_id}"
    params = {"workspace_id": workspace_id}
    draft = client.post(f"{base}/relay-escalation-draft", params=params).json()
    assert (draft["image_count"], draft["image_digests"]) == (1, [_digest(mine)])
    body = {"text": QUESTION, "text_digest": text_digest(QUESTION)}
    foreign = client.post(f"{base}/relay-escalate", params=params,
                          json={**body, "image_digests": [_digest(_png("foreign"))]})
    assert foreign.status_code == 409 and foreign.json()["detail"]["code"] == "image_not_in_source_turn"
    excess = client.post(f"{base}/relay-escalate", params=params,
                         json={**body, "image_digests": [_digest(_png(str(n))) for n in range(3)]})
    assert excess.status_code == 422
    assert gateway["launches"] == []
    sent = client.post(f"{base}/relay-escalate", params=params, json=body)
    assert sent.status_code == 200
    _wait(workspace_id, thread_id, sent.json()["id"])


def test_a_different_image_set_is_not_a_replay(gateway) -> None:  # noqa: F811
    workspace_id, thread_id, interaction_id = _turn("shots-replay")
    first, second = _png("r1"), _png("r2")
    _retain(workspace_id, interaction_id, first, second)
    approval = _approval(digests=[_digest(first)])
    run = service.escalate_with_relay(workspace_id, thread_id, interaction_id, approval)
    completed = _wait(workspace_id, thread_id, run.id)
    replay = service.escalate_with_relay(workspace_id, thread_id, interaction_id, approval)
    assert replay.id == completed.id and len(gateway["launches"]) == 1
    for other in (_approval(digests=[_digest(second)]), _approval(digests=[_digest(first), _digest(second)]), _approval()):
        with pytest.raises(RelayGatewayError, match="relay_escalation_digest_conflict"):
            service.escalate_with_relay(workspace_id, thread_id, interaction_id, other)
    assert len(gateway["launches"]) == 1
    with open_sqlite_connection() as connection:
        stored = connection.execute("SELECT prompt_digest FROM relay_runs WHERE id = ?", (run.id,)).fetchone()
    assert stored["prompt_digest"] != hashlib.sha256(QUESTION.encode("utf-8")).hexdigest()
    assert service._run_prompt_digest(QUESTION, []) == hashlib.sha256(QUESTION.encode("utf-8")).hexdigest()
