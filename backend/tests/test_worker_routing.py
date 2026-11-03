"""Worker router: difficulty x risk x capability selection over quota pools (offline, readers mocked)."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "worker_routing.py"
SPEC = importlib.util.spec_from_file_location("worker_routing_under_test", SCRIPT)
assert SPEC and SPEC.loader
wr = importlib.util.module_from_spec(SPEC)
sys.modules["worker_routing_under_test"] = wr
SPEC.loader.exec_module(wr)


def pool(name: str, five_hour: float, weekly: float | None = None) -> Any:
    windows = {"5h": five_hour, "weekly": five_hour if weekly is None else weekly}
    return wr.Pool(name, windows, min(windows.values()))


def pools(codex: Any = None, gemini: Any = None, claude: Any = None) -> dict[str, Any]:
    return {"codex": codex or pool("codex", 0.95), "gemini": gemini or pool("gemini", 0.99),
            "claude": claude or pool("claude", 0.60)}


def test_tier_is_the_maximum_of_difficulty_and_risk() -> None:
    assert wr.tier("low", "low") == 1
    assert wr.tier("high", "low") == 3
    assert wr.tier("low", "high") == 3
    assert wr.tier("medium", "low") == 2
    assert wr.tier("medium", "medium") == 2


def test_models_table_is_declarative_and_argon_free() -> None:
    assert [m.key for m in wr.MODELS] == ["luna", "flash", "haiku", "terra", "sonnet", "sol"]
    assert {m.kind for m in wr.MODELS} == {"codex", "agy", "claude"}
    by_key = {m.key: m for m in wr.MODELS}
    assert (by_key["haiku"].model, by_key["haiku"].pool, by_key["haiku"].capability) == ("claude-haiku-5-5", "claude", 1)
    assert (by_key["sonnet"].model, by_key["sonnet"].pool, by_key["sonnet"].capability) == ("claude-sonnet-5-5", "claude", 2)
    assert wr.RESERVES == {"claude": 0.03, "codex": 0.10, "gemini": 0.10}
    assert all(1 <= m.capability <= 3 for m in wr.MODELS)
    assert "argon" not in {m.key for m in wr.MODELS}  # spec 221: Argon is an agy worker only under --coordinator-fallback


def test_cheapest_adequate_model_wins_by_smallest_surplus() -> None:
    assert wr.decide("medium", "low", pools())["worker"] == "terra"  # terra (cap 2) beats sol (cap 3)
    assert wr.decide("high", "low", pools())["worker"] == "sol"
    low = wr.decide("low", "low", pools())
    assert low["worker"] == "flash"  # equal surplus: the pool with more headroom
    assert [a["worker"] for a in low["alternatives"]] == ["luna", "haiku", "terra", "sonnet", "sol"]


def test_pool_priority_then_headroom_then_table_order_break_a_capability_tie() -> None:
    # gemini, codex, claude: the pool priority outranks headroom
    assert wr.decide("low", "low", pools(gemini=pool("gemini", 0.15)))["worker"] == "flash"
    assert wr.decide("low", "low", pools(gemini=pool("gemini", 0.05)))["worker"] == "luna"
    # pools with no priority entry fall back to most headroom, then table order
    models = (wr.Model("a", "codex", "x", "p1", 1, 1), wr.Model("b", "codex", "y", "p2", 1, 1),
              wr.Model("c", "codex", "z", "p2", 1, 1))
    ranked, _ = wr.rank(1, {"p1": pool("p1", 0.5), "p2": pool("p2", 0.9)}, models)
    assert [e["worker"] for e in ranked] == ["b", "c", "a"]


def test_pool_free_fraction_is_the_minimum_over_its_windows() -> None:
    decision = wr.decide("low", "low", pools(gemini=pool("gemini", 1.0, 0.30)))
    assert decision["worker"] == "flash" and "luna" in [a["worker"] for a in decision["alternatives"]]
    assert wr.rank(1, pools(gemini=pool("gemini", 1.0, 0.30)))[0][0]["free"] == pytest.approx(0.30)  # the weekly window
    assert wr.decide("low", "low", pools(gemini=pool("gemini", 1.0, 0.05)))["worker"] == "luna"  # weekly under reserve


def test_pool_below_the_reserve_is_skipped() -> None:
    near = wr.decide("low", "low", pools(gemini=pool("gemini", 1.0, 0.0999)))
    assert near["worker"] == "luna"
    assert {"worker": "flash", "skipped": "below_reserve"}.items() <= next(s for s in near["skipped"] if s["worker"] == "flash").items()
    assert wr.decide("low", "low", pools(gemini=pool("gemini", 0.10)))["worker"] == "flash"  # exactly 10% is allowed


def test_unreadable_or_disabled_pool_fails_closed_for_that_pool_only() -> None:
    state = pools(gemini=wr.unreadable("gemini", "timeout"))
    low = wr.decide("low", "low", state)
    assert low["worker"] == "luna" and "flash" not in [a["worker"] for a in low["alternatives"]]
    assert next(s for s in low["skipped"] if s["worker"] == "flash")["skipped"] == "timeout"
    codex_down = pools(codex=wr.unreadable("codex", "no_reading"))
    assert wr.decide("medium", "low", codex_down)["worker"] == "sonnet"  # the claude pool still serves tier 2
    codex_down["claude"] = wr.unreadable("claude", "stale")
    assert wr.decide("medium", "low", codex_down)["status"] == "no_worker"
    missing = wr.decide("low", "low", {"codex": pool("codex", 0.9)})  # a pool with no reading at all
    assert missing["worker"] == "luna"


def test_no_worker_when_nothing_qualifies() -> None:
    decision = wr.decide("high", "medium", pools(codex=pool("codex", 0.05)))
    assert decision["status"] == "no_worker" and decision["reason"] == "no_worker"
    assert [s["worker"] for s in decision["skipped"]] == ["sol"]
    assert "worker" not in decision


def test_review_requirement_follows_risk() -> None:
    assert wr.decide("low", "high", pools())["review"] == "coordinator_full_diff_review"
    assert wr.decide("low", "medium", pools())["review"] == "coordinator_spot_check"
    assert wr.decide("high", "low", pools())["review"] == "tests_only"


def test_launch_commands() -> None:
    assert wr.decide("medium", "low", pools())["command"] == 'codex exec --model gpt-6.1-terra --cd "$WORKTREE" - < "$TASK_FILE"'
    assert wr.decide("low", "low", pools())["command"] == (
        'python3 scripts/agy_worker.py run --task-file "$TASK_FILE" --worktree "$WORKTREE" --model flash')
    claude = wr.launch_command(next(m for m in wr.MODELS if m.key == "sonnet"))
    assert claude == ('cd "$WORKTREE" && claude -p --model claude-sonnet-5-5 --permission-mode acceptEdits '
                      '--permission-prompts none --output-format json < "$TASK_FILE"')
    assert "bypassPermissions" not in claude and "dangerously" not in claude


def test_gemini_parse_requires_both_enabled_buckets() -> None:
    good = {"command": {"data": {"groups": [{"buckets": [
        {"id": "gemini-weekly", "remaining_fraction": 0.98}, {"id": "gemini-5h", "remaining_fraction": 1}]},
        {"buckets": [{"id": "3p-weekly", "remaining_fraction": 0}]}]}}}
    parsed = wr.parse_gemini(good)
    assert parsed.free == pytest.approx(0.98) and parsed.windows == {"5h": 1.0, "weekly": pytest.approx(0.98)}
    only_weekly = {"buckets": [{"id": "gemini-weekly", "remaining_fraction": 0.9}]}
    assert wr.parse_gemini(only_weekly).free is None
    disabled = {"buckets": [{"id": "gemini-weekly", "remaining_fraction": 0.9},
                            {"id": "gemini-5h", "remaining_fraction": 1, "disabled": True}]}
    assert wr.parse_gemini(disabled) == wr.Pool("gemini", {}, None, "5h_disabled")
    bad = {"buckets": [{"id": "gemini-weekly", "remaining_fraction": 1.5}, {"id": "gemini-5h", "remaining_fraction": 1}]}
    assert wr.parse_gemini(bad).reason == "invalid_reading"
    assert wr.parse_gemini(None).free is None


def test_codex_parse_uses_primary_as_5h_and_secondary_as_weekly() -> None:
    parsed = wr.parse_codex({"primary": {"usedPercent": 10}, "secondary": {"usedPercent": 40}})
    assert parsed.windows == {"5h": pytest.approx(0.9), "weekly": pytest.approx(0.6)} and parsed.free == pytest.approx(0.6)
    assert wr.parse_codex({"primary": {"usedPercent": 5}}).free == pytest.approx(0.95)
    for broken in (None, {}, {"primary": {"usedPercent": "x"}}, {"primary": {"usedPercent": 120}},
                   {"primary": {"usedPercent": 1}, "secondary": "bad"}):
        assert wr.parse_codex(broken).free is None


def test_summary_line_formatting() -> None:
    line = wr.summary_line(pools(codex=pool("codex", 1.0, 0.95), gemini=pool("gemini", 1.0, 0.99)))
    assert line == ("worker quota: codex 95% free (5h 100%) · gemini 99% free (5h 100%) · claude 60% free (5h 60%)"
                    " — prefer cheapest adequate pool")
    degraded = wr.summary_line(pools(codex=pool("codex", 0.05), gemini=wr.unreadable("gemini", "timeout")))
    assert "codex 5% free (5h 5%) [below reserve]" in degraded and "gemini unavailable (timeout)" in degraded
    assert "\n" not in line


def test_read_pools_survives_a_raising_and_a_hanging_reader() -> None:
    def broken() -> Any:
        raise RuntimeError("boom")

    def hanging() -> Any:
        time.sleep(30)

    started = time.monotonic()
    result = wr.read_pools({"codex": broken, "gemini": hanging}, timeout=0.3)
    assert time.monotonic() - started < 5
    assert result["codex"].reason == "reader_error" and result["gemini"].reason == "timeout"
    assert all(p.free is None for p in result.values())


def test_cached_summary_caches_and_never_raises() -> None:
    calls: list[int] = []
    now = {"t": 1000.0}

    def read() -> Any:
        calls.append(1)
        return pools()

    wr._cache.update(at=None, line=None, ttl=0.0)
    first = wr.cached_summary(clock=lambda: now["t"], read=read)
    now["t"] += 600
    assert wr.cached_summary(clock=lambda: now["t"], read=read) == first and len(calls) == 1
    now["t"] += 400  # past 15 minutes
    wr.cached_summary(clock=lambda: now["t"], read=read)
    assert len(calls) == 2

    def explode() -> Any:
        raise OSError("no agy")

    wr._cache.update(at=None, line=None, ttl=0.0)
    assert wr.cached_summary(clock=lambda: now["t"], read=explode) == "worker quota: unreadable"
    now["t"] += 61  # a failed reading is retried after a minute, not 15
    assert wr.cached_summary(clock=lambda: now["t"], read=read).startswith("worker quota: codex")
    wr._cache.update(at=None, line=None, ttl=0.0)


def test_cli_pick_prints_and_exits(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                   tmp_path: Path) -> None:
    monkeypatch.setattr(wr, "read_pools", lambda: pools())
    nothing = ["--quota-file", str(tmp_path / "none.json")]
    assert wr.main(["pick", "--difficulty", "medium", "--risk", "low", *nothing]) == 0
    out = capsys.readouterr().out
    assert "worker: terra" in out and "review: tests_only" in out and "alternatives: sonnet (claude 60% free), sol" in out
    assert "note:" not in out
    assert wr.main(["pick", "--difficulty", "low", "--risk", "high", "--json", *nothing]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["worker"] == "sol" and data["source"] == "live" and data["required_tier"] == 3 and data["review"] == "coordinator_full_diff_review"


def test_cli_pick_exits_three_when_no_worker(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                             tmp_path: Path) -> None:
    monkeypatch.setattr(wr, "read_pools", lambda: {"codex": wr.unreadable("codex", "timeout"), "gemini": pool("gemini", 0.5)})
    nothing = ["--quota-file", str(tmp_path / "none.json")]
    assert wr.main(["pick", "--difficulty", "high", "--risk", "high", *nothing]) == wr.NO_WORKER_EXIT == 3
    assert "no_worker" in capsys.readouterr().out
    assert wr.main(["pick", "--difficulty", "high", "--risk", "high", "--json", *nothing]) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "no_worker"


def test_cli_rejects_unknown_levels_and_summary_never_fails(monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        wr.main(["pick", "--difficulty", "extreme", "--risk", "low"])
    assert exit_info.value.code == 2
    capsys.readouterr()
    wr._cache.update(at=None, line=None, ttl=0.0)
    monkeypatch.setattr(wr, "read_pools", lambda: pools())
    assert wr.main(["summary", "--quota-file", str(tmp_path / "none.json")]) == 0
    assert capsys.readouterr().out.startswith("worker quota: codex 95% free")
    wr._cache.update(at=None, line=None, ttl=0.0)


# ---------------------------------------------------------------------------
# Supervisor snapshot (the sandbox fallback)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def snapshot_file(path: Path, *, age_s: float = 60, codex: tuple[float, float] = (1.0, 0.80), schema: str = wr.SNAPSHOT_SCHEMA) -> Path:
    at = wr._iso(NOW - timedelta(seconds=age_s))
    path.write_text(json.dumps({"schema": schema, "as_of": at, "pools": {
        "codex": {"as_of": at, "windows": {"5h": codex[0], "weekly": codex[1]}}}}))
    return path


def test_snapshot_is_written_on_a_successful_reading_only(tmp_path: Path) -> None:
    target = tmp_path / ".agent-relay" / "worker-quota.json"
    wr._cache.update(at=None, line=None, ttl=0.0)
    wr.cached_summary(read=lambda: pools(codex=pool("codex", 1.0, 0.95)), snapshot_file=target)
    data = json.loads(target.read_text())
    assert data["schema"] == wr.SNAPSHOT_SCHEMA and wr._parse_ts(data["as_of"]) is not None
    assert data["pools"]["codex"]["windows"] == {"5h": 1.0, "weekly": 0.95}
    assert set(data["pools"]) == {"codex", "gemini", "claude"} and wr._parse_ts(data["pools"]["gemini"]["as_of"]) is not None
    assert not [p for p in target.parent.iterdir() if p != target]  # atomic: no temp file left behind

    before = target.read_text()
    wr._cache.update(at=None, line=None, ttl=0.0)
    wr.cached_summary(read=lambda: {n: wr.unreadable(n, "timeout") for n in ("codex", "gemini")}, snapshot_file=target)
    assert target.read_text() == before  # an all-unreadable reading never touches the last good snapshot

    def explode() -> Any:
        raise OSError("no agy")

    wr._cache.update(at=None, line=None, ttl=0.0)
    assert wr.cached_summary(read=explode, snapshot_file=target) == "worker quota: unreadable"
    assert target.read_text() == before
    fresh = tmp_path / "never.json"
    wr._cache.update(at=None, line=None, ttl=0.0)
    wr.cached_summary(read=explode, snapshot_file=fresh)
    assert not fresh.exists()
    wr._cache.update(at=None, line=None, ttl=0.0)


def test_partial_reading_keeps_the_other_pools_older_entry(tmp_path: Path) -> None:
    target = snapshot_file(tmp_path / "q.json", age_s=600)
    assert wr.write_snapshot(target, {"codex": wr.unreadable("codex", "timeout"), "gemini": pool("gemini", 0.9)}, NOW)
    data = json.loads(target.read_text())
    assert data["pools"]["gemini"]["as_of"] == wr._iso(NOW)
    assert data["pools"]["codex"]["as_of"] == wr._iso(NOW - timedelta(seconds=600))  # not refreshed by a failure
    assert not wr.write_snapshot(target, {"codex": wr.Pool("codex", {"5h": 1.0}, 1.0, None, "snapshot")}, NOW)  # never re-snapshot a snapshot


def test_fresh_snapshot_is_the_fallback_for_an_unreadable_pool(tmp_path: Path) -> None:
    live = {"codex": wr.unreadable("codex", "timeout"), "gemini": pool("gemini", 0.99)}
    resolved = wr.apply_snapshot(live, wr.load_snapshot([snapshot_file(tmp_path / "q.json", age_s=1799)]), NOW)
    assert resolved["codex"].source == "snapshot" and resolved["codex"].free == pytest.approx(0.80)
    assert resolved["gemini"].source == "live"
    decision = wr.decide("medium", "low", resolved)
    assert decision["worker"] == "terra" and decision["source"] == "snapshot"
    assert "[snapshot]" in wr.summary_line(resolved)


def test_stale_missing_or_malformed_snapshot_is_ignored(tmp_path: Path) -> None:
    live = {"codex": wr.unreadable("codex", "timeout"), "gemini": pool("gemini", 0.99)}
    for case in (snapshot_file(tmp_path / "old.json", age_s=1801), snapshot_file(tmp_path / "future.json", age_s=-3600),
                 snapshot_file(tmp_path / "schema.json", schema="other.v9"), tmp_path / "missing.json"):
        assert wr.apply_snapshot(live, wr.load_snapshot([case]), NOW)["codex"].free is None
    (tmp_path / "junk.json").write_text("{not json")
    (tmp_path / "bad.json").write_text(json.dumps({"schema": wr.SNAPSHOT_SCHEMA, "pools": {
        "codex": {"as_of": wr._iso(NOW), "windows": {"5h": 7}}}}))
    assert wr.load_snapshot([tmp_path / "junk.json", tmp_path / "bad.json"]) == {}
    assert wr.decide("medium", "low", wr.apply_snapshot(live, {}, NOW))["status"] == "no_worker"  # fail-closed as before


def test_live_reading_wins_over_the_snapshot_and_disabled_never_falls_back(tmp_path: Path) -> None:
    snap = wr.load_snapshot([snapshot_file(tmp_path / "q.json", codex=(1.0, 1.0))])
    live = {"codex": pool("codex", 0.4), "gemini": pool("gemini", 0.99)}
    resolved = wr.apply_snapshot(live, snap, NOW)
    assert resolved["codex"].free == pytest.approx(0.4) and resolved["codex"].source == "live"
    disabled = {"codex": wr.Pool("codex", {}, None, "5h_disabled"), "gemini": pool("gemini", 0.99)}
    assert wr.apply_snapshot(disabled, snap, NOW)["codex"].free is None


def test_resolve_pools_reads_live_first_and_cli_marks_the_snapshot_source(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    fresh = wr._iso(datetime.now(UTC))
    quota = tmp_path / "q.json"
    quota.write_text(json.dumps({"schema": wr.SNAPSHOT_SCHEMA, "as_of": fresh, "pools": {
        "codex": {"as_of": fresh, "windows": {"5h": 1.0, "weekly": 0.7}}}}))
    monkeypatch.setattr(wr, "read_pools", lambda: {"codex": wr.unreadable("codex", "timeout"), "gemini": pool("gemini", 0.99)})
    assert wr.main(["pick", "--difficulty", "medium", "--risk", "low", "--json", "--quota-file", str(quota)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["worker"] == "terra" and data["source"] == "snapshot"
    assert {a["worker"]: a["source"] for a in data["alternatives"]} == {"sol": "snapshot"}
    assert wr.main(["pick", "--difficulty", "medium", "--risk", "low", "--quota-file", str(quota)]) == 0
    assert "quota from snapshot" in capsys.readouterr().out
    monkeypatch.setattr(wr, "read_pools", lambda: pools(codex=pool("codex", 0.5)))  # live now readable: it wins
    assert wr.main(["pick", "--difficulty", "medium", "--risk", "low", "--json", "--quota-file", str(quota)]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "live"
    wr._cache.update(at=None, line=None, ttl=0.0)


# ---------------------------------------------------------------------------
# Claude Code workers (haiku, sonnet) on the coordinator's own pool

LOW_CODEX_GEMINI = {"codex": pool("codex", 0.05), "gemini": pool("gemini", 0.05)}


def test_claude_workers_are_ordered_after_gemini_and_codex_at_equal_surplus() -> None:
    low = wr.decide("low", "low", pools(claude=pool("claude", 1.0), gemini=pool("gemini", 0.2)))
    assert [low["worker"], *[a["worker"] for a in low["alternatives"]][:2]] == ["flash", "luna", "haiku"]
    medium = wr.decide("medium", "low", pools(claude=pool("claude", 1.0)))
    assert [medium["worker"], medium["alternatives"][0]["worker"]] == ["terra", "sonnet"]  # more headroom does not help
    assert medium["claude_pool_low"] is False and "note" not in medium


def test_claude_worker_is_chosen_when_the_cheaper_pools_are_below_reserve() -> None:
    low = wr.decide("low", "low", pools(claude=pool("claude", 0.8), **LOW_CODEX_GEMINI))
    assert low["worker"] == "haiku" and low["pool"] == "claude" and low["claude_pool_low"] is False
    medium = wr.decide("medium", "low", pools(claude=pool("claude", 0.8), **LOW_CODEX_GEMINI))
    assert medium["worker"] == "sonnet" and medium["command"].startswith('cd "$WORKTREE" && claude -p --model claude-sonnet-5-5')
    assert wr.decide("high", "low", pools(claude=pool("claude", 0.8), **LOW_CODEX_GEMINI))["status"] == "no_worker"  # cap 3 is sol only


def test_claude_pool_is_not_skipped_at_forty_percent_but_carries_a_low_pool_note(capsys: pytest.CaptureFixture[str]) -> None:
    state = pools(claude=pool("claude", 0.35), **LOW_CODEX_GEMINI)
    decision = wr.decide("medium", "low", state)
    assert decision["worker"] == "sonnet" and decision["claude_pool_low"] is True
    assert decision["note"].startswith("claude_pool_low — keep worker task small")
    assert "prefer waiting for a cheaper pool reset if the task is not urgent" in decision["note"]
    assert "note: claude_pool_low" in wr.render(decision)
    assert wr.decide("medium", "low", pools(claude=pool("claude", 0.40), **LOW_CODEX_GEMINI))["claude_pool_low"] is False
    # the note rides on the chosen Claude worker only
    assert wr.decide("medium", "low", pools(claude=pool("claude", 0.35)))["claude_pool_low"] is False


def test_claude_pool_is_skipped_below_three_percent() -> None:
    state = pools(claude=pool("claude", 0.029), **LOW_CODEX_GEMINI)
    decision = wr.decide("low", "low", state)
    assert decision["status"] == "no_worker"
    assert {s["worker"]: s["skipped"] for s in decision["skipped"]}["haiku"] == "below_reserve"
    assert wr.decide("low", "low", pools(claude=pool("claude", 0.03), **LOW_CODEX_GEMINI))["worker"] == "haiku"
    assert "claude 3% free" in wr.summary_line(pools(claude=pool("claude", 0.03)))
    assert "[below reserve]" in wr.summary_line(pools(claude=pool("claude", 0.02)))
    assert "[below reserve]" not in wr.summary_line(pools(claude=pool("claude", 0.30)))  # 30% is above the claude reserve


def entry(as_of: Any, **extra: Any) -> dict[str, Any]:
    return {"agent_key": "claude", "status": "ok", "as_of": as_of, "windows": [
        {"id": "session", "used_percent": 20.0}, {"id": "weekly", "used_percent": 45.0}], **extra}


def test_claude_reading_must_be_fresh_and_ok_or_the_pool_is_unreadable() -> None:
    fresh = wr.parse_claude(entry(wr._iso(NOW - timedelta(minutes=14))), NOW)
    assert fresh.windows == {"5h": pytest.approx(0.8), "weekly": pytest.approx(0.55)} and fresh.free == pytest.approx(0.55)
    assert wr.parse_claude(entry("2026-11-02T11:59:30.250Z"), NOW).free is not None  # ms and Z are accepted
    assert wr.parse_claude(entry("2026-11-02T12:00:00+00:00"), NOW).free is not None
    stale = wr.parse_claude(entry(wr._iso(NOW - timedelta(minutes=16))), NOW)
    assert stale.free is None and stale.reason == "stale"
    assert wr.parse_claude(entry(wr._iso(NOW + timedelta(minutes=16))), NOW).reason == "stale"
    expired = wr.parse_claude({"agent_key": "claude", "status": "unavailable", "windows": [], "as_of": None,
                               "reason": "Claude Code session expired. Sign in with `claude`."}, NOW)
    assert expired.free is None and expired.reason == "unavailable"
    for broken in (None, "x", entry(None), entry("not a time"), {**entry(wr._iso(NOW)), "windows": []},
                   {**entry(wr._iso(NOW)), "windows": [{"id": "session", "used_percent": "20"}]},
                   {**entry(wr._iso(NOW)), "windows": [{"id": "session", "used_percent": 101}]}):
        assert wr.parse_claude(broken, NOW).free is None


def test_stale_claude_reading_fails_closed_in_the_decision() -> None:
    stale = wr.parse_claude(entry(wr._iso(NOW - timedelta(hours=1))), NOW)
    decision = wr.decide("medium", "low", pools(claude=stale, **LOW_CODEX_GEMINI))
    assert decision["status"] == "no_worker"
    assert {s["worker"]: s["skipped"] for s in decision["skipped"]}["sonnet"] == "stale"


def test_read_claude_uses_the_relay_limits_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stamp = wr._iso(datetime.now(UTC))
    document = {"command": "limits", "agents": [{"agent_key": "codex", "status": "ok", "windows": []},
                                                 entry(stamp)]}
    fake = tmp_path / "agent-relay"
    fake.write_text("#!/bin/sh\n[ \"$1 $2 $3\" = '--json limits --refresh' ] || exit 9\ncat <<'EOF'\n"
                    + json.dumps(document) + "\nEOF\n")
    fake.chmod(0o755)
    monkeypatch.setenv("JARVISOS_AGENT_RELAY_BIN", str(fake))
    reading = wr.read_claude()
    assert reading.free == pytest.approx(0.55) and reading.source == "live"
    fake.write_text("#!/bin/sh\necho '{\"agents\": []}'\n")
    assert wr.read_claude().free is None
    monkeypatch.setenv("JARVISOS_AGENT_RELAY_BIN", str(tmp_path / "missing"))
    assert wr.read_claude().free is None


def test_read_gemini_reads_quota_and_waits_longer_than_other_readers(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    document = {"status": "SUCCESS", "command": {"data": {"groups": [{"buckets": [
        {"id": "gemini-weekly", "remaining_fraction": 0.98}, {"id": "gemini-5h", "remaining_fraction": 1}]},
        {"buckets": [{"id": "3p-weekly", "remaining_fraction": 0}]}]}}}
    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\n[ \"$1 $2 $3 $4\" = '-p /quota --output-format json' ] || exit 9\ncat <<'EOF'\n"
                    + json.dumps(document) + "\nEOF\n")
    fake.chmod(0o755)
    monkeypatch.setenv("JARVISOS_AGY_BIN", str(fake))
    reading = wr.read_gemini()
    assert reading.free == pytest.approx(0.98) and reading.windows == {"5h": 1.0, "weekly": 0.98}
    assert reading.source == "live"

    fake.write_text("#!/bin/sh\nsleep 0.5\ncat <<'EOF'\n" + json.dumps(document) + "\nEOF\n")
    fake.chmod(0o755)
    monkeypatch.setattr(wr, "READ_TIMEOUT", 0.2)
    monkeypatch.setattr(wr, "AGY_READ_TIMEOUT", 3)
    assert wr.read_gemini().free == pytest.approx(0.98)


@pytest.mark.parametrize("sleep_after_auth", [False, True])
def test_read_gemini_reports_auth_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                           sleep_after_auth: bool) -> None:
    fake = tmp_path / "agy"
    suffix = "sleep 2\n" if sleep_after_auth else "exit 1\n"
    fake.write_text("#!/bin/sh\necho 'Authentication required. Please visit the URL to log in:'\n" + suffix)
    fake.chmod(0o755)
    monkeypatch.setenv("JARVISOS_AGY_BIN", str(fake))
    if sleep_after_auth:
        monkeypatch.setattr(wr, "AGY_READ_TIMEOUT", 0.3)
    assert wr.read_gemini().reason == "auth_required"


def test_total_timeout_covers_agy_reader() -> None:
    assert wr.TOTAL_TIMEOUT > wr.AGY_READ_TIMEOUT


def test_read_gemini_reuses_prepare_login_and_reports_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / ".local/share/jarvisos-sandbox/state/agy-home"
    target.mkdir(parents=True)
    document = {"status": "SUCCESS", "command": {"data": {"groups": [{"buckets": [
        {"id": "gemini-weekly", "remaining_fraction": 0.98}, {"id": "gemini-5h", "remaining_fraction": 1}]}]}}}
    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\ncat <<'EOF'\n" + json.dumps(document) + "\nEOF\n")
    fake.chmod(0o755)
    monkeypatch.setenv("JARVISOS_AGY_BIN", str(fake))

    reading = wr.read_gemini()
    assert (tmp_path / ".gemini").is_symlink()
    assert (tmp_path / ".gemini").resolve() == target.resolve()
    assert reading.free == pytest.approx(0.98) and reading.windows == {"5h": 1.0, "weekly": 0.98}

    (tmp_path / ".gemini").unlink()
    (tmp_path / ".gemini").mkdir()
    assert wr.read_gemini().reason == "login_path_conflict"
