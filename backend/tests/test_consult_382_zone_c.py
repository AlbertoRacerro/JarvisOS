"""Tests for Spec 382: Zone-C hardening, unified guard, mosaic defense, and leak benchmark.

Covers all 6 acceptance criteria:
1. Every zone-C door passes through evaluate_zone_c_request and blocks when any stage fails.
2. Inference judge blocks flagged queries (zone_c_inference_judge) with an ai_jobs row; malformed blocks (zone_c_judge_error).
3. Rolling 30-day group counter blocks with mosaic_group_threshold and expires entries older than 30 days.
4. Contextual mosaic judge inspects last 20 same-group queries and blocks cross-query inference (zone_c_mosaic_judge).
5. Strict mode is active by default and stays active unless consult.toml has valid zone_c_benchmark_passed_on.
6. Offline leak benchmark (zone_c_leak_bench.py) runs end-to-end with single-query and whole-log passes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import packs, vault
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.agents.hermes.browser_page_tool import execute_browser_page_read
from app.modules.agents.hermes.paper_search_tool import execute_paper_search
from app.modules.consult.config import load_consult_config
from app.modules.consult.mcp_server import ConsultMcpServer
from app.modules.consult.zone_c_guard import (
    FAKE_TRANSPORT_ENV,
    FAKE_ZONE_C_INFERENCE_ENV,
    FAKE_ZONE_C_MOSAIC_ENV,
    evaluate_zone_c_request,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.zone_c_leak_bench import run_leak_benchmark  # noqa: E402

BRIEF = "# Test brief\nNucleus content."
DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.fixture()
def zone_c_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    """Setup isolated test environment with pack, vault, consult.toml, and fake transport."""
    # Data root
    data_root = tmp_path / "data-root"
    data_root.mkdir()
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(data_root))

    # Pack
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "brief.md").write_text(BRIEF, encoding="utf-8")
    (pack_dir / "jarvis_pack.toml").write_text(
        'id = "orchard"\n[sensitivity]\nterms = ["secretfruit"]\n'
        '[search_guard]\nmin_groups = 2\nexact_combinations = [["root", "fungus"]]\n'
        'groups = [["soil", "dirt"], ["tree", "apple"]]\n'
        '[consult]\nbrief = "brief.md"\nnucleus_heading = "Nucleus"\n',
        encoding="utf-8",
    )

    # Vault
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "jarvis_vault.toml").write_text(
        '[[local_only]]\nid = "L1"\ntext = "proprietary irrigation nozzle geometry"\n\n'
        '[[protected_propositions]]\nid = "P1"\ntext = "subterranean moisture sensor array"\n',
        encoding="utf-8",
    )
    (vault_dir / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\n'
        '[[trusted_accounts]]\nid = "sub"\nagent = "claude"\n'
        'model = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n',
        encoding="utf-8",
    )

    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack_dir))
    monkeypatch.setenv(vault.VAULT_PATH_ENV, str(vault_dir))
    monkeypatch.setenv(FAKE_TRANSPORT_ENV, "1")

    packs._load.cache_clear()
    vault._load.cache_clear()
    initialize_database()

    with open_sqlite_connection() as conn:
        yield conn

    packs._load.cache_clear()
    vault._load.cache_clear()


# ===========================================================================
# Acceptance Criterion 1: All 4 Zone-C Doors Wired and Fail-Closed
# ===========================================================================


def test_door1_search_guard_proxy(zone_c_env: sqlite3.Connection) -> None:
    """Door 1: evaluate_zone_c_request invoked for search guard proxy queries."""
    # 1. Clean query passes
    res_ok = evaluate_zone_c_request("web_search", "weather forecast tomorrow", connection=zone_c_env)
    assert res_ok.allowed is True
    assert res_ok.reason_code == "ok"

    # 2. Pack term blocks in Stage 1
    res_pack = evaluate_zone_c_request("web_search", "secretfruit analysis", connection=zone_c_env)
    assert res_pack.allowed is False
    assert res_pack.reason_code == "pack_term"
    assert "secretfruit" not in (res_pack.refusal_message or "")


def test_door2_paper_search_tool(zone_c_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Door 2: execute_paper_search passes through evaluate_zone_c_request and blocks on flags."""
    # Stage 1 deterministic block
    res_det = execute_paper_search("study on secretfruit trees")
    assert res_det.get("error") == "egress_blocked_private_content"
    assert res_det.get("reason_code") == "pack_term"
    assert "secretfruit" not in res_det.get("message", "")

    # Stage 2 inference judge block
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, json.dumps({"hits": ["L1"], "reason": "Mentions proprietary nozzle"}))
    res_judge = execute_paper_search("agricultural spray nozzle fluid dynamics")
    assert res_judge.get("error") == "egress_blocked_private_content"
    assert res_judge.get("reason_code") == "zone_c_inference_judge"
    assert "proprietary irrigation nozzle geometry" not in res_judge.get("message", "")


def test_door3_browser_page_tool(zone_c_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Door 3: execute_browser_page_read passes through evaluate_zone_c_request and blocks on flags."""
    def fake_dns(host: str) -> list[str]:
        return ["93.184.216.34"]

    # URL containing pack term blocks in Stage 1
    res_det = execute_browser_page_read("https://example.com/secretfruit_docs", dns_resolver=fake_dns)
    assert res_det.get("status") == "error"
    assert res_det.get("reason_code") == "pack_term"

    # Search term flagged by Stage 2 inference judge
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, json.dumps({"hits": ["P1"], "reason": "Mentions sensor array"}))
    res_judge = execute_browser_page_read(
        "https://example.com/research", search_term="sensor array calibration", dns_resolver=fake_dns
    )
    assert res_judge.get("status") == "error"
    assert res_judge.get("reason_code") == "zone_c_inference_judge"
    assert "subterranean moisture sensor array" not in res_judge.get("message", "")


def test_door4_consult_mcp_server(zone_c_env: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Door 4: consult/mcp_server web_search, paper_search, and read_page pass through evaluate_zone_c_request."""
    server = ConsultMcpServer(str(tmp_path / "mcp.sock"))

    # 1. MCP web_search blocks pack term
    req_ws = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "web_search", "arguments": {"query": "harvesting secretfruit"}},
    }
    resp_ws = server.handle_request(req_ws)
    assert resp_ws is not None
    assert "search_guard_blocked:pack_term" in resp_ws["result"]["content"][0]["text"]

    # 2. MCP paper_search blocks judge flag
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, json.dumps({"hits": ["L1"], "reason": "Nozzle inference"}))
    req_ps = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "paper_search", "arguments": {"query": "nozzle geometry fluid mechanics"}},
    }
    resp_ps = server.handle_request(req_ps)
    assert resp_ps is not None
    assert "search_guard_blocked:zone_c_inference_judge" in resp_ps["result"]["content"][0]["text"]

    # 3. MCP read_page blocks on Stage 1
    req_rp = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "read_page", "arguments": {"url": "https://example.com/secretfruit"}},
    }
    resp_rp = server.handle_request(req_rp)
    assert resp_rp is not None
    assert "search_guard_blocked:pack_term" in resp_rp["result"]["content"][0]["text"]


# ===========================================================================
# Acceptance Criterion 2: Local Inference Judge and Malformed Error Handling
# ===========================================================================


def test_stage2_inference_judge_flagged_creates_ai_job(
    zone_c_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flagged local inference judge blocks with zone_c_inference_judge and writes an ai_jobs row."""
    monkeypatch.setenv(
        FAKE_ZONE_C_INFERENCE_ENV,
        json.dumps({"hits": ["L1"], "reason": "Mentions nozzle shape"}),
    )

    decision = evaluate_zone_c_request(
        "web_search",
        "What nozzle dimensions work best for low-pressure drip irrigation?",
        connection=zone_c_env,
    )
    assert decision.allowed is False
    assert decision.reason_code == "zone_c_inference_judge"
    assert decision.ai_job_id is not None

    # Check ai_jobs table row exists (invariants 1 and 2)
    cursor = zone_c_env.execute("SELECT id, requested_route_class FROM ai_jobs WHERE id = ?", (decision.ai_job_id,))
    row = cursor.fetchone()
    assert row is not None
    assert row[1] == "local:llamacpp"

    # Query history table recorded the blocked decision
    cursor_hist = zone_c_env.execute(
        "SELECT decision, reason_code, ai_job_id FROM zone_c_query_history WHERE id = (SELECT max(id) FROM zone_c_query_history)"
    )
    h_row = cursor_hist.fetchone()
    assert h_row[0] == "blocked"
    assert h_row[1] == "zone_c_inference_judge"
    assert h_row[2] == decision.ai_job_id


def test_stage2_judge_error_or_malformed_blocks(
    zone_c_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed judge JSON or provider error blocks with zone_c_judge_error."""
    # 1. Malformed JSON
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, "NOT_JSON_AT_ALL")
    dec_malformed = evaluate_zone_c_request("web_search", "farming techniques", connection=zone_c_env)
    assert dec_malformed.allowed is False
    assert dec_malformed.reason_code == "zone_c_judge_error"

    # 2. Missing hits field
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, json.dumps({"reason": "no hits field provided"}))
    dec_nohits = evaluate_zone_c_request("web_search", "farming techniques", connection=zone_c_env)
    assert dec_nohits.allowed is False
    assert dec_nohits.reason_code == "zone_c_judge_error"

    # 3. Provider failure
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, "provider_error")
    dec_err = evaluate_zone_c_request("web_search", "farming techniques", connection=zone_c_env)
    assert dec_err.allowed is False
    assert dec_err.reason_code == "zone_c_judge_error"


# ===========================================================================
# Acceptance Criterion 3: Rolling 30-Day Concept Group Counter and Expiry
# ===========================================================================


def test_stage3_rolling_30day_concept_group_threshold(
    zone_c_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rolling 30-day per-group counter blocks with mosaic_group_threshold when union reaches min_groups."""
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, json.dumps({"hits": [], "reason": "clean"}))
    monkeypatch.setenv(FAKE_ZONE_C_MOSAIC_ENV, json.dumps({"hits": [], "reason": "clean"}))

    # Pack groups: 0: ["soil", "dirt"], 1: ["tree", "apple"], min_groups = 2
    # Query 1: touches group 0 ("soil")
    q1 = evaluate_zone_c_request("web_search", "soil moisture drainage", connection=zone_c_env)
    assert q1.allowed is True
    assert "orchard:0" in q1.matched_groups

    # Query 2: touches group 1 ("tree") within 30 days -> union of touched groups is {0, 1} >= min_groups (2)
    q2 = evaluate_zone_c_request("web_search", "tree branch growth rate", connection=zone_c_env)
    assert q2.allowed is False
    assert q2.reason_code == "mosaic_group_threshold"
    assert "30-day concept group threshold reached" in (q2.refusal_message or "")


def test_stage3_rolling_30day_expiry(
    zone_c_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Group history older than 30 days expires and does not count toward the rolling union."""
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, json.dumps({"hits": [], "reason": "clean"}))
    monkeypatch.setenv(FAKE_ZONE_C_MOSAIC_ENV, json.dumps({"hits": [], "reason": "clean"}))

    # Insert an allowed query touching group 0 ("orchard:0") with created_at 35 days ago (expired)
    old_ts = (datetime.now(UTC) - timedelta(days=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
    zone_c_env.execute(
        """
        INSERT INTO zone_c_query_history (
            id, kind, query_text, query_sha256, matched_groups_json, decision, reason_code, ai_job_id, created_at
        ) VALUES ('zcq_old_0', 'web_search', 'soil test old', 'sha_old', '["orchard:0"]', 'allowed', 'ok', NULL, ?)
        """,
        (old_ts,),
    )
    zone_c_env.commit()

    # Query touching group 1: since old query is expired, union of last 30 days is only {1} (< min_groups 2)
    q_now = evaluate_zone_c_request("web_search", "tree branch growth rate", connection=zone_c_env)
    assert q_now.allowed is True
    assert q_now.reason_code == "ok"

    # Insert an allowed query touching group 0 with created_at 29 days ago (still within 30-day window)
    recent_ts = (datetime.now(UTC) - timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")
    zone_c_env.execute(
        """
        INSERT INTO zone_c_query_history (
            id, kind, query_text, query_sha256, matched_groups_json, decision, reason_code, ai_job_id, created_at
        ) VALUES ('zcq_recent_0', 'web_search', 'soil test recent', 'sha_rec', '["orchard:0"]', 'allowed', 'ok', NULL, ?)
        """,
        (recent_ts,),
    )
    zone_c_env.commit()

    # Now touching group 1 hits union {orchard:0, orchard:1} >= min_groups 2 and blocks
    q_blocked = evaluate_zone_c_request("web_search", "apple canopy pruning", connection=zone_c_env)
    assert q_blocked.allowed is False
    assert q_blocked.reason_code == "mosaic_group_threshold"


# ===========================================================================
# Acceptance Criterion 4: Contextual Mosaic Judge (Last 20 Same-Group Queries)
# ===========================================================================


def test_stage3_contextual_mosaic_judge(
    zone_c_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mosaic judge receives recent same-group queries and blocks cross-query inference with zone_c_mosaic_judge."""
    # Query 1 touches group 0 ("soil") and is allowed
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, json.dumps({"hits": [], "reason": "clean"}))
    monkeypatch.setenv(FAKE_ZONE_C_MOSAIC_ENV, json.dumps({"hits": [], "reason": "clean"}))
    q1 = evaluate_zone_c_request("web_search", "soil moisture composition", connection=zone_c_env)
    assert q1.allowed is True

    # Query 2 also touches group 0 ("dirt")
    # Mosaic judge flags cross-query inference
    monkeypatch.setenv(
        FAKE_ZONE_C_MOSAIC_ENV,
        json.dumps({"hits": ["L1"], "reason": "Combining soil composition with dirt pressure implies nozzle depth"}),
    )
    q2 = evaluate_zone_c_request("web_search", "dirt pressure and compaction", connection=zone_c_env)
    assert q2.allowed is False
    assert q2.reason_code == "zone_c_mosaic_judge"
    assert q2.ai_job_id is not None

    # Refusal message contains no secrets or items
    assert "proprietary irrigation nozzle geometry" not in (q2.refusal_message or "")
    assert "cross-query mosaic analysis detected risk" in (q2.refusal_message or "")


# ===========================================================================
# Acceptance Criterion 5: Strict Mode vs zone_c_benchmark_passed_on
# ===========================================================================


def test_strict_mode_active_by_default_and_relaxed_by_valid_date(
    zone_c_env: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict mode blocks uncertain judge verdicts by default; relaxes only when zone_c_benchmark_passed_on is valid."""
    vault_dir = Path(tmp_path / "vault")

    # 1. By default, consult.toml has NO zone_c_benchmark_passed_on -> strict mode active
    cfg_default = load_consult_config()
    assert cfg_default.zone_c_strict is True

    # In strict mode, an uncertain verdict (hits: [], uncertain: true) is BLOCKED
    monkeypatch.setenv(
        FAKE_ZONE_C_INFERENCE_ENV,
        json.dumps({"hits": [], "uncertain": True, "reason": "possible correlation with L1"}),
    )
    res_strict = evaluate_zone_c_request("web_search", "irrigation system techniques", connection=zone_c_env)
    assert res_strict.allowed is False
    assert res_strict.reason_code == "zone_c_inference_judge"

    # 2. Add valid zone_c_benchmark_passed_on to consult.toml -> strict mode relaxed
    (vault_dir / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\nzone_c_benchmark_passed_on = 2026-11-25\n'
        '[[trusted_accounts]]\nid = "sub"\nagent = "claude"\n'
        'model = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n',
        encoding="utf-8",
    )
    cfg_relaxed = load_consult_config()
    assert cfg_relaxed.zone_c_strict is False
    assert cfg_relaxed.zone_c_benchmark_passed_on is not None

    # Now uncertain verdict without hits passes under standard hit matching
    res_relaxed = evaluate_zone_c_request("web_search", "irrigation system techniques", connection=zone_c_env)
    assert res_relaxed.allowed is True
    assert res_relaxed.reason_code == "ok"

    # 3. Invalid date: future date keeps strict mode active
    (vault_dir / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\nzone_c_benchmark_passed_on = 2099-01-01\n'
        '[[trusted_accounts]]\nid = "sub"\nagent = "claude"\n'
        'model = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n',
        encoding="utf-8",
    )
    cfg_future = load_consult_config()
    assert cfg_future.zone_c_strict is True

    # 4. Invalid date: datetime string keeps strict mode active
    (vault_dir / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\nzone_c_benchmark_passed_on = 2026-11-25T12:00:00\n'
        '[[trusted_accounts]]\nid = "sub"\nagent = "claude"\n'
        'model = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n',
        encoding="utf-8",
    )
    cfg_datetime = load_consult_config()
    assert cfg_datetime.zone_c_strict is True


# ===========================================================================
# Fail-closed Constraints & Invariants
# ===========================================================================


def test_vault_fail_closed_when_vault_path_set_and_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing or corrupted vault directory fails closed with vault_load_error."""
    monkeypatch.setenv(FAKE_TRANSPORT_ENV, "1")

    # Non-existent vault directory
    monkeypatch.setenv(vault.VAULT_PATH_ENV, str(tmp_path / "nonexistent_vault"))
    vault._load.cache_clear()
    dec_missing = evaluate_zone_c_request("web_search", "simple search")
    assert dec_missing.allowed is False
    assert dec_missing.reason_code == "vault_load_error"

    # Corrupted jarvis_vault.toml
    bad_v = tmp_path / "bad_vault"
    bad_v.mkdir()
    (bad_v / "jarvis_vault.toml").write_text("malformed [[ toml", encoding="utf-8")
    monkeypatch.setenv(vault.VAULT_PATH_ENV, str(bad_v))
    vault._load.cache_clear()
    dec_corrupt = evaluate_zone_c_request("web_search", "simple search")
    assert dec_corrupt.allowed is False
    assert dec_corrupt.reason_code == "vault_load_error"


def test_db_write_error_fails_closed(
    zone_c_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If recording to zone_c_query_history raises an error, the request fails closed."""
    monkeypatch.setattr("app.modules.consult.zone_c_guard._record_query_history", lambda *args, **kwargs: False)
    dec = evaluate_zone_c_request("web_search", "test search", connection=zone_c_env)
    assert dec.allowed is False
    assert dec.reason_code == "guard_error"


# ===========================================================================
# Acceptance Criterion 6: Offline Zone-C Leak Benchmark
# ===========================================================================


def test_offline_leak_benchmark_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scripts/zone_c_leak_bench.py runs end-to-end producing result.json and summary.md."""
    # Create isolated pack & vault
    pack_dir = tmp_path / "bench_pack"
    pack_dir.mkdir()
    (pack_dir / "jarvis_pack.toml").write_text(
        'id = "zephyr"\n[sensitivity]\nterms = ["classifiedcore"]\n'
        '[search_guard]\nmin_groups = 3\n'
        'groups = [["optics", "lens"], ["laser", "beam"]]\n',
        encoding="utf-8",
    )
    vault_dir = tmp_path / "bench_vault"
    vault_dir.mkdir()
    (vault_dir / "jarvis_vault.toml").write_text(
        '[[local_only]]\nid = "L10"\ntext = "focal length exact curve"\n',
        encoding="utf-8",
    )
    (vault_dir / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\nzone_c_benchmark_passed_on = 2026-11-25\n',
        encoding="utf-8",
    )

    corpus_path = tmp_path / "queries.json"
    corpus_queries = [
        "quantum optics principles",
        "lens focal properties",
        "classifiedcore experimental results",  # Stage 1 block
        "laser beam dispersion rates",
    ]
    corpus_path.write_text(json.dumps(corpus_queries), encoding="utf-8")

    out_dir = tmp_path / "bench_output"
    data_root = tmp_path / "bench_data"

    monkeypatch.setenv(FAKE_TRANSPORT_ENV, "1")
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, json.dumps({"hits": [], "reason": "clean"}))
    monkeypatch.setenv(FAKE_ZONE_C_MOSAIC_ENV, json.dumps({"hits": [], "reason": "clean"}))
    # Attacker finds no leaks
    monkeypatch.setenv("JARVISOS_FAKE_ATTACKER_RESPONSE", json.dumps({"inferred": [], "reason": "clean"}))

    results = run_leak_benchmark(
        corpus_path=corpus_path,
        output_dir=out_dir,
        data_root=data_root,
        vault_path=vault_dir,
        pack_path=pack_dir,
    )

    assert results["total_queries"] == 4
    assert results["allowed_count"] == 3
    assert results["blocked_count"] == 1
    assert results["blocked_reasons"].get("pack_term") == 1
    assert results["leak_detected"] is False
    assert results["release_criterion_met"] is True

    # Verify generated files
    res_json_file = out_dir / "result.json"
    summary_md_file = out_dir / "summary.md"
    assert res_json_file.is_file()
    assert summary_md_file.is_file()

    summary_text = summary_md_file.read_text(encoding="utf-8")
    assert "Zone-C Leak Benchmark Summary" in summary_text
    assert "PASSED (Zero Leaks)" in summary_text
    assert "`pack_term`" in summary_text


def test_stage3_mosaic_judge_capped_at_20_recent_queries(
    zone_c_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contextual mosaic judge receives at most the 20 most recent allowed same-group queries."""
    from types import SimpleNamespace

    from app.modules.ai.contracts import AIResponse, AIUsage

    captured_prompts: list[str] = []

    def fake_runner(**kwargs):
        captured_prompts.append(kwargs["user_prompt"])
        return SimpleNamespace(
            status="success",
            ledger_id="job-cap-test",
            response=AIResponse(
                provider_id="local_judge_fake",
                model_id="inkling-fake",
                request_id="req-cap",
                text=json.dumps({"hits": [], "reason": "clean"}),
                usage=AIUsage(provider_id="local_judge_fake", model_id="inkling-fake", input_tokens=10, output_tokens=5),
            ),
        )

    # Seed 25 allowed queries touching orchard:0
    base = datetime.now(UTC) - timedelta(hours=30)
    for i in range(25):
        ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        zone_c_env.execute(
            """
            INSERT INTO zone_c_query_history (
                id, kind, query_text, query_sha256, matched_groups_json, decision, reason_code, ai_job_id, created_at
            ) VALUES (?, 'web_search', ?, ?, '["orchard:0"]', 'allowed', 'ok', NULL, ?)
            """,
            (f"zcq_cap_{i:02d}", f"soil sample marker_{i:02d}", f"sha_{i:02d}", ts),
        )
    zone_c_env.commit()

    dec = evaluate_zone_c_request("web_search", "soil salinity probe", connection=zone_c_env, runner=fake_runner)
    assert dec.allowed is True
    assert len(captured_prompts) == 2
    mosaic_prompt = captured_prompts[1]
    assert "marker_24" in mosaic_prompt
    assert "marker_05" in mosaic_prompt
    assert "marker_04" not in mosaic_prompt
    assert "marker_00" not in mosaic_prompt


def test_offline_leak_benchmark_fails_closed_on_attacker_hit_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attacker hit or malformed attacker output both set release_criterion_met=False."""
    pack_dir = tmp_path / "bench_pack2"
    pack_dir.mkdir()
    (pack_dir / "jarvis_pack.toml").write_text('id = "zephyr"\n[sensitivity]\nterms = ["classifiedcore"]\n', encoding="utf-8")
    vault_dir = tmp_path / "bench_vault2"
    vault_dir.mkdir()
    (vault_dir / "jarvis_vault.toml").write_text(
        '[[local_only]]\nid = "L10"\ntext = "focal length exact curve"\n', encoding="utf-8"
    )
    (vault_dir / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\nzone_c_benchmark_passed_on = 2026-11-25\n', encoding="utf-8"
    )
    corpus_path = tmp_path / "q2.json"
    corpus_path.write_text(json.dumps(["general optics theory"]), encoding="utf-8")

    monkeypatch.setenv(FAKE_TRANSPORT_ENV, "1")
    monkeypatch.setenv(FAKE_ZONE_C_INFERENCE_ENV, json.dumps({"hits": [], "reason": "clean"}))

    # 1. Attacker infers a hit -> fails
    monkeypatch.setenv("JARVISOS_FAKE_ATTACKER_RESPONSE", json.dumps({"inferred": ["L10"], "reason": "leaked"}))
    res_hit = run_leak_benchmark(
        corpus_path=corpus_path,
        output_dir=tmp_path / "out_hit",
        data_root=tmp_path / "data_hit",
        vault_path=vault_dir,
        pack_path=pack_dir,
    )
    assert res_hit["leak_detected"] is True
    assert res_hit["release_criterion_met"] is False

    # 2. Attacker returns malformed JSON -> fails closed
    monkeypatch.setenv("JARVISOS_FAKE_ATTACKER_RESPONSE", "NOT_JSON")
    res_err = run_leak_benchmark(
        corpus_path=corpus_path,
        output_dir=tmp_path / "out_err",
        data_root=tmp_path / "data_err",
        vault_path=vault_dir,
        pack_path=pack_dir,
    )
    assert len(res_err["attacker_errors"]) == 1
    assert res_err["release_criterion_met"] is False
