"""Spec 380 part a: consult attempts, loop guard, rewrite cap, ask/auto, approval, edit and ledger (fake transport)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from app.core import packs
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.consult.config import ConsultConfig, TrustedAccount
from app.modules.consult.service import (
    ConsultRequest,
    approve_consult,
    edit_consult,
    read_answer,
    reject_consult,
    request_consult,
)

BRIEF = """# Fake brief

## Nucleus

You are a consultant for an orchard research project.

## Modules

**M1 — Soil.** Questions about soil and roots.

**M2 — Water.** Questions about irrigation.
"""
DIGEST = hashlib.sha256(BRIEF.encode()).hexdigest()
ACCOUNT = TrustedAccount(id="sub", agent="claude", model="fake-consultant", training_off_verified_on=date(2026, 11, 25))


class FakeTransport:
    def __init__(self, answer: str = "Consider root depth first.\nQuestions: none.") -> None:
        self.answer = answer
        self.sent: list[str] = []

    def send(self, packet: str, account: TrustedAccount) -> str:
        self.sent.append(packet)
        return self.answer


def _config(mode: str = "ask", digest: str | None = DIGEST, accounts: tuple = (ACCOUNT,)):
    return lambda: ConsultConfig(mode=mode, brief_sha256=digest, trusted_accounts=accounts)  # type: ignore[arg-type]


@pytest.fixture()
def connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "brief.md").write_text(BRIEF, encoding="utf-8")
    (pack / "jarvis_pack.toml").write_text(
        'id = "orchard"\n[consult]\nbrief = "brief.md"\nnucleus_heading = "Nucleus"\n', encoding="utf-8")
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack))
    packs._load.cache_clear()
    initialize_database()
    with open_sqlite_connection() as conn:
        yield conn
    packs._load.cache_clear()


def _request(**overrides) -> ConsultRequest:
    values = {"kind": "physics", "question": "Why do roots grow towards water?", "modules": ["M1"],
              "context": "An indicative description.", "turn_id": "turn-1"}
    values.update(overrides)
    return ConsultRequest(**values)


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM consult_attempts ORDER BY created_at, rowid").fetchall()


@pytest.mark.parametrize(("loader", "code"), [
    (_config(mode="off"), "consult_off"),
    (_config(accounts=()), "no_trusted_account"),
    (_config(digest=None), "brief_not_approved"),
    (_config(digest="0" * 64), "brief_not_approved"),
])
def test_unusable_config_refuses_and_sends_nothing(connection, loader, code) -> None:
    transport = FakeTransport()
    outcome = request_consult(connection, _request(), transport=transport, config_loader=loader)
    assert (outcome.status, outcome.code) == ("refused", code)
    assert transport.sent == []
    assert _rows(connection)[0]["state"] == "refused"


def test_without_transport_any_mode_refuses_transport_unavailable(connection) -> None:
    outcome = request_consult(connection, _request(), transport=None, config_loader=_config("auto"))
    assert outcome.code == "transport_unavailable"


def test_argument_checks_local_only_and_unknown_module(connection) -> None:
    t, cfg = FakeTransport(), _config()
    assert request_consult(connection, _request(kind="compute"), transport=t, config_loader=cfg).code == "kind_unknown"
    assert request_consult(connection, _request(question="x" * 1501, turn_id="t2"), transport=t,
                           config_loader=cfg).code == "question_invalid"
    assert request_consult(connection, _request(modules=["M1", "M2", "M3"], turn_id="t3"), transport=t,
                           config_loader=cfg).code == "modules_invalid"
    assert request_consult(connection, _request(modules=["M9"], turn_id="t4"), transport=t,
                           config_loader=cfg).code == "module_unknown"
    local = request_consult(connection, _request(local_only=True, turn_id="t5"), transport=t, config_loader=cfg)
    assert local.status == "sage_unavailable"
    assert t.sent == []


def test_ask_mode_waits_then_approval_sends_the_exact_packet(connection) -> None:
    transport = FakeTransport()
    pending = request_consult(connection, _request(), transport=transport, config_loader=_config())
    assert pending.status == "pending" and f"{{{{consult:{pending.consult_id}}}}}" in pending.reason
    assert transport.sent == []
    row = _rows(connection)[0]
    assert row["state"] == "awaiting_approval" and row["sanitizer_kind"] == "model_local"
    assert row["brief_sha256"] == DIGEST and json.loads(row["modules_json"]) == ["M1"]
    steps = [s["step"] for s in json.loads(row["gate_steps_json"])]
    assert steps == ["secrets_and_identity", "number_taint", "files_and_verbatim"]
    packet = row["packet_text"]
    assert "Questions about soil" in packet and "irrigation" not in packet and "Why do roots" in packet

    wrong = approve_consult(connection, pending.consult_id, "f" * 64, transport=transport, config_loader=_config())
    assert wrong.code == "digest_mismatch" and transport.sent == []
    done = approve_consult(connection, pending.consult_id, row["packet_sha256"], transport=transport,
                           config_loader=_config())
    assert done.status == "answered" and transport.sent == [packet]
    replay = approve_consult(connection, pending.consult_id, row["packet_sha256"], transport=transport,
                             config_loader=_config())
    assert replay.code == "not_awaiting_approval" and len(transport.sent) == 1
    final = _rows(connection)[0]
    assert final["state"] == "sent" and final["approval"] == "operator"
    answer = read_answer(connection, pending.consult_id)
    assert answer["untrusted"] is True and answer["text"] == transport.answer
    assert answer["answer_sha256"] == hashlib.sha256(transport.answer.encode()).hexdigest()


def test_rejection_sends_nothing(connection) -> None:
    transport = FakeTransport()
    pending = request_consult(connection, _request(), transport=transport, config_loader=_config())
    assert reject_consult(connection, pending.consult_id).status == "rejected"
    assert reject_consult(connection, pending.consult_id).code == "not_awaiting_approval"
    assert _rows(connection)[0]["state"] == "rejected" and transport.sent == []


def test_edit_reruns_the_gate_and_keeps_rows_append_only(connection) -> None:
    transport = FakeTransport()
    pending = request_consult(connection, _request(), transport=transport, config_loader=_config())
    secret = edit_consult(connection, pending.consult_id, "Why?", "mail me at someone@example.org",
                          config_loader=_config())
    assert secret.status == "refused" and secret.code == "personal_email"
    refused_row = connection.execute("SELECT packet_text FROM consult_attempts WHERE id = ?",
                                     (secret.consult_id,)).fetchone()
    assert refused_row["packet_text"] is None
    edited = edit_consult(connection, pending.consult_id, "Why do roots seek water, briefly?", "",
                          config_loader=_config())
    assert edited.status == "pending" and edited.consult_id != pending.consult_id
    states = {r["id"]: r["state"] for r in _rows(connection)}
    assert states[pending.consult_id] == "rejected" and states[edited.consult_id] == "awaiting_approval"
    new_sha = connection.execute("SELECT packet_sha256 FROM consult_attempts WHERE id = ?",
                                 (edited.consult_id,)).fetchone()[0]
    done = approve_consult(connection, edited.consult_id, new_sha, transport=transport, config_loader=_config())
    assert done.status == "answered" and "briefly?" in transport.sent[0]


def test_auto_mode_sends_at_once_and_flags_about_one_in_five(connection) -> None:
    transport = FakeTransport()
    flags = []
    for index in range(40):
        outcome = request_consult(connection, _request(question=f"Why do roots grow, variant {chr(65 + index % 26)}"
                                                       f"{chr(65 + index // 26)}?", turn_id=f"auto-{index}"),
                                  transport=transport, config_loader=_config("auto"))
        assert outcome.status == "answered"
        flags.append(_rows(connection)[-1]["audit"])
    assert len(transport.sent) == 40
    assert all(r["approval"] == "policy_auto" for r in _rows(connection))
    assert 1 <= sum(flags) <= 20


def test_taint_rewrite_cap_and_loop_guard(connection) -> None:
    transport = FakeTransport()
    tainted = _request(question="Is a drop of 240 Pa plausible?", tool_outputs=("pressure drop 236.8 Pa",))
    first = request_consult(connection, tainted, transport=transport, config_loader=_config())
    assert first.status == "rewrite_required" and first.offending == ("240",)
    assert request_consult(connection, tainted, transport=transport, config_loader=_config()).status == \
        "rewrite_required"
    third = request_consult(connection, tainted, transport=transport, config_loader=_config())
    assert third.status == "sage_unavailable"
    ok = request_consult(connection, _request(question="Is a drop of 100-300 Pa plausible?"), transport=transport,
                         config_loader=_config())
    assert ok.status == "pending"
    assert request_consult(connection, _request(), transport=transport, config_loader=_config()).status == "pending"
    guarded = request_consult(connection, _request(), transport=transport, config_loader=_config())
    assert guarded.code == "loop_guard"
    assert transport.sent == []
    rows = _rows(connection)
    assert len(rows) == 6 and all(json.loads(r["gate_steps_json"]) is not None for r in rows)


def test_stored_parameter_numbers_taint(connection) -> None:
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO parameters (id, workspace_id, name, value, unit, created_at, updated_at) "
        "VALUES ('p1', 'ws', 'drop', '417.3', 'Pa', '2026-11-26T00:00:00Z', '2026-11-26T00:00:00Z')")
    transport = FakeTransport()
    tainted = request_consult(connection, _request(question="Is 415 Pa a plausible drop?"), transport=transport,
                              config_loader=_config())
    assert tainted.status == "rewrite_required" and tainted.offending == ("415",)
    ranged = request_consult(connection, _request(question="Is a 200-800 Pa drop plausible?"), transport=transport,
                             config_loader=_config())
    assert ranged.status == "pending"


def test_s4_refusal_stores_only_hash_and_reason(connection) -> None:
    transport = FakeTransport()
    outcome = request_consult(connection, _request(context="key sk-ant-api03-" + "x" * 40), transport=transport,
                              config_loader=_config())
    assert outcome.code == "secret_detected"
    row = _rows(connection)[0]
    assert row["packet_text"] is None and len(row["packet_sha256"]) == 64 and row["result_code"] == "secret_detected"
    assert "sk-ant" not in json.dumps([dict(r) for r in _rows(connection)])
