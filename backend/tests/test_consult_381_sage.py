"""Tests for Spec 381: Sage local consultant path, per-turn batched swap, fallback, and bounded 2-round exchanges."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.core import packs, vault
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.consult.config import ConsultConfig, TrustedAccount
from app.modules.consult.render import render_consult_blocks
from app.modules.consult.sage import (
    FAKE_SAGE_ENV,
    clear_sage_queue,
    flush_sage_queue,
    get_queued_sage_consults,
    is_sage_available,
)
from app.modules.consult.service import (
    ConsultRequest,
    request_consult,
)
from app.modules.local_ai.runtime import model_registry
from tests.test_consult_380_flow import BRIEF, DIGEST

ACCOUNT_1 = TrustedAccount(
    id="acc1",
    agent="claude",
    model="fake-primary",
    training_off_verified_on=date(2026, 11, 25),
)
ACCOUNT_2 = TrustedAccount(
    id="acc2",
    agent="claude",
    model="fake-secondary",
    training_off_verified_on=date(2026, 11, 25),
)


class FakeTransportWithQuota:
    def __init__(self, failing_accounts: set[str] | None = None, answer: str = "Expert answer from transport.") -> None:
        self.failing_accounts = failing_accounts or set()
        self.answer = answer
        self.sent: list[tuple[str, str]] = []

    def send(self, packet: str, account: TrustedAccount, *, consult_id: str | None = None) -> str:
        if account.id in self.failing_accounts:
            raise RuntimeError(f"Quota exceeded for account {account.id}")
        self.sent.append((account.id, packet))
        return self.answer


@pytest.fixture()
def sage_env(isolated_data_root: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    clear_sage_queue()
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "brief.md").write_text(BRIEF, encoding="utf-8")
    (pack / "jarvis_pack.toml").write_text(
        'id = "orchard"\n[sensitivity]\nterms = ["secretfruit"]\n'
        '[search_guard]\nmin_groups = 2\nexact_combinations = [["root", "fungus"]]\n'
        'groups = [["soil", "dirt"], ["tree", "apple"]]\n'
        '[consult]\nbrief = "brief.md"\nnucleus_heading = "Nucleus"\n',
        encoding="utf-8",
    )
    v = tmp_path / "vault"
    v.mkdir()
    (v / "jarvis_vault.toml").write_text(
        '[[local_only]]\nid = "L1"\ntext = "proprietary irrigation nozzle geometry"\n\n'
        '[[protected_propositions]]\nid = "P1"\ntext = "subterranean moisture sensor array"\n',
        encoding="utf-8",
    )
    (v / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\nsage_model_id = "sage"\n'
        '[[trusted_accounts]]\nid = "acc1"\nagent = "claude"\nmodel = "fake-primary"\ntraining_off_verified_on = 2026-11-25\n'
        '[[trusted_accounts]]\nid = "acc2"\nagent = "claude"\nmodel = "fake-secondary"\ntraining_off_verified_on = 2026-11-25\n',
        encoding="utf-8",
    )
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    registry_file = settings_dir / "local_models.json"
    choice_file = settings_dir / "local_model_choice.json"

    import hashlib
    sha_inkling = hashlib.sha256(b"fake-inkling").hexdigest()
    sha_sage = hashlib.sha256(b"fake-sage").hexdigest()

    models = [
        {
            "id": "inkling",
            "label": "Inkling",
            "runtime": "llama_cpp",
            "qualification": "default",
            "model_path": str((tmp_path / "inkling.gguf").resolve()),
            "model_sha256": sha_inkling,
        },
        {
            "id": "sage",
            "label": "Sage",
            "runtime": "llama_cpp",
            "qualification": "qualified",
            "model_path": str((tmp_path / "sage.gguf").resolve()),
            "model_sha256": sha_sage,
        },
    ]
    (tmp_path / "inkling.gguf").write_text("fake-inkling-model", encoding="utf-8")
    (tmp_path / "sage.gguf").write_text("fake-sage-model", encoding="utf-8")
    registry_file.write_text(json.dumps({"models": models}), encoding="utf-8")
    choice_file.write_text(json.dumps({"model": "inkling"}), encoding="utf-8")

    from types import SimpleNamespace
    paths = SimpleNamespace(
        data_root=tmp_path,
        local_models_file=registry_file,
        local_model_choice_file=choice_file,
    )
    monkeypatch.setattr(model_registry, "build_paths", lambda: paths)
    monkeypatch.setattr("app.core.paths.build_paths", lambda: paths)
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path))

    from app.modules.consult.judge import FAKE_JUDGE_ENV
    monkeypatch.setenv(FAKE_JUDGE_ENV, json.dumps({"hits": [], "reason": "No local items."}))
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack))
    monkeypatch.setenv(vault.VAULT_PATH_ENV, str(v))
    monkeypatch.setenv("JARVISOS_CONSULT_FAKE_TRANSPORT", "1")
    monkeypatch.setenv(FAKE_SAGE_ENV, "Risposta di Sage con ragionamento approfondito.")

    packs._load.cache_clear()
    vault._load.cache_clear()
    initialize_database()
    with open_sqlite_connection() as conn:
        yield conn
    packs._load.cache_clear()
    vault._load.cache_clear()
    clear_sage_queue()


def test_sage_unavailable_fallback(sage_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When sage_model_id is missing, unknown, or not host-startable, request returns sage_unavailable."""
    # 1. Unknown sage model id in consult.toml
    v = tmp_path / "vault"
    (v / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\nsage_model_id = "nonexistent_model"\n'
        '[[trusted_accounts]]\nid = "acc1"\nagent = "claude"\nmodel = "fake-primary"\ntraining_off_verified_on = 2026-11-25\n',
        encoding="utf-8",
    )
    assert not is_sage_available("nonexistent_model")

    req = ConsultRequest(kind="ideas", question="How to prune trees?", modules=["M1"], local_only=True)
    outcome = request_consult(sage_env, req)
    assert outcome.status == "sage_unavailable"


def test_single_batched_swap_for_multiple_queued_questions(sage_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two Sage questions in one turn perform exactly one swap to Sage and back, without overwriting choice file."""
    # Track activate_model calls
    swap_calls: list[tuple[str, bool]] = []

    def mock_activate(model_id: str, persist_choice: bool = True) -> dict[str, Any]:
        swap_calls.append((model_id, persist_choice))
        return {"status": "ok", "active_model_id": model_id}

    monkeypatch.setattr(model_registry, "activate_model", mock_activate)

    # 1. Enqueue two local_only requests during the same turn
    turn_id = "hermes-turn-42"
    req1 = ConsultRequest(kind="ideas", question="Question 1 for Sage?", modules=["M1"], local_only=True, turn_id=turn_id)
    req2 = ConsultRequest(kind="ideas", question="Question 2 for Sage?", modules=["M1"], local_only=True, turn_id=turn_id)

    outcome1 = request_consult(sage_env, req1)
    outcome2 = request_consult(sage_env, req2)

    assert outcome1.status == "queued_for_sage"
    assert outcome2.status == "queued_for_sage"
    assert outcome1.consult_id is not None
    assert outcome2.consult_id is not None

    queued = get_queued_sage_consults(turn_id)
    assert len(queued) == 2

    # 2. Flush the queue at turn end
    sage_env.commit()
    results = flush_sage_queue(turn_id)
    assert len(results) == 2

    # 3. Verify exactly one swap to Sage and one swap back to Inkling (total 2 swaps)
    assert len(swap_calls) == 2
    assert swap_calls[0] == ("sage", False)
    assert swap_calls[1] == ("inkling", False)

    # 4. Verify persist_choice=False preserved local_model_choice.json
    choice_path = model_registry.build_paths().local_model_choice_file
    choice_data = json.loads(choice_path.read_text(encoding="utf-8"))
    assert choice_data["model"] == "inkling"

    # 5. Verify both consults were answered and recorded in DB
    for cid in (outcome1.consult_id, outcome2.consult_id):
        row = sage_env.execute("SELECT state, account_id, model_id, ai_job_id FROM consult_attempts WHERE id = ?", (cid,)).fetchone()
        assert row is not None
        assert row[0] == "sent"
        assert row[1] == "sage_local"
        assert row[2] == "sage"
        assert row[3] is not None

        ans_row = sage_env.execute("SELECT answer_text FROM consult_answers WHERE attempt_id = ?", (cid,)).fetchone()
        assert ans_row is not None
        assert "Risposta di Sage" in ans_row[0]


def test_finally_reload_inkling_on_error(sage_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """When an error occurs during Sage execution, original model is always restored in finally:."""
    swap_calls: list[tuple[str, bool]] = []

    def mock_activate(model_id: str, persist_choice: bool = True) -> dict[str, Any]:
        swap_calls.append((model_id, persist_choice))
        return {"status": "ok", "active_model_id": model_id}

    monkeypatch.setattr(model_registry, "activate_model", mock_activate)

    # Force run_ai_task to raise
    def mock_run_ai_task(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("GPU execution failed")

    monkeypatch.setattr("app.modules.consult.sage.run_ai_task", mock_run_ai_task)

    turn_id = "hermes-turn-err"
    req = ConsultRequest(kind="ideas", question="Will fail?", modules=["M1"], local_only=True, turn_id=turn_id)
    out = request_consult(sage_env, req)
    assert out.status == "queued_for_sage"

    with pytest.raises(RuntimeError, match="GPU execution failed"):
        flush_sage_queue(turn_id, connection=sage_env)

    # Inkling reload must have been called in finally:
    assert len(swap_calls) == 2
    assert swap_calls[0] == ("sage", False)
    assert swap_calls[1] == ("inkling", False)


def test_swap_time_recorded_in_gate_steps(sage_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured swap timings (swap_to_sage_s, swap_back_s, total_swap_s) are recorded in gate_steps_json."""
    monkeypatch.setattr(model_registry, "activate_model", lambda m, persist_choice=True: {})

    turn_id = "hermes-turn-timing"
    req = ConsultRequest(kind="ideas", question="Check swap timing?", modules=["M1"], local_only=True, turn_id=turn_id)
    out = request_consult(sage_env, req)
    assert out.status == "queued_for_sage"

    flush_sage_queue(turn_id, connection=sage_env)

    row = sage_env.execute("SELECT gate_steps_json FROM consult_attempts WHERE id = ?", (out.consult_id,)).fetchone()
    assert row is not None
    steps = json.loads(row[0])
    swap_step = next((s for s in steps if s.get("step") == "sage_swap"), None)
    assert swap_step is not None
    assert swap_step["result"] == "pass"
    assert swap_step["swap_to_sage_s"] > 0
    assert swap_step["swap_back_s"] > 0
    assert swap_step["total_swap_s"] > 0

    # Test rendering includes swap_timing
    blocks = render_consult_blocks(sage_env, turn_id, f"{{{{consult:{out.consult_id}}}}}")
    assert len(blocks) == 1
    assert blocks[0].swap_timing is not None
    assert blocks[0].swap_timing["total_swap_s"] > 0


def test_ordered_trusted_account_fallback_to_second_account(sage_env: sqlite3.Connection) -> None:
    """When account 1 hits quota error, transport tries account 2 automatically."""
    transport = FakeTransportWithQuota(failing_accounts={"acc1"}, answer="Answer from account 2.")

    cfg = lambda: ConsultConfig(  # noqa: E731
        mode="auto",
        brief_sha256=DIGEST,
        trusted_accounts=(ACCOUNT_1, ACCOUNT_2),
        sage_model_id="sage",
    )

    req = ConsultRequest(kind="ideas", question="General question for zone A?", modules=["M1"])
    outcome = request_consult(sage_env, req, transport=transport, config_loader=cfg)
    assert outcome.status == "answered"
    assert outcome.answer == "Answer from account 2."

    # Verify attempt recorded account_id as acc2
    row = sage_env.execute("SELECT account_id, gate_steps_json FROM consult_attempts WHERE id = ?", (outcome.consult_id,)).fetchone()
    assert row is not None
    assert row[0] == "acc2"
    steps = json.loads(row[1])
    failed_step = next((s for s in steps if s.get("step") == "transport_attempt" and s.get("account_id") == "acc1"), None)
    assert failed_step is not None
    assert failed_step["result"] == "refused"


def test_ordered_trusted_account_fallback_all_fail_to_sage(sage_env: sqlite3.Connection) -> None:
    """When all trusted accounts fail transport, attempt falls back to Sage."""
    transport = FakeTransportWithQuota(failing_accounts={"acc1", "acc2"})

    cfg = lambda: ConsultConfig(  # noqa: E731
        mode="auto",
        brief_sha256=DIGEST,
        trusted_accounts=(ACCOUNT_1, ACCOUNT_2),
        sage_model_id="sage",
    )

    req = ConsultRequest(kind="ideas", question="All accounts will fail?", modules=["M1"], turn_id="t-fallback")
    outcome = request_consult(sage_env, req, transport=transport, config_loader=cfg)
    assert outcome.status == "queued_for_sage"

    row = sage_env.execute("SELECT state, local_only, account_id, model_id FROM consult_attempts WHERE id = ?", (outcome.consult_id,)).fetchone()
    assert row is not None
    assert row[0] == "approved"
    assert row[1] == 1
    assert row[2] == "sage_local"
    assert row[3] == "sage"


def test_ordered_trusted_account_fallback_all_fail_zone_a_unavailable(sage_env: sqlite3.Connection) -> None:
    """When all trusted accounts fail and Sage is not configured, returns zone_a_unavailable."""
    transport = FakeTransportWithQuota(failing_accounts={"acc1", "acc2"})

    cfg = lambda: ConsultConfig(  # noqa: E731
        mode="auto",
        brief_sha256=DIGEST,
        trusted_accounts=(ACCOUNT_1, ACCOUNT_2),
        sage_model_id=None,
    )

    req = ConsultRequest(kind="ideas", question="All accounts fail and no sage?", modules=["M1"])
    outcome = request_consult(sage_env, req, transport=transport, config_loader=cfg)
    assert outcome.status == "refused"
    assert outcome.code == "zone_a_unavailable"

    row = sage_env.execute("SELECT state, result_code FROM consult_attempts WHERE id = ?", (outcome.consult_id,)).fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] == "zone_a_unavailable"


def test_2_round_bounded_exchange_and_round_3_refusal(sage_env: sqlite3.Connection) -> None:
    """reply_to allows round 2 through full gate and refuses round 3 with exchange_limit_reached."""
    transport = FakeTransportWithQuota(answer="Initial expert advice.")
    cfg = lambda: ConsultConfig(  # noqa: E731
        mode="auto",
        brief_sha256=DIGEST,
        trusted_accounts=(ACCOUNT_1,),
        sage_model_id="sage",
    )

    # 1. Round 1 consult
    req1 = ConsultRequest(kind="ideas", question="Initial question for round 1?", modules=["M1"], thread_id="th1", workspace_id="ws1")
    out1 = request_consult(sage_env, req1, transport=transport, config_loader=cfg)
    assert out1.status == "answered"
    round_1_id = out1.consult_id
    assert round_1_id is not None

    # 2. Round 2 consult with reply_to
    req2 = ConsultRequest(
        kind="ideas",
        question="Follow-up question based on round 1?",
        modules=["M1"],
        reply_to=round_1_id,
        thread_id="th1",
        workspace_id="ws1",
    )
    out2 = request_consult(sage_env, req2, transport=transport, config_loader=cfg)
    row = sage_env.execute("SELECT gate_steps_json FROM consult_attempts WHERE id = ?", (out2.consult_id,)).fetchone()
    steps_debug = json.loads(row[0]) if row and row[0] else []
    assert out2.status == "answered", f"out2 status={out2.status} code={out2.code} steps={steps_debug}"
    round_2_id = out2.consult_id
    assert round_2_id is not None

    # Check that outbound packet for round 2 contains round 1 summary
    row2 = sage_env.execute("SELECT packet_text, gate_steps_json FROM consult_attempts WHERE id = ?", (round_2_id,)).fetchone()
    assert row2 is not None
    assert f"[Round 1 exchange: {round_1_id}]" in row2[0]
    steps2 = json.loads(row2[1])
    exchange_step = next((s for s in steps2 if s.get("step") == "exchange"), None)
    assert exchange_step is not None
    assert exchange_step["round"] == 2
    assert exchange_step["reply_to"] == round_1_id

    # 3. Round 3 attempt pointing to round 2 -> refuses exchange_limit_reached
    req3 = ConsultRequest(
        kind="ideas",
        question="Attempting third round?",
        modules=["M1"],
        reply_to=round_2_id,
        thread_id="th1",
        workspace_id="ws1",
    )
    out3 = request_consult(sage_env, req3, transport=transport, config_loader=cfg)
    assert out3.status == "refused"
    assert out3.code == "exchange_limit_reached"

    # 4. Invalid reply_to (non-existent id)
    req_bad = ConsultRequest(
        kind="ideas",
        question="Invalid reply?",
        modules=["M1"],
        reply_to="consult-nonexistent",
        thread_id="th1",
        workspace_id="ws1",
    )
    out_bad = request_consult(sage_env, req_bad, transport=transport, config_loader=cfg)
    assert out_bad.status == "refused"
    assert out_bad.code == "reply_target_invalid"


def test_sage_activation_failure_never_answers_in_sages_name(
    sage_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Sage cannot be loaded, the queued attempt fails; the still-loaded model never answers for it."""
    swap_calls: list[str] = []

    def mock_activate(model_id: str, persist_choice: bool = True) -> dict[str, Any]:
        swap_calls.append(model_id)
        if model_id == "sage":
            raise RuntimeError("no gpu memory")
        return {"status": "ok"}

    def must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no answer may be produced without Sage loaded")

    monkeypatch.setattr(model_registry, "activate_model", mock_activate)
    monkeypatch.setattr("app.modules.consult.sage.run_ai_task", must_not_run)
    req = ConsultRequest(kind="ideas", question="Needs Sage?", modules=["M1"], local_only=True, turn_id="t-act")
    out = request_consult(sage_env, req)
    assert out.status == "queued_for_sage"
    assert flush_sage_queue("t-act", connection=sage_env) == []
    assert swap_calls == ["sage", "inkling"]
    row = sage_env.execute("SELECT state FROM consult_attempts WHERE id = ?", (out.consult_id,)).fetchone()
    assert row[0] == "failed"
    assert sage_env.execute("SELECT COUNT(*) FROM consult_answers WHERE attempt_id = ?",
                            (out.consult_id,)).fetchone()[0] == 0


def test_sage_round_one_never_feeds_a_zone_a_round_two(sage_env: sqlite3.Connection) -> None:
    """A round 1 answered locally by Sage cannot be quoted into a zone-A round 2."""
    transport = FakeTransportWithQuota(answer="unused")
    cfg = lambda: ConsultConfig(  # noqa: E731
        mode="auto", brief_sha256=DIGEST, trusted_accounts=(ACCOUNT_1,), sage_model_id="sage")
    sage_env.execute(
        """
        INSERT INTO consult_attempts (id, turn_id, kind, local_only, modules_json, packet_sha256, account_id, model_id,
            state, mode, gate_steps_json, audit, created_at, updated_at)
        VALUES ('consult-sage-r1', 't1', 'ideas', 1, '["M1"]', 'x', 'sage_local', 'sage', 'sent', 'auto', '[]', 0,
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """
    )
    sage_env.commit()
    req = ConsultRequest(kind="ideas", question="Follow-up?", modules=["M1"], reply_to="consult-sage-r1")
    out = request_consult(sage_env, req, transport=transport, config_loader=cfg)
    assert out.status == "refused" and out.code == "reply_target_local_only"


def _strata_entry(model_id: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=model_id, runtime="strata", host="127.0.0.1", port=18090, served_model=model_id)


def test_wait_strata_ready_polls_until_the_model_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.modules.consult import sage
    from app.modules.local_ai.runtime import host_start

    states = iter(["loading", "loading", "ready"])
    calls: list[str] = []

    class _Controller:
        def get_state(self, model_id: str, host: str, port: int, served_model: str) -> Any:
            calls.append(model_id)
            return SimpleNamespace(state=next(states))

    monkeypatch.setattr(model_registry, "read_registry", lambda: ([_strata_entry("sage-x")], True))
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: _Controller())
    monkeypatch.setattr(sage, "STRATA_READY_POLL_S", 0.0)

    assert sage._wait_strata_ready("sage-x") is True
    assert calls == ["sage-x", "sage-x", "sage-x"]


def test_wait_strata_ready_fails_closed_on_failed_or_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.modules.consult import sage
    from app.modules.local_ai.runtime import host_start

    current = {"state": "failed"}

    class _Controller:
        def get_state(self, model_id: str, host: str, port: int, served_model: str) -> Any:
            return SimpleNamespace(state=current["state"])

    monkeypatch.setattr(model_registry, "read_registry", lambda: ([_strata_entry("sage-x")], True))
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: _Controller())
    monkeypatch.setattr(sage, "STRATA_READY_POLL_S", 0.0)

    assert sage._wait_strata_ready("sage-x") is False
    current["state"] = "loading"
    assert sage._wait_strata_ready("sage-x", timeout_s=0.0) is False
    # Non-Strata models are ready once activation returns.
    assert sage._wait_strata_ready("unknown-model") is True
