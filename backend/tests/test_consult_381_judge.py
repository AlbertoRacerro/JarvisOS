"""Tests for Spec 381: local_only deterministic pre-check, local judge via run_ai_task, fail-closed rules, and edit_consult."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from app.core import packs, vault
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.consult.config import TrustedAccount
from app.modules.consult.judge import FAKE_JUDGE_ENV, JudgeOutcome, run_local_judge
from app.modules.consult.service import (
    ConsultRequest,
    edit_consult,
    request_consult,
)
from app.modules.consult.transport import DEFAULT_CONSULT_TRANSPORT
from tests.test_consult_380_flow import BRIEF, DIGEST

ACCOUNT = TrustedAccount(
    id="sub",
    agent="claude",
    model="fake-consultant",
    training_off_verified_on=date(2026, 11, 25),
)


@pytest.fixture()
def judge_env(isolated_data_root: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
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
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\n[[trusted_accounts]]\nid = "sub"\nagent = "claude"\n'
        'model = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack))
    monkeypatch.setenv(vault.VAULT_PATH_ENV, str(v))
    monkeypatch.setenv("JARVISOS_CONSULT_FAKE_TRANSPORT", "1")
    packs._load.cache_clear()
    vault._load.cache_clear()
    initialize_database()
    with open_sqlite_connection() as conn:
        yield conn
    packs._load.cache_clear()
    vault._load.cache_clear()


def test_deterministic_precheck_vault_literals(judge_env: sqlite3.Connection) -> None:
    """Outbound text matching vault local_only or protected_propositions literals returns rewrite_required with IDs only."""
    # Test L1 literal match
    outcome = run_local_judge("How does proprietary irrigation nozzle geometry affect spray?", "context here")
    assert outcome.status == "rewrite_required"
    assert outcome.code == "local_only"
    assert "L1" in outcome.hits
    # Secret text must NOT be echoed in reason
    assert "proprietary irrigation nozzle geometry" not in (outcome.reason or "")
    assert "L1" in (outcome.reason or "")

    # Test P1 literal match
    outcome2 = run_local_judge("Could we use a subterranean moisture sensor array?", "")
    assert outcome2.status == "rewrite_required"
    assert "P1" in outcome2.hits
    assert "subterranean moisture sensor array" not in (outcome2.reason or "")
    assert "P1" in (outcome2.reason or "")


def test_deterministic_precheck_pack_search_guard(judge_env: sqlite3.Connection) -> None:
    """Outbound text matching pack exact_combinations or min_groups triggers rewrite_required."""
    # Pack exact combination: ["root", "fungus"]
    outcome = run_local_judge("How does root interaction with fungus develop?", "")
    assert outcome.status == "rewrite_required"
    assert outcome.code == "local_only"
    assert "exact_combination" in outcome.hits

    # Pack min_groups: min_groups = 2, group 1 has "soil", group 2 has "apple"
    outcome2 = run_local_judge("Analyzing soil quality near the apple orchard", "")
    assert outcome2.status == "rewrite_required"
    assert outcome2.code == "local_only"
    assert "min_groups" in outcome2.hits


def test_vault_fail_closed_missing_or_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing or unreadable vault fails closed (vault_absent / gate_error), never passes."""
    # 1. Unset vault path
    monkeypatch.delenv(vault.VAULT_PATH_ENV, raising=False)
    vault._load.cache_clear()
    outcome = run_local_judge("General question about trees", "")
    assert outcome.status == "refused"
    assert outcome.code == "vault_absent"

    # 2. Broken vault (malformed toml)
    bad_vault = tmp_path / "bad_vault"
    bad_vault.mkdir()
    (bad_vault / "jarvis_vault.toml").write_text("invalid [[ toml", encoding="utf-8")
    monkeypatch.setenv(vault.VAULT_PATH_ENV, str(bad_vault))
    vault._load.cache_clear()
    outcome2 = run_local_judge("General question about trees", "")
    assert outcome2.status == "refused"
    assert outcome2.code == "gate_error"


def test_local_judge_runs_via_run_ai_task_and_creates_ai_jobs_row(judge_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh context local judge executes via run_ai_task on local route and records ai_jobs row."""
    # Simulate judge detecting inferred local item L1
    monkeypatch.setenv(FAKE_JUDGE_ENV, json.dumps({"hits": ["L1"], "reason": "The text implies nozzle design."}))

    outcome = run_local_judge("Could the spray shape be narrowed using a shaped orifice?", "")
    assert outcome.status == "rewrite_required"
    assert outcome.code == "local_only"
    assert outcome.hits == ("L1",)
    assert outcome.ai_job_id is not None
    # Proposition text not echoed in reason
    assert "proprietary" not in (outcome.reason or "")

    # Check ai_jobs row was created
    row = judge_env.execute("SELECT id, selected_route_class, status FROM ai_jobs WHERE id = ?", (outcome.ai_job_id,)).fetchone()
    assert row is not None
    assert row[1] == "local:llamacpp"
    assert row[2] == "success"


def test_local_judge_pass_creates_ai_job(judge_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """When judge finds no hits, verdict is pass and ai_job is recorded."""
    monkeypatch.setenv(FAKE_JUDGE_ENV, json.dumps({"hits": [], "reason": "No local-only items or specific values."}))

    outcome = run_local_judge("What are common organic fertilizers for fruit trees?", "")
    assert outcome.status == "pass"
    assert outcome.code is None
    assert outcome.hits == ()
    assert outcome.ai_job_id is not None

    # Check ai_jobs row
    row = judge_env.execute("SELECT id, selected_route_class, status FROM ai_jobs WHERE id = ?", (outcome.ai_job_id,)).fetchone()
    assert row is not None
    assert row[2] == "success"


def test_local_judge_malformed_json_fails_closed(judge_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unparseable judge JSON, missing fields, or provider failure refuses with judge_error."""
    # 1. Unparseable JSON
    monkeypatch.setenv(FAKE_JUDGE_ENV, "not json at all")
    outcome = run_local_judge("General question", "")
    assert outcome.status == "refused"
    assert outcome.code == "judge_error"

    # 2. Missing reason field
    monkeypatch.setenv(FAKE_JUDGE_ENV, json.dumps({"hits": []}))
    outcome2 = run_local_judge("General question", "")
    assert outcome2.status == "refused"
    assert outcome2.code == "judge_error"

    # 3. Hits is not a list
    monkeypatch.setenv(FAKE_JUDGE_ENV, json.dumps({"hits": "L1", "reason": "bad"}))
    outcome3 = run_local_judge("General question", "")
    assert outcome3.status == "refused"
    assert outcome3.code == "judge_error"

    # 4. Provider failure
    monkeypatch.setenv(FAKE_JUDGE_ENV, "provider_error")
    outcome4 = run_local_judge("General question", "")
    assert outcome4.status == "refused"
    assert outcome4.code == "judge_error"


def test_request_consult_records_judge_verdict_and_ai_job_in_gate_steps(
    judge_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """request_consult records the judge verdict and ai_job_id in consult_attempts.gate_steps_json."""
    monkeypatch.setenv(FAKE_JUDGE_ENV, json.dumps({"hits": ["L1"], "reason": "Implies L1."}))

    outcome = request_consult(
        judge_env,
        ConsultRequest(kind="ideas", question="How does the orifice narrow spray?", modules=["M1"]),
        transport=DEFAULT_CONSULT_TRANSPORT,
    )
    assert outcome.status == "rewrite_required"
    assert outcome.code == "local_only"
    assert outcome.offending == ("L1",)
    assert outcome.consult_id is not None

    row = judge_env.execute(
        "SELECT gate_steps_json, ai_job_id FROM consult_attempts WHERE id = ?",
        (outcome.consult_id,),
    ).fetchone()
    assert row is not None
    steps = json.loads(row[0])
    judge_step = next((s for s in steps if s.get("step") == "local_only_judge"), None)
    assert judge_step is not None
    assert judge_step["result"] == "rewrite_required"
    assert judge_step["hits"] == ["L1"]
    assert judge_step.get("ai_job_id") is not None


def test_edit_consult_reruns_judge(
    judge_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """edit_consult re-runs the judge and refuses if edited text contains local-only items."""
    # First, create a pending consult that passes
    monkeypatch.setenv(FAKE_JUDGE_ENV, json.dumps({"hits": [], "reason": "Clean text."}))
    outcome = request_consult(
        judge_env,
        ConsultRequest(kind="ideas", question="Initial clean question about leaves", modules=["M1"]),
        transport=DEFAULT_CONSULT_TRANSPORT,
    )
    assert outcome.status == "pending"
    consult_id = outcome.consult_id
    assert consult_id is not None

    # Now edit to add a local-only proposition literal (P1)
    edit_outcome = edit_consult(
        judge_env,
        consult_id,
        question="Edited question mentioning subterranean moisture sensor array",
        context="",
    )
    assert edit_outcome.status == "rewrite_required"
    assert edit_outcome.code == "local_only"
    assert "P1" in edit_outcome.offending
    assert "subterranean moisture sensor array" not in (edit_outcome.reason or "")
