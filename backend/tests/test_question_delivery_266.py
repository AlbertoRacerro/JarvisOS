"""Spec 266: Deliver maintainer questions even when their format is incomplete.

Focused offline tests for:
- Malformed new questions (including inline options) with no ledger write
- Structured new questions acceptance (2 to 4 options, sequential numbering)
- Legacy unstructured questions snapshot, delivery once, no made-up choices, and free-text answer
- Settled and already-sent behavior
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.modules.coding import relay_monitor

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "devctx.py"
SPEC = importlib.util.spec_from_file_location("devctx", SCRIPT)
assert SPEC and SPEC.loader
devctx = importlib.util.module_from_spec(SPEC)
sys.modules["devctx"] = devctx
SPEC.loader.exec_module(devctx)

ro = relay_monitor.observe()
rc = ro.rc
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
CONFIG = {"bot_token": "xoxb-FAKE-SECRET-1234567890123456", "channel_id": "C123", "maintainer_user_id": "U123"}


@pytest.fixture
def space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, repo = tmp_path / "continuity", tmp_path / "repo"
    root.mkdir()
    repo.mkdir()
    monkeypatch.setenv("JARVIS_CONTROL_DIR", str(tmp_path / "control"))
    monkeypatch.setattr(relay_monitor, "control_root", lambda: root)
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)
    return root, repo


# ---------------------------------------------------------------------------
# Acceptance criterion 1: Validation of new questions in devctx.py note question
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_text,reason", [
    ("Solo una domanda senza alcuna opzione", "no options"),
    ("Domanda con una sola opzione?\n1. Solo questa", "single option"),
    ("Quale approccio preferisci? 1. opzione A 2. opzione B", "inline options on single line"),
    ("Quale approccio preferisci? 1) opzione A 2) opzione B", "inline paren options on single line"),
    ("1. opzione A 2. opzione B", "inline options on single line starting with number"),
    ("Quale approccio?\n1. Prima opzione\n2. Seconda opzione e 3. Terza opzione", "inline option in option line"),
    ("Quale scelta?\n1. Prima/2. Seconda\n2. Terza", "inline option after slash in option line"),
    ("Scegli? 1. Approva\n1. Rimanda\n2. Annulla", "one inline alternative in prompt line"),
    ("Quale scelta?\n1. A, 2)B\n2. C", "unspaced inline paren option in option line"),
    ("Quale approccio?\n2. Parte da due\n3. Seconda", "starts at 2"),
    ("Quale approccio?\n1. Prima\n3. Salta il due", "skips 2"),
    ("Quale approccio?\n1. A\n2. B\n3. C\n4. D\n5. E", "exceeds 4 options"),
    ("Quale approccio?\n1. A\n2. B\nTesto non valido dopo le opzioni", "non-option text after options"),
])
def test_malformed_new_questions_rejected_without_ledger_write(space, capsys, bad_text, reason):
    _root, repo = space
    env = devctx.default_env(repo)
    # Pre-populate ledger with an unrelated note to verify existing entries stay intact
    devctx.append_entry(env, devctx.make_entry(env, "note", "pre-existing note"))
    ledger_before = env.ledger.read_bytes()

    exit_code = devctx.main(["--repo", str(repo), "note", "question", bad_text])
    assert exit_code == 2, f"Expected rejection for {reason}"

    captured = capsys.readouterr()
    assert "devctx:" in captured.err
    # Actionable explanation of required format
    assert "option" in captured.err.lower()

    # Ledger must not have mutated
    assert env.ledger.read_bytes() == ledger_before
    entries = devctx.read_ledger(env)
    assert len(entries) == 1 and entries[0]["kind"] == "note"


@pytest.mark.parametrize("sep", ["/", ":", "|", ";", ",", "-", "—", "–", "\\"])
def test_inline_alternatives_after_separators_rejected_without_ledger_write(space, capsys, sep):
    _root, repo = space
    env = devctx.default_env(repo)
    devctx.append_entry(env, devctx.make_entry(env, "note", "pre-existing note"))
    ledger_before = env.ledger.read_bytes()

    bad_text = f"Quale scelta?\n1. Prima{sep}2. Seconda\n2. Terza"
    exit_code = devctx.main(["--repo", str(repo), "note", "question", bad_text])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "devctx:" in captured.err
    assert "inline alternatives" in captured.err.lower()

    # Ledger must not have mutated
    assert env.ledger.read_bytes() == ledger_before
    entries = devctx.read_ledger(env)
    assert len(entries) == 1 and entries[0]["kind"] == "note"


@pytest.mark.parametrize("prompt", [
    "Dal 2026-11-13. Come procediamo?",
    "La quota torna alle 16:07. Come procediamo?",
    "Serve Python 3.12. Come procediamo?",
])
def test_dates_times_and_versions_in_prompt_are_not_inline_options(prompt):
    devctx.validate_question(f"{prompt}\n1. Procedi\n2. Rimanda")


def test_legacy_question_with_one_inline_alternative_has_no_choices():
    entry = {"id": "L1", "kind": "question", "ts": "2026-11-13T00:00:00Z",
             "text": "Scegli:\n1. Approva ora 2. Rimanda a domani"}
    [question] = ro.parse_open_questions([entry])
    assert question["options"] == []
    assert "Rimanda a domani" in question["prompt"]


def test_structured_new_question_accepted(space, capsys):
    _root, repo = space
    env = devctx.default_env(repo)

    # 2 options with recommended mark and consequences
    q2 = "Quale approccio scegliere?\n1. (consigliata) Approccio rapido — conseguenza: veloce\n2. Approccio lento — conseguenza: accurato"
    ret = devctx.main(["--repo", str(repo), "note", "question", q2])
    assert ret == 0
    qid2 = capsys.readouterr().out.strip()
    assert qid2.startswith("L")

    # 3 options
    q3 = "Come procedere?\n1. Opzione A — conseguenza: a\n2. Opzione B — conseguenza: b\n3. Opzione C — conseguenza: c"
    ret = devctx.main(["--repo", str(repo), "note", "question", q3])
    assert ret == 0
    qid3 = capsys.readouterr().out.strip()

    # 4 options with parenthesis delimiter
    q4 = "Quale database?\n1) SQLite\n2) PostgreSQL\n3) MySQL\n4) Altro"
    ret = devctx.main(["--repo", str(repo), "note", "question", q4])
    assert ret == 0
    qid4 = capsys.readouterr().out.strip()

    entries = devctx.read_ledger(env)
    assert [e["id"] for e in entries] == [qid2, qid3, qid4]
    assert all(e["kind"] == "question" for e in entries)


# ---------------------------------------------------------------------------
# Acceptance criterion 2: Legacy unstructured question snapshot & delivery
# ---------------------------------------------------------------------------


def test_legacy_unstructured_question_in_snapshot_and_delivery_once(space):
    root, repo = space
    env = devctx.default_env(repo)

    # Simulate legacy entries in the ledger that did not follow the option format
    legacy_freeform = devctx.make_entry(
        env, "question", "Blocco legacy: decidere se procedere con il refactoring del parser"
    )
    devctx.append_entry(env, legacy_freeform)

    legacy_inline = devctx.make_entry(
        env, "question", "Scelta architettura: 1. opzione A oppure 2. opzione B"
    )
    devctx.append_entry(env, legacy_inline)

    # And a structured question
    structured = devctx.make_entry(
        env, "question", "Nuova domanda?\n1. (consigliata) Sì — conseguenza: fatto\n2. No — conseguenza: aspetta"
    )
    devctx.append_entry(env, structured)

    # 1. parse_open_questions returns legacy questions with options: []
    open_qs = ro.parse_open_questions(devctx.read_ledger(env))
    assert len(open_qs) == 3
    q_map = {q["id"]: q for q in open_qs}

    assert q_map[legacy_freeform["id"]]["options"] == []
    assert q_map[legacy_freeform["id"]]["prompt"] == "Blocco legacy: decidere se procedere con il refactoring del parser"

    assert q_map[legacy_inline["id"]]["options"] == []
    assert "Scelta architettura" in q_map[legacy_inline["id"]]["prompt"]

    assert len(q_map[structured["id"]]["options"]) == 2
    assert q_map[structured["id"]]["options"][0]["recommended"] is True

    # 2. snapshot exposes all of them in open_questions
    road = {"enabled": True, "paused": None, "authority": "test"}
    rc.write_json_atomic(rc.roadmap_path(root), road)
    snap = ro.snapshot(root, repo, NOW, alive=lambda _p: True, ledger=lambda _r: devctx.read_ledger(env))
    assert len(snap["open_questions"]) == 3

    # 3. question_text produces clean message without made-up choices
    msg_freeform = ro.question_text(q_map[legacy_freeform["id"]])
    assert "Blocco legacy" in msg_freeform
    assert "Puoi rispondere qui in testo libero, nel thread di questa domanda." in msg_freeform
    # No made-up option lines like "1. opzione"
    assert "1." not in msg_freeform

    # 4. observe_step delivers each question once
    sent_questions: list[tuple[str, str]] = []
    obs: dict = {}

    def send_transport(kind: str, text: str) -> bool:
        if kind.startswith("question|"):
            sent_questions.append((kind, text))
        return True

    ro.observe_step(
        obs, snap, NOW, send=send_transport,
        digest_every=timedelta(hours=2), merges=lambda _s: []
    )
    assert len(sent_questions) == 3
    assert set(obs["sent_questions"].keys()) == {legacy_freeform["id"], legacy_inline["id"], structured["id"]}

    # Delivering again does not resend
    ro.observe_step(
        obs, snap, NOW + timedelta(minutes=10), send=send_transport,
        digest_every=timedelta(hours=2), merges=lambda _s: []
    )
    assert len(sent_questions) == 3, "Already sent questions must not be resent"


def test_legacy_unstructured_question_free_text_answer(space):
    root, repo = space
    env = devctx.default_env(repo)
    road = {"enabled": True, "paused": "coordinator_blocked", "authority": "test"}
    rc.write_json_atomic(rc.roadmap_path(root), road)

    legacy = devctx.make_entry(env, "question", "Domanda legacy aperta senza opzioni")
    devctx.append_entry(env, legacy)

    # Free-text answer via record_answer
    result = ro.record_answer(
        root, repo, legacy["id"], "procedi pure con approccio snello",
        source="Jarvis UI", author="maintainer (test)"
    )
    assert result["state"] == "ok"

    # Decision recorded in ledger
    entries = devctx.read_ledger(env)
    decisions = [e for e in entries if e["kind"] == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["supersedes"] == [legacy["id"]]
    assert "procedi pure con approccio snello" in decisions[0]["text"]

    # Settled question is no longer open
    assert ro.parse_open_questions(entries) == []


# ---------------------------------------------------------------------------
# Acceptance criterion 3: Settled question absent, already sent not resent
# ---------------------------------------------------------------------------


def test_settled_question_is_absent_and_previously_sent_not_resent(space):
    root, repo = space
    env = devctx.default_env(repo)
    road = {"enabled": True, "paused": None, "authority": "test"}
    rc.write_json_atomic(rc.roadmap_path(root), road)

    # 1. Historical already-settled question
    q_settled = devctx.make_entry(env, "question", "Domanda già risolta in passato")
    devctx.append_entry(env, q_settled)
    dec = devctx.make_entry(
        env, "decision", f"Risposta alla domanda {q_settled['id']}: ok",
        supersedes=[q_settled["id"]]
    )
    devctx.append_entry(env, dec)

    # 2. Still-open legacy question
    q_open = devctx.make_entry(env, "question", "Domanda ancora aperta")
    devctx.append_entry(env, q_open)

    open_qs = ro.parse_open_questions(devctx.read_ledger(env))
    assert len(open_qs) == 1
    assert open_qs[0]["id"] == q_open["id"]

    # 3. If q_open was already sent previously (tracked in obs["sent_questions"]), it is not resent
    obs = {"sent_questions": {q_open["id"]: "1000.000001"}}
    sent_questions: list[str] = []
    snap = ro.snapshot(root, repo, NOW, alive=lambda _p: True, ledger=lambda _r: devctx.read_ledger(env))

    def send_question_only(kind: str, text: str) -> bool:
        if kind.startswith("question|"):
            sent_questions.append(text)
        return True

    ro.observe_step(
        obs, snap, NOW, send=send_question_only,
        digest_every=timedelta(hours=2), merges=lambda _s: []
    )
    assert len(sent_questions) == 0, "Previously sent question must not be resent"

    # 4. Now add a fresh unsent open question: it gets delivered
    q_fresh = devctx.make_entry(
        env, "question", "Nuova domanda?\n1. (consigliata) A — conseguenza: 1\n2. B — conseguenza: 2"
    )
    devctx.append_entry(env, q_fresh)
    snap = ro.snapshot(root, repo, NOW, alive=lambda _p: True, ledger=lambda _r: devctx.read_ledger(env))

    ro.observe_step(
        obs, snap, NOW, send=send_question_only,
        digest_every=timedelta(hours=2), merges=lambda _s: []
    )
    assert len(sent_questions) == 1
    assert "Nuova domanda?" in sent_questions[0]
    assert q_fresh["id"] in obs["sent_questions"]
