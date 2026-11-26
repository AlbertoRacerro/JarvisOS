"""Tests for Spec 384: STRATA-FAST-SWITCH-1 (measure and minimise local Strata model switch latency).

Covers:
1. scripts/benchmarks/strata_switch_bench.py:
   - Phase-breakdown math & aggregation
   - Strata log parser (regex matching weight_read_s, vram_upload_s, engine_init_s, units ms/s) and /api-monitor JSON
   - Loopback and port-8000 guard
   - Vault guard integration
   - Plain-Italian summary table formatting
   - Idle check (/slots is_processing)
   - Benchmark orchestration with mock runner
2. page_cache.py:
   - MemAvailable safety floor refusal
   - posix_fadvise POSIX_FADV_WILLNEED call when safe
   - Async background prewarm helper
   - Missing file / non-Linux fallback
3. host_start.py:
   - StrataRuntimeController tight polling (0.1s default, configurable)
   - Loopback /unload probing with strict loopback & port-8000 checks
   - Fail-closed fallback to service.control stop on 404/403/network error
   - Timing attributes (last_stop_duration_s, last_start_dispatch_s, last_switch_mode)
4. sage.py & supervisor.py:
   - Maintainer-first guard: /slots busy wait and timeout refusal
   - Early overlap page-cache prewarm on enqueue_sage_consult
   - Batched multi-item swap with gate_steps_json timing breakdown
   - Supervisor avoiding redundant steps after specialist consult queued while preserving batching
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.core import packs, vault
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.agents.hermes.supervisor import _has_non_consult_work
from app.modules.consult.sage import (
    FAKE_SAGE_ENV,
    SageQueueItem,
    clear_sage_queue,
    enqueue_sage_consult,
    flush_sage_queue,
)
from app.modules.consult.service import ConsultRequest, request_consult
from app.modules.local_ai.runtime import host_start, model_registry, page_cache
from tests.test_consult_380_flow import BRIEF, DIGEST

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import vault_guard  # noqa: E402
from scripts.benchmarks import strata_switch_bench as bench  # noqa: E402

# ==============================================================================
# 1. Benchmark: strata_switch_bench.py tests
# ==============================================================================

def test_bench_loopback_and_port_8000_guard() -> None:
    """Benchmark refuses non-loopback hosts and strictly refuses port 8000."""
    # Allowed loopback
    p1 = bench.validate_loopback_url("http://127.0.0.1:8080")
    assert p1.port == 8080
    assert bench.is_loopback_host(p1.hostname or "")

    p2 = bench.validate_loopback_url("http://localhost:8081")
    assert p2.port == 8081

    # Port 8000 refused
    with pytest.raises(ValueError, match="Port 8000 is refused"):
        bench.validate_loopback_url("http://127.0.0.1:8000")

    with pytest.raises(ValueError, match="Port 8000 is refused"):
        bench.validate_loopback_url("http://localhost:8000")

    # Non-loopback refused
    with pytest.raises(ValueError, match="not a loopback interface"):
        bench.validate_loopback_url("http://192.168.1.100:8080")

    with pytest.raises(ValueError, match="not a loopback interface"):
        bench.validate_loopback_url("http://example.com:8080")

    # Invalid scheme
    with pytest.raises(ValueError, match="Invalid URL scheme"):
        bench.validate_loopback_url("ftp://127.0.0.1:8080")


def test_bench_vault_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Benchmark respects vault guard and exits 3 if paths touch the vault."""
    vault_dir = tmp_path / "protected_vault"
    vault_dir.mkdir()
    monkeypatch.setenv(vault_guard.VAULT_PATH_ENV, str(vault_dir))

    inside_vault = vault_dir / "result.json"
    code = bench.main(["--output", str(inside_vault)])
    assert code == vault_guard.REFUSED_EXIT


def test_bench_log_parser_and_api_monitor() -> None:
    """Strata log parser handles log lines (seconds and ms) as well as /api-monitor dict."""
    # Test text log with mixed units
    log_sample = """
    2026-11-26 14:00:00 [info] Engine starting up...
    2026-11-26 14:00:05 [info] weights read in 12.345 s
    2026-11-26 14:00:07 [info] VRAM upload in 3450 ms
    2026-11-26 14:00:08 [info] engine init: 1.25 s
    2026-11-26 14:00:09 [info] ready to serve requests
    """
    subphases = bench.parse_strata_subphases(log_sample)
    assert subphases["weight_read_s"] == 12.345
    assert subphases["vram_upload_s"] == 3.450
    assert subphases["engine_init_s"] == 1.250

    # Test api-monitor dict
    monitor_dict = {
        "weight_read_s": 8.5,
        "vram_upload_ms": 2500,
        "engine_init_s": 0.8,
    }
    parsed_dict = bench.parse_strata_subphases(monitor_dict)
    assert parsed_dict["weight_read_s"] == 8.5
    assert parsed_dict["vram_upload_s"] == 2.5
    assert parsed_dict["engine_init_s"] == 0.8


def test_bench_host_idle_check() -> None:
    """Host idle check polls /slots and waits until all is_processing are False."""
    call_count = 0

    def mock_getter(url: str, timeout: float) -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return [{"id": 0, "is_processing": True}]
        return [{"id": 0, "is_processing": False}]

    # Should wait until call_count is 3 and return True
    idle = bench.check_host_idle(
        "http://127.0.0.1:8080",
        timeout_s=5.0,
        poll_s=0.01,
        getter=mock_getter,
    )
    assert idle is True
    assert call_count == 3


def test_bench_host_idle_check_timeout() -> None:
    """Host idle check times out and returns False when model is continuously busy."""
    def mock_getter_busy(url: str, timeout: float) -> list[dict[str, Any]]:
        return [{"id": 0, "is_processing": True}]

    idle = bench.check_host_idle(
        "http://127.0.0.1:8080",
        timeout_s=0.1,
        poll_s=0.02,
        getter=mock_getter_busy,
    )
    assert idle is False


def test_bench_summary_and_italian_table() -> None:
    """Phase-breakdown math aggregation and Italian summary table formatting."""
    timings = [
        bench.SwitchPhaseTimings("default_to_specialist", 1, stop_old_s=1.0, start_dispatch_s=0.2, model_ready_s=10.0, first_token_s=0.5, total_switch_s=11.7),
        bench.SwitchPhaseTimings("default_to_specialist", 2, stop_old_s=1.2, start_dispatch_s=0.1, model_ready_s=10.2, first_token_s=0.4, total_switch_s=11.9),
    ]
    summary = bench.summarize_timings(timings)
    assert summary["runs"] == 2
    assert summary["stop_old_s_avg"] == 1.1
    assert summary["start_dispatch_s_avg"] == 0.15
    assert summary["model_ready_s_avg"] == 10.1
    assert summary["first_token_s_avg"] == 0.45
    assert summary["total_switch_s_avg"] == 11.8

    result_data = {
        "runs": 2,
        "default_to_specialist_summary": summary,
        "specialist_to_default_summary": summary,
        "host_ram_snapshot": {"mem_total_gib": 32.0, "mem_available_gib": 24.5},
        "decode_speed_check": {"before_tok_s": 35.2, "after_tok_s": 35.1},
        "page_cache_residency": {"default_model": {"residency_ratio": 1.0}},
        "subphases": {"weight_read_s": 4.5, "vram_upload_s": 1.2},
    }
    table = bench.format_italian_summary(result_data)
    assert "Riepilogo Switch Strata" in table
    assert "default -> specialist" in table
    assert "specialist -> default" in table
    assert "RAM Host: MemTotal = 32.00 GiB, MemAvailable = 24.50 GiB" in table
    assert "Velocità di decodifica modello predefinito" in table
    assert "prima = 35.20 tok/s, dopo = 35.10 tok/s" in table
    assert "Residenza cache pagina (default_model): 100.0%" in table


def test_bench_full_mock_run(tmp_path: Path) -> None:
    """Full benchmark run with mock runner emits valid result.json structure."""
    def mock_switch(source: str, target: str, direction: str, run_idx: int) -> bench.SwitchPhaseTimings:
        return bench.SwitchPhaseTimings(
            direction=direction,
            run_index=run_idx,
            stop_old_s=0.5,
            start_dispatch_s=0.1,
            model_ready_s=5.0,
            first_token_s=0.3,
            total_switch_s=5.9,
            switch_mode="loopback_unload",
        )

    res = bench.run_benchmark(
        base_url="http://127.0.0.1:8080",
        runs=2,
        slots_checker=lambda: True,
        switch_runner=mock_switch,
        decode_checker=lambda m: 42.0,
        check_decode=True,
    )
    assert res["runs"] == 2
    assert len(res["default_to_specialist_runs"]) == 2
    assert len(res["specialist_to_default_runs"]) == 2
    assert res["default_to_specialist_summary"]["total_switch_s_avg"] == 5.9
    assert res["decode_speed_check"]["before_tok_s"] == 42.0
    assert res["decode_speed_check"]["after_tok_s"] == 42.0


# ==============================================================================
# 2. Page Cache: page_cache.py tests
# ==============================================================================

def test_page_cache_safety_floor_refusal(tmp_path: Path) -> None:
    """can_prewarm refuses if MemAvailable would drop below safety floor."""
    import stat

    dummy_model = tmp_path / "model.gguf"
    dummy_model.write_bytes(b"0" * 1024)

    meminfo_file = tmp_path / "meminfo"
    # Set MemAvailable to 11 GiB, file is 2 GiB, floor is 10 GiB -> 11 - 2 = 9 < 10 -> REFUSED!
    gib_kb = 1024 * 1024
    meminfo_file.write_text(f"MemTotal: {32 * gib_kb} kB\nMemAvailable: {11 * gib_kb} kB\n", encoding="utf-8")

    # Mock file size to 2 GiB
    with patch.object(Path, "stat") as mock_stat:
        mock_stat.return_value.st_size = 2 * 1024 * 1024 * 1024
        mock_stat.return_value.st_mode = stat.S_IFREG | 0o644
        allowed, reason = page_cache.can_prewarm(
            dummy_model,
            safety_floor_bytes=10 * 1024 * 1024 * 1024,
            meminfo_path=meminfo_file,
        )
        assert not allowed
        assert "insufficient_ram_headroom" in reason

    # Now set MemAvailable to 15 GiB -> 15 - 2 = 13 >= 10 -> ALLOWED!
    meminfo_file.write_text(f"MemTotal: {32 * gib_kb} kB\nMemAvailable: {15 * gib_kb} kB\n", encoding="utf-8")
    with patch.object(Path, "stat") as mock_stat:
        mock_stat.return_value.st_size = 2 * 1024 * 1024 * 1024
        mock_stat.return_value.st_mode = stat.S_IFREG | 0o644
        allowed, reason = page_cache.can_prewarm(
            dummy_model,
            safety_floor_bytes=10 * 1024 * 1024 * 1024,
            meminfo_path=meminfo_file,
        )
        assert allowed
        assert reason == "ok"


def test_page_cache_missing_file_and_meminfo(tmp_path: Path) -> None:
    """can_prewarm safely handles missing model file or missing /proc/meminfo."""
    allowed, reason = page_cache.can_prewarm(tmp_path / "nonexistent.bin")
    assert not allowed
    assert reason == "file_not_found"

    model_f = tmp_path / "exists.bin"
    model_f.write_bytes(b"data")
    allowed, reason = page_cache.can_prewarm(model_f, meminfo_path=tmp_path / "no_meminfo")
    assert not allowed
    assert reason == "meminfo_unavailable"


def test_page_cache_prewarm_calls_fadvise(tmp_path: Path) -> None:
    """prewarm_model_file calls os.posix_fadvise with POSIX_FADV_WILLNEED when permitted."""
    model_f = tmp_path / "model.bin"
    model_f.write_bytes(b"model data")

    meminfo_file = tmp_path / "meminfo"
    gib_kb = 1024 * 1024
    meminfo_file.write_text(f"MemTotal: {32 * gib_kb} kB\nMemAvailable: {20 * gib_kb} kB\n", encoding="utf-8")

    fadvise_calls = []

    def mock_fadvise(fd: int, offset: int, length: int, advice: int) -> None:
        fadvise_calls.append((offset, length, advice))

    with patch("os.posix_fadvise", mock_fadvise):
        ok = page_cache.prewarm_model_file(
            model_f,
            safety_floor_bytes=10 * 1024 * 1024 * 1024,
            meminfo_path=meminfo_file,
        )
        assert ok is True
        assert len(fadvise_calls) == 1
        assert fadvise_calls[0] == (0, 0, os.POSIX_FADV_WILLNEED)


def test_page_cache_prewarm_async(tmp_path: Path) -> None:
    """prewarm_model_file_async returns a daemon thread that executes in background."""
    model_f = tmp_path / "model.bin"
    model_f.write_bytes(b"model data")

    meminfo_file = tmp_path / "meminfo"
    gib_kb = 1024 * 1024
    meminfo_file.write_text(f"MemTotal: {32 * gib_kb} kB\nMemAvailable: {20 * gib_kb} kB\n", encoding="utf-8")

    thread = page_cache.prewarm_model_file_async(
        model_f,
        safety_floor_bytes=10 * 1024 * 1024 * 1024,
        meminfo_path=meminfo_file,
    )
    assert thread is not None
    assert thread.daemon is True
    thread.join(timeout=2.0)
    assert not thread.is_alive()


# ==============================================================================
# 3. Host Start: host_start.py tests
# ==============================================================================

def test_host_start_tight_polling_default_and_config() -> None:
    """StrataRuntimeController defaults poll_interval_s to 0.1s and allows custom value."""
    ctrl = host_start.StrataRuntimeController()
    assert ctrl._poll_interval_s == 0.1

    ctrl_custom = host_start.StrataRuntimeController(poll_interval_s=0.05)
    assert ctrl_custom._poll_interval_s == 0.05


def test_host_start_loopback_unload_and_load_guards() -> None:
    """try_loopback_unload and try_loopback_load enforce loopback and refuse port 8000."""
    # Port 8000 refused
    assert not host_start.try_loopback_unload("127.0.0.1", 8000, "model")
    assert not host_start.try_loopback_load("127.0.0.1", 8000, "model")

    # Non-loopback refused
    assert not host_start.try_loopback_unload("192.168.1.50", 8080, "model")
    assert not host_start.try_loopback_load("192.168.1.50", 8080, "model")


def test_host_start_fast_unload_and_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """StrataRuntimeController.stop uses loopback unload when opted in and falls back on error."""
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    probe_results = [True, False]  # First ready, then stops

    def mock_probe(h: str, p: int, m: str) -> bool:
        return probe_results.pop(0) if probe_results else False

    # 1. Fast unload succeeds
    ctrl = host_start.StrataRuntimeController(
        probe_fn=mock_probe,
        unload_fn=lambda h, p, m: True,
        poll_interval_s=0.01,
    )
    st = ctrl.stop("test-model", "test.service", "127.0.0.1", 8080, "test-model", try_fast_unload=True)
    assert st.state == "stopped"
    assert st.last_action == "unload"
    assert ctrl.last_switch_mode == "loopback_unload"
    assert ctrl.last_stop_duration_s >= 0.0

    # 2. Fast unload fails (e.g. 404/network error) -> falls back to hostops spool
    spool_receipts = spool / "receipts"
    spool_receipts.mkdir(parents=True, exist_ok=True)

    def write_ok_receipt(spool_path: Path) -> None:
        # Check for request file and write receipt
        req_files = list((spool_path / "requests").glob("*.json"))
        if req_files:
            req_id = req_files[0].stem
            (spool_path / "receipts" / f"{req_id}.json").write_text(
                json.dumps({"schema": host_start.RECEIPT_SCHEMA, "request_id": req_id, "outcome": "ok"}),
                encoding="utf-8",
            )

    probe_results = [True, False]
    ctrl_fallback = host_start.StrataRuntimeController(
        probe_fn=mock_probe,
        unload_fn=lambda h, p, m: False,  # Unload fails!
        poll_interval_s=0.01,
    )

    # In background thread write ok receipt as soon as request appears
    def _receipt_worker() -> None:
        for _ in range(50):
            write_ok_receipt(spool)
            time.sleep(0.01)

    threading.Thread(target=_receipt_worker, daemon=True).start()

    st_fallback = ctrl_fallback.stop(
        "test-model", "test.service", "127.0.0.1", 8080, "test-model", try_fast_unload=True
    )
    assert st_fallback.state == "stopped"
    assert st_fallback.last_action == "stop"
    assert ctrl_fallback.last_switch_mode == "hostops"


# ==============================================================================
# 4. Sage & Supervisor: sage.py & supervisor.py tests
# ==============================================================================

@pytest.fixture()
def fast_switch_env(isolated_data_root: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
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
        '[[trusted_accounts]]\nid = "acc1"\nagent = "claude"\nmodel = "fake-primary"\ntraining_off_verified_on = 2026-11-25\n',
        encoding="utf-8",
    )
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    registry_file = settings_dir / "local_models.json"
    choice_file = settings_dir / "local_model_choice.json"

    models = [
        {
            "id": "inkling",
            "label": "Inkling",
            "runtime": "strata",
            "qualification": "default",
            "host": "127.0.0.1",
            "port": 8080,
            "unit": "inkling.service",
            "served_model": "inkling",
        },
        {
            "id": "sage",
            "label": "Sage",
            "runtime": "strata",
            "qualification": "qualified",
            "host": "127.0.0.1",
            "port": 8080,
            "unit": "sage.service",
            "served_model": "sage",
            "model_path": str((tmp_path / "sage.gguf").resolve()),
        },
    ]
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

    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack))
    monkeypatch.setenv(vault.VAULT_PATH_ENV, str(v))
    monkeypatch.setenv("JARVISOS_CONSULT_FAKE_TRANSPORT", "1")
    monkeypatch.setenv(FAKE_SAGE_ENV, "Risposta rapida specialistica di Sage.")

    packs._load.cache_clear()
    vault._load.cache_clear()
    initialize_database()
    with open_sqlite_connection() as conn:
        yield conn
    packs._load.cache_clear()
    vault._load.cache_clear()
    clear_sage_queue()


def test_maintainer_first_slots_busy_wait_and_abort(
    fast_switch_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flush_sage_queue waits if /slots is busy; if still busy past timeout, refuses swap."""
    turn_id = "turn-maintainer-busy"
    req = ConsultRequest(kind="ideas", question="Question for Sage?", modules=["M1"], local_only=True, turn_id=turn_id)
    out = request_consult(fast_switch_env, req)
    assert out.status == "queued_for_sage"

    # Mock wait_strata_slots_idle to return (False, 1.5) -> timeout while maintainer in flight!
    monkeypatch.setattr(
        "app.modules.consult.sage.wait_strata_slots_idle",
        lambda host, port, **kwargs: (False, 1.5),
    )

    swap_called = False

    def mock_activate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal swap_called
        swap_called = True
        return {}

    monkeypatch.setattr(model_registry, "activate_model", mock_activate)

    results = flush_sage_queue(turn_id, connection=fast_switch_env)
    assert len(results) == 0
    # Swap must NEVER have been initiated because maintainer was in flight!
    assert swap_called is False

    # Check attempt marked failed with maintainer_busy
    row = fast_switch_env.execute("SELECT state, gate_steps_json FROM consult_attempts WHERE id = ?", (out.consult_id,)).fetchone()
    assert row is not None
    assert row[0] == "failed"
    steps = json.loads(row[1])
    assert any(s.get("reason") == "maintainer_busy" for s in steps)


def test_maintainer_first_slots_wait_then_proceed(
    fast_switch_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flush_sage_queue waits for /slots to clear, then performs swap and records all phase timings."""
    turn_id = "turn-maintainer-cleared"
    req1 = ConsultRequest(kind="ideas", question="Q1 for Sage?", modules=["M1"], local_only=True, turn_id=turn_id)
    req2 = ConsultRequest(kind="critique", question="Q2 for Sage?", modules=["M1"], local_only=True, turn_id=turn_id)

    out1 = request_consult(fast_switch_env, req1)
    out2 = request_consult(fast_switch_env, req2)
    assert out1.status == "queued_for_sage"
    assert out2.status == "queued_for_sage"

    # Mock wait_strata_slots_idle to simulate 0.8s wait before maintainer finished
    monkeypatch.setattr(
        "app.modules.consult.sage.wait_strata_slots_idle",
        lambda host, port, **kwargs: (True, 0.825),
    )
    # Mock model ready
    monkeypatch.setattr("app.modules.consult.sage._wait_strata_ready", lambda m, **kwargs: True)
    monkeypatch.setattr(model_registry, "activate_model", lambda m, persist_choice=True: {})

    results = flush_sage_queue(turn_id, connection=fast_switch_env)
    assert len(results) == 2

    for cid in (out1.consult_id, out2.consult_id):
        row = fast_switch_env.execute("SELECT state, gate_steps_json FROM consult_attempts WHERE id = ?", (cid,)).fetchone()
        assert row is not None
        assert row[0] == "sent"
        steps = json.loads(row[1])
        swap_step = next((s for s in steps if s.get("step") == "sage_swap"), None)
        assert swap_step is not None
        assert swap_step["result"] == "pass"
        # Verify all phase timings are recorded
        assert swap_step["wait_idle_s"] == 0.825
        assert "stop_old_s" in swap_step
        assert "start_ready_s" in swap_step
        assert swap_step["swap_to_sage_s"] > 0
        assert swap_step["swap_back_s"] > 0
        assert swap_step["total_swap_s"] > 0
        assert "switch_mode" in swap_step


def test_early_overlap_prewarm_on_enqueue(
    fast_switch_env: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """enqueue_sage_consult triggers non-blocking page-cache prewarm for Sage model file."""
    prewarmed_files = []

    def mock_prewarm_async(file_path: str | Path, **kwargs: Any) -> Any:
        prewarmed_files.append(str(file_path))
        return None

    monkeypatch.setattr("app.modules.local_ai.runtime.page_cache.prewarm_model_file_async", mock_prewarm_async)

    item = SageQueueItem(
        consult_id="cid-overlap-1",
        turn_id="turn-overlap",
        workspace_id=None,
        thread_id=None,
        kind="ideas",
        question="How to optimize Strata switches?",
        context="context",
        modules=["M1"],
    )
    enqueue_sage_consult(item)

    assert len(prewarmed_files) == 1
    assert "sage.gguf" in prewarmed_files[0]


def test_supervisor_has_non_consult_work_helper() -> None:
    """_has_non_consult_work returns True only when pending non-consult steps remain."""
    # Empty plan
    assert not _has_non_consult_work({})
    assert not _has_non_consult_work({"steps": []})

    # Plan with only completed steps
    completed_plan = {
        "steps": [
            {"text": "Search literature", "status": "done"},
            {"text": "Run calculations", "status": "completed"},
        ]
    }
    assert not _has_non_consult_work(completed_plan)

    # Plan with pending non-consult step
    pending_plan = {
        "steps": [
            {"text": "Search literature", "status": "done"},
            {"text": "Run thermal calculations", "status": "pending"},
        ]
    }
    assert _has_non_consult_work(pending_plan)

    # Plan with pending consult step (should return False)
    consult_pending_plan = {
        "steps": [
            {"text": "Search literature", "status": "done"},
            {"text": "Consult Sage expert on irrigation", "status": "pending"},
        ]
    }
    assert not _has_non_consult_work(consult_pending_plan)


def test_supervisor_avoids_redundant_steps_after_consult_queued() -> None:
    """When a consult is queued and no pending non-consult steps remain, tools are restricted to consult/plan."""
    from app.modules.agents.hermes.supervisor import HermesSupervisor

    sup = HermesSupervisor.__new__(HermesSupervisor)
    sup.turn_step_count = 1
    sup.turn_cap = 10
    sup.turn_sage_queued = True
    sup.active_turn_plan = {
        "steps": [
            {"text": "Initial research", "status": "done"},
            {"text": "Consult Sage", "status": "pending"},
        ]
    }

    frame = {
        "tools": [
            {"name": "jarvis_repo_read"},
            {"name": "jarvis_paper_search"},
            {"name": "jarvis_consult"},
            {"name": "jarvis_turn_plan"},
        ],
        "allowed_tools": ["jarvis_repo_read", "jarvis_paper_search", "jarvis_consult", "jarvis_turn_plan"],
    }

    # Call the real production helper on HermesSupervisor
    restricted = sup._apply_sage_overlap_restriction(frame)
    assert restricted is True

    # Verify only consult and plan tools remain admitted
    remaining_names = [t["name"] for t in frame["tools"]]
    assert "jarvis_repo_read" not in remaining_names
    assert "jarvis_paper_search" not in remaining_names
    assert "jarvis_consult" in remaining_names
    assert "jarvis_turn_plan" in remaining_names
    assert frame["allowed_tools"] == ["jarvis_consult", "jarvis_turn_plan"]

    # Without a turn plan, tools must NOT be prematurely stripped
    sup.active_turn_plan = {}
    frame2 = {"tools": [{"name": "jarvis_python"}], "allowed_tools": ["jarvis_python"]}
    assert sup._apply_sage_overlap_restriction(frame2) is False
    assert frame2["allowed_tools"] == ["jarvis_python"]


def test_probe_strata_readiness_respects_unloaded_status() -> None:
    """probe_strata_readiness returns False when /v1/models reports status.value == 'unloaded'."""
    class _Resp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.status_code = 200
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

    class _Client:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str) -> _Resp:
            return _Resp(self._payload)

    unloaded_payload = {"data": [{"id": "m1", "status": {"value": "unloaded"}}]}
    loaded_payload = {"data": [{"id": "m1", "status": {"value": "loaded"}}]}
    legacy_payload = {"data": [{"id": "m1"}]}

    assert host_start.probe_strata_readiness(
        "127.0.0.1", 18091, "m1", client_factory=lambda **kw: _Client(unloaded_payload)
    ) is False
    assert host_start.probe_strata_readiness(
        "127.0.0.1", 18091, "m1", client_factory=lambda **kw: _Client(loaded_payload)
    ) is True
    assert host_start.probe_strata_readiness(
        "127.0.0.1", 18091, "m1", client_factory=lambda **kw: _Client(legacy_payload)
    ) is True
    assert bench.is_model_loaded_response(unloaded_payload, "m1") is False
    assert bench.is_model_loaded_response(loaded_payload, "m1") is True


def test_wait_strata_slots_idle_timeout_is_fail_closed() -> None:
    """wait_strata_slots_idle treats httpx.TimeoutException as busy (fail-closed), not idle."""
    import httpx

    from app.modules.consult.sage import wait_strata_slots_idle

    class _TimeoutClient:
        def __enter__(self) -> _TimeoutClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str) -> Any:
            raise httpx.ReadTimeout("busy")

    is_idle, wait_s = wait_strata_slots_idle(
        "127.0.0.1", 18091, timeout_s=0.05, poll_s=0.02, client_factory=lambda **kw: _TimeoutClient()
    )
    assert is_idle is False
    assert wait_s >= 0.04

