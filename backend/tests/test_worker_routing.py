"""Worker router: difficulty x risk x capability selection over quota pools (offline, readers mocked)."""
from __future__ import annotations

import importlib.util
import json
import math
import subprocess
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


@pytest.fixture(autouse=True)
def _fresh_agy_reading() -> Any:
    wr._agy_doc.update(at=None, reading=None)
    yield
    wr._agy_doc.update(at=None, reading=None)


def pool(name: str, five_hour: float, weekly: float | None = None) -> Any:
    windows = {"5h": five_hour, "weekly": five_hour if weekly is None else weekly}
    return wr.Pool(name, windows, min(windows.values()))


def pools(codex: Any = None, gemini: Any = None, claude: Any = None, copilot: Any = None, argon: Any = None,
          agy3p: Any = None) -> dict[str, Any]:
    return {"codex": codex or pool("codex", 0.95), "gemini": gemini or pool("gemini", 0.99),
            "claude": claude or pool("claude", 0.60), "copilot": copilot or wr.unreadable("copilot", "no_reading"),
            "argon": argon or wr.unreadable("argon", "no_reading"), "agy3p": agy3p or wr.unreadable("agy3p", "no_reading")}


def test_tier_is_the_maximum_of_difficulty_and_risk() -> None:
    assert wr.tier("low", "low") == 1
    assert wr.tier("high", "low") == 3
    assert wr.tier("low", "high") == 3
    assert wr.tier("medium", "low") == 2
    assert wr.tier("medium", "medium") == 2


def test_models_table_is_declarative_with_effort_tiers() -> None:
    assert [m.key for m in wr.MODELS] == ["luna", "flash", "hydrogen-low", "copilot-auto", "haiku",
                                          "flash-medium", "hydrogen-medium", "argon-medium",
                                          "terra", "sonnet", "flash-high", "hydrogen-high",
                                          "argon-high", "agy-sonnet", "agy-opus", "sol"]
    assert {m.kind for m in wr.MODELS} == {"codex", "agy", "copilot", "claude"}
    by_key = {m.key: m for m in wr.MODELS}
    assert (by_key["haiku"].model, by_key["haiku"].pool, by_key["haiku"].capability) == ("claude-haiku-5-5", "claude", 1)
    assert (by_key["sonnet"].model, by_key["sonnet"].pool, by_key["sonnet"].capability) == ("claude-sonnet-5-5", "claude", 2)
    assert wr.RESERVES == {"claude": 0.03, "codex": 0.05, "copilot": 0.05, "gemini": 0.05, "argon": 0.05, "agy3p": 0.05}
    assert all(1 <= m.capability <= 3 for m in wr.MODELS)
    # spec 255: no brand bias; Flash and Argon at high effort take tier 3 until measured
    assert {m.key: (m.pool, m.capability) for m in wr.MODELS if m.kind == "agy"} == {
        "flash": ("gemini", 1), "hydrogen-low": ("gemini", 1),
        "flash-medium": ("gemini", 2), "hydrogen-medium": ("gemini", 2),
        "flash-high": ("gemini", 3), "hydrogen-high": ("gemini", 3),
        "argon-medium": ("argon", 2), "argon-high": ("argon", 3), "agy-sonnet": ("agy3p", 3), "agy-opus": ("agy3p", 3)}
    assert {wr.family(m) for m in wr.MODELS if m.pool == "agy3p"} == {"claude"}


def test_cheapest_adequate_model_wins_by_smallest_surplus() -> None:
    assert wr.decide("medium", "low", pools())["worker"] == "flash-medium"  # effort by task: medium, not high
    assert wr.decide("high", "low", pools())["worker"] == "flash-high"
    low = wr.decide("low", "low", pools())
    assert low["worker"] == "flash"  # equal surplus: the gemini family first
    assert [a["worker"] for a in low["alternatives"]] == ["hydrogen-low", "luna", "haiku", "flash-medium",
                                                          "hydrogen-medium", "terra", "sonnet",
                                                          "flash-high", "hydrogen-high", "sol"]
    no_gemini = pools(gemini=pool("gemini", 0.0))
    assert wr.decide("medium", "low", no_gemini)["worker"] == "terra"  # terra (cap 2) beats sol (cap 3)
    assert wr.decide("high", "low", no_gemini)["worker"] == "sol"


def test_pool_priority_then_headroom_then_table_order_break_a_capability_tie() -> None:
    # gemini, codex, claude: the pool priority outranks headroom
    assert wr.decide("low", "low", pools(gemini=pool("gemini", 0.15)))["worker"] == "flash"
    assert wr.decide("low", "low", pools(gemini=pool("gemini", 0.04)))["worker"] == "luna"
    # other pools: quota that would expire unused first, then most headroom, then table order
    models = (wr.Model("a", "codex", "x", "p1", 1), wr.Model("b", "codex", "y", "p2", 1),
              wr.Model("c", "codex", "z", "p2", 1))
    ranked, _ = wr.rank(1, {"p1": pool("p1", 0.5), "p2": pool("p2", 0.9)}, models)
    assert [e["worker"] for e in ranked] == ["b", "c", "a"]


def test_pool_free_fraction_is_the_minimum_over_its_windows() -> None:
    decision = wr.decide("low", "low", pools(gemini=pool("gemini", 1.0, 0.30)))
    assert decision["worker"] == "flash" and "luna" in [a["worker"] for a in decision["alternatives"]]
    assert wr.rank(1, pools(gemini=pool("gemini", 1.0, 0.30)))[0][0]["free"] == pytest.approx(0.30)  # the weekly window
    assert wr.decide("low", "low", pools(gemini=pool("gemini", 1.0, 0.04)))["worker"] == "luna"  # weekly under reserve


def test_pool_below_the_reserve_is_skipped() -> None:
    near = wr.decide("low", "low", pools(gemini=pool("gemini", 1.0, 0.0499)))
    assert near["worker"] == "luna"
    assert {"worker": "flash", "skipped": "below_reserve"}.items() <= next(s for s in near["skipped"] if s["worker"] == "flash").items()
    assert wr.decide("low", "low", pools(gemini=pool("gemini", 0.05)))["worker"] == "flash"  # exactly 5% is allowed


def test_unreadable_or_disabled_pool_fails_closed_for_that_pool_only() -> None:
    state = pools(gemini=wr.unreadable("gemini", "timeout"))
    low = wr.decide("low", "low", state)
    assert low["worker"] == "luna" and "flash" not in [a["worker"] for a in low["alternatives"]]
    assert next(s for s in low["skipped"] if s["worker"] == "flash")["skipped"] == "timeout"
    codex_down = pools(codex=wr.unreadable("codex", "no_reading"), gemini=wr.unreadable("gemini", "timeout"))
    assert wr.decide("medium", "low", codex_down)["worker"] == "sonnet"  # the claude pool still serves tier 2
    codex_down["claude"] = wr.unreadable("claude", "stale")
    assert wr.decide("medium", "low", codex_down)["status"] == "no_worker"
    missing = wr.decide("low", "low", {"codex": pool("codex", 0.9)})  # a pool with no reading at all
    assert missing["worker"] == "luna"


def test_no_worker_when_nothing_qualifies() -> None:
    decision = wr.decide("high", "medium", pools(codex=pool("codex", 0.04), gemini=pool("gemini", 0.04)))
    assert decision["status"] == "no_worker" and decision["reason"] == "no_worker"
    assert [s["worker"] for s in decision["skipped"]] == ["flash-high", "hydrogen-high", "argon-high", "agy-sonnet", "agy-opus", "sol"]
    assert "worker" not in decision


def test_flash_ranks_before_hydrogen_and_hydrogen_skipped_below_reserve() -> None:
    for req_tier, flash_key, hydro_key in [
        (1, "flash", "hydrogen-low"),
        (2, "flash-medium", "hydrogen-medium"),
        (3, "flash-high", "hydrogen-high"),
    ]:
        ranked, _ = wr.rank(req_tier, pools())
        workers = [e["worker"] for e in ranked]
        assert flash_key in workers and hydro_key in workers
        assert workers.index(flash_key) < workers.index(hydro_key)

    below = pools(gemini=pool("gemini", 0.04))
    for req_tier, hydro_key in [
        (1, "hydrogen-low"),
        (2, "hydrogen-medium"),
        (3, "hydrogen-high"),
    ]:
        ranked, skipped = wr.rank(req_tier, below)
        assert hydro_key not in [e["worker"] for e in ranked]
        hydro_skip = next(s for s in skipped if s["worker"] == hydro_key)
        assert hydro_skip["skipped"] == "below_reserve"


def test_review_requirement_follows_risk() -> None:
    assert wr.decide("low", "high", pools())["review"] == "coordinator_full_diff_review"
    assert wr.decide("low", "medium", pools())["review"] == "coordinator_spot_check"
    assert wr.decide("high", "low", pools())["review"] == "tests_only"


def test_launch_commands() -> None:
    assert wr.bare_command(next(m for m in wr.MODELS if m.key == "terra")) == 'codex exec --model gpt-6.1-terra --cd "$WORKTREE" - < "$TASK_FILE"'
    assert wr.bare_command(next(m for m in wr.MODELS if m.key == "flash")) == (
        'python3 scripts/agy_worker.py run --task-file "$TASK_FILE" --worktree "$WORKTREE" --model flash')
    claude = wr.bare_command(next(m for m in wr.MODELS if m.key == "sonnet"))
    assert claude == ('cd "$WORKTREE" && claude -p --model claude-sonnet-5-5 --permission-mode acceptEdits '
                      '--permission-prompts none --output-format json < "$TASK_FILE"')
    assert "bypassPermissions" not in claude and "dangerously" not in claude
    for model in wr.MODELS:
        command = wr.launch_command(model)
        assert command.startswith('python3 scripts/relay_jobs.py start --label "$LABEL" --authority "$AUTHORITY" --spec "$SPEC" '
                                  '--wall-hours "${WALL_HOURS:-1}" --cwd "$WORKTREE" --input "$TASK_FILE" -- bash -c ')
        assert wr.bare_command(model) in command


def test_launch_command_shell_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wr, "bare_command", lambda model: 'printf "%s\\n" "$WORKTREE" "$TASK_FILE"')
    command = wr.launch_command(wr.MODELS[0])
    # The stub stands in for relay_jobs.py start: it records the wrapper arguments and runs what follows "--".
    script = ("python3() { while [ \"$1\" != -- ]; do printf 'arg %s\\n' \"$1\"; shift; done; shift; \"$@\"; }\n"
              "export WORKTREE='/tmp/work tree' TASK_FILE='/tmp/task file' LABEL='test job' AUTHORITY=spec SPEC=243\n"
              + command)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines()
    assert lines[-2:] == ["/tmp/work tree", "/tmp/task file"]
    args = [line[4:] for line in lines if line.startswith("arg ")]
    assert args[args.index("--input") + 1] == "/tmp/task file" and args[args.index("--wall-hours") + 1] == "1"
    assert args[args.index("--label") + 1] == "test job" and args[args.index("--cwd") + 1] == "/tmp/work tree"


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
    assert line.startswith("worker quota: codex 95% free (5h 100%) · gemini 99% free (5h 100%) · copilot unavailable "
                           "(no_reading) · claude 60% free (5h 60%) · argon unavailable (no_reading) · agy3p unavailable "
                           "(no_reading) — effort by task tier, gemini first, then quota expiring unused; tiers: luna t1, ")
    assert "flash-high t3, hydrogen-high t3, argon-high t3, agy-sonnet t3, agy-opus t3, sol t3" in line
    degraded = wr.summary_line(pools(codex=pool("codex", 0.04), gemini=wr.unreadable("gemini", "timeout")))
    assert "codex 4% free (5h 4%) [below reserve]" in degraded and "gemini unavailable (timeout)" in degraded
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
    monkeypatch.setattr(wr, "read_pools", lambda: pools(gemini=pool("gemini", 0.0)))
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
    monkeypatch.setattr(wr, "read_pools", lambda: {"codex": wr.unreadable("codex", "timeout"), "gemini": pool("gemini", 0.0)})
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
    live = {"codex": wr.unreadable("codex", "timeout"), "gemini": pool("gemini", 0.0)}
    resolved = wr.apply_snapshot(live, wr.load_snapshot([snapshot_file(tmp_path / "q.json", age_s=1799)]), NOW)
    assert resolved["codex"].source == "snapshot" and resolved["codex"].free == pytest.approx(0.80)
    assert resolved["gemini"].source == "live"
    decision = wr.decide("medium", "low", resolved)
    assert decision["worker"] == "terra" and decision["source"] == "snapshot"
    assert "[snapshot]" in wr.summary_line(resolved)


def test_stale_missing_or_malformed_snapshot_is_ignored(tmp_path: Path) -> None:
    live = {"codex": wr.unreadable("codex", "timeout"), "gemini": pool("gemini", 0.0)}
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
    monkeypatch.setattr(wr, "read_pools", lambda: {"codex": wr.unreadable("codex", "timeout"), "gemini": pool("gemini", 0.0)})
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

LOW_CODEX_GEMINI = {"codex": pool("codex", 0.04), "gemini": pool("gemini", 0.04)}


def test_gemini_first_then_quota_expiring_unused_at_equal_surplus() -> None:
    low = wr.decide("low", "low", pools(claude=pool("claude", 1.0), gemini=pool("gemini", 0.2)))
    assert low["worker"] == "flash"  # the gemini family first, whatever the headroom elsewhere
    # no fixed claude-last order: with equal (unknown) resets, more free room ranks first
    assert [a["worker"] for a in low["alternatives"]][:3] == ["hydrogen-low", "haiku", "luna"]
    now = datetime.now(UTC)
    soon = wr.Pool("claude", {"5h": 0.9, "weekly": 0.4}, 0.4, resets={"weekly": now + timedelta(hours=6)})
    later = wr.Pool("codex", {"5h": 0.9, "weekly": 0.7}, 0.7, resets={"weekly": now + timedelta(days=6)})
    medium = wr.decide("medium", "low", pools(claude=soon, codex=later, gemini=pool("gemini", 0.0)))
    assert [medium["worker"], medium["alternatives"][0]["worker"]] == ["sonnet", "terra"]  # 40% in 6 h beats 70% in 6 d
    assert medium["claude_pool_low"] is False


def test_claude_worker_is_chosen_when_the_cheaper_pools_are_below_reserve() -> None:
    low = wr.decide("low", "low", pools(claude=pool("claude", 0.8), **LOW_CODEX_GEMINI))
    assert low["worker"] == "haiku" and low["pool"] == "claude" and low["claude_pool_low"] is False
    medium = wr.decide("medium", "low", pools(claude=pool("claude", 0.8), **LOW_CODEX_GEMINI))
    assert medium["worker"] == "sonnet" and 'cd "$WORKTREE" && claude -p --model claude-sonnet-5-5' in medium["command"]
    assert wr.decide("high", "low", pools(claude=pool("claude", 0.8), **LOW_CODEX_GEMINI))["status"] == "no_worker"  # no tier-3 pool


def test_claude_pool_is_not_skipped_at_forty_percent_but_carries_a_low_pool_note(capsys: pytest.CaptureFixture[str]) -> None:
    state = pools(claude=pool("claude", 0.35), **LOW_CODEX_GEMINI)
    decision = wr.decide("medium", "low", state)
    assert decision["worker"] == "sonnet" and decision["claude_pool_low"] is True
    assert decision["note"].startswith("claude_pool_low — keep worker task small")
    assert "prefer waiting for a cheaper pool reset if the task is not urgent" in decision["note"]
    assert "note: claude_pool_low" in wr.render(decision)
    assert wr.decide("medium", "low", pools(claude=pool("claude", 0.40), **LOW_CODEX_GEMINI))["claude_pool_low"] is False
    # the note rides on the chosen Claude worker only
    assert wr.decide("medium", "low", pools(claude=pool("claude", 0.35), gemini=pool("gemini", 0.9)))["claude_pool_low"] is False


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
    fake.write_text("#!/bin/sh\n[ \"$1 $2\" = '--json limits' ] || exit 9\ncat <<'EOF'\n"
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

    monkeypatch.setattr(wr.relay_argon, "sandbox", lambda: True)
    reading = wr.read_gemini()
    assert (tmp_path / ".gemini").is_symlink()
    assert (tmp_path / ".gemini").resolve() == target.resolve()
    assert reading.free == pytest.approx(0.98) and reading.windows == {"5h": 1.0, "weekly": 0.98}

    (tmp_path / ".gemini").unlink()
    (tmp_path / ".gemini").mkdir()
    assert wr.read_gemini().reason == "login_path_conflict"

    monkeypatch.setattr(wr.relay_argon, "sandbox", lambda: False)
    assert wr.read_gemini().free == pytest.approx(0.98)


def test_copilot_parse_entitlement_and_overage_boundaries() -> None:
    def reading(**premium: Any) -> dict[str, Any]:
        return {"cli_enabled": True, "quota_snapshots": {"premium_interactions": premium}}

    assert wr.parse_copilot(None).reason == "no_reading"
    assert wr.parse_copilot({"cli_enabled": False}).reason == "cli_disabled"
    assert wr.parse_copilot({"cli_enabled": True}).reason == "bucket_missing"
    assert wr.parse_copilot(reading(has_quota=False, entitlement=0)).reason == "no_entitlement"
    assert wr.parse_copilot(reading(has_quota=False, entitlement=100)).free == 0
    assert wr.parse_copilot(reading(unlimited=True)).free == 1
    assert wr.parse_copilot(reading(has_quota=True, percent_remaining=75)).windows == {"monthly": 0.75}
    assert wr.parse_copilot(reading(has_quota=True, percent_remaining=0, overage_permitted=True)).free == 0
    assert wr.parse_copilot(reading(has_quota=True, percent_remaining=50, remaining=0,
                                    overage_permitted=True)).free == 0
    for bad in (True, -1, 101, "50", float("nan")):
        assert wr.parse_copilot(reading(has_quota=True, percent_remaining=bad)).reason == "invalid_reading"


def test_copilot_routing_priority_and_fourth_column() -> None:
    available = pools(copilot=wr.Pool("copilot", {"monthly": 0.8}, 0.8))
    assert wr.decide("low", "low", available)["worker"] == "flash"
    available["gemini"] = wr.Pool("gemini", {"5h": 0.04, "weekly": 0.04}, 0.04)
    available["codex"] = wr.Pool("codex", {"5h": 0.6, "weekly": 0.6}, 0.6)
    assert wr.decide("low", "low", available)["worker"] == "copilot-auto"  # most free room among unknown resets
    assert wr.decide("medium", "low", available)["worker"] == "terra"
    assert wr.bare_command(next(m for m in wr.MODELS if m.key == "copilot-auto")) == (
        'python3 scripts/copilot_worker.py run --task-file "$TASK_FILE" '
        '--worktree "$WORKTREE" --model copilot-auto')
    assert "copilot 80% free" in wr.summary_line(available)
    available["copilot"] = wr.Pool("copilot", {"monthly": 0.04}, 0.04)
    assert wr.decide("medium", "low", available)["worker"] == "terra"


def test_read_copilot_uses_dedicated_token_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "gh"
    script.write_text("#!/usr/bin/env python3\nimport json, os\n"
                      "assert os.environ.get('GH_TOKEN') == 'dedicated'\n"
                      "print(json.dumps({'cli_enabled': True, 'quota_snapshots': {'premium_interactions': "
                      "{'has_quota': True, 'percent_remaining': 80}}}))\n")
    script.chmod(0o755)
    monkeypatch.setenv("JARVISOS_GH_BIN", str(script))
    monkeypatch.setenv("JARVISOS_COPILOT_GH_TOKEN", "dedicated")
    monkeypatch.setenv("GH_TOKEN", "coordinator")
    copilot = tmp_path / "copilot"
    copilot.write_text("#!/bin/sh\nexit 0\n")
    copilot.chmod(0o755)
    monkeypatch.setenv("JARVISOS_COPILOT_BIN", str(copilot))
    assert wr.read_copilot().free == 0.8
    monkeypatch.delenv("JARVISOS_COPILOT_BIN")
    monkeypatch.setattr(wr, "_host_which", lambda name: None)
    assert wr.read_copilot().reason == "copilot_missing"
    at = datetime.now(UTC)
    live = {"copilot": wr.unreadable("copilot", "copilot_missing")}
    assert wr.apply_snapshot(live, {"copilot": (at, {"monthly": 1.0})}, at)["copilot"].free is None


def test_read_copilot_requires_worker_auth_even_with_coordinator_gh(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "gh"
    script.write_text("#!/usr/bin/env python3\nimport json\n"
                      "print(json.dumps({'cli_enabled': True, 'quota_snapshots': {'premium_interactions': "
                      "{'has_quota': True, 'percent_remaining': 80}}}))\n")
    script.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JARVISOS_GH_BIN", str(script))
    monkeypatch.setenv("GH_TOKEN", "coordinator")
    monkeypatch.delenv("JARVISOS_COPILOT_GH_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    assert wr.read_copilot().reason == "auth_missing"


def test_read_claude_cached_and_stale_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_AGENT_RELAY_BIN", "relay")
    now = datetime.now(UTC)
    calls: list[list[str]] = []
    documents = [{"agents": [entry(wr._iso(now))]}]
    def run(args: list[str], **_kwargs: Any) -> Any:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(documents[min(len(calls) - 1, len(documents) - 1)]), "")
    monkeypatch.setattr(wr.subprocess, "run", run)
    assert wr.read_claude().free is not None and len(calls) == 1
    calls.clear()
    documents[:] = [{"agents": [entry(wr._iso(now - timedelta(hours=1)))]}, {"agents": [entry(wr._iso(now))]}]
    assert wr.read_claude().free is not None and calls[1][-1] == "--refresh"
    calls.clear()
    documents[1] = {"agents": []}
    assert wr.read_claude().free is None and len(calls) == 2


# ---------------------------------------------------------------------------
# Spec 255: every agy pool, use it or lose it, review by another family

AGY_DOC = {"status": "SUCCESS", "command": {"data": {"groups": [{"buckets": [
    {"id": "gemini-5h", "remaining_fraction": 0.9}, {"id": "gemini-weekly", "remaining_fraction": 0.8},
    {"id": "gemini-argon-wtu-weekly", "remaining_fraction": 0.0, "reset_time": "2030-01-01T00:00:00Z"},
    {"id": "3p-5h", "remaining_fraction": 0.7}, {"id": "3p-weekly", "remaining_fraction": 0.5}]}]}}}


def test_one_agy_reading_feeds_gemini_argon_and_3p_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(wr, "read_agy_document", lambda: calls.append(1) or AGY_DOC)
    result = wr.read_pools({name: wr.READERS[name] for name in ("gemini", "argon", "agy3p")})
    assert len(calls) == 1
    assert result["gemini"].free == pytest.approx(0.8)
    assert result["argon"].windows["argon-weekly"] == 0.0
    assert result["argon"].resets["argon-weekly"] == datetime(2030, 1, 1, tzinfo=UTC)
    assert result["agy3p"].free == pytest.approx(0.5)
    monkeypatch.setattr(wr, "read_agy_document", lambda: "auth_required")
    assert wr.read_pools({"argon": wr.READERS["argon"]})["argon"].reason == "auth_required"


def test_claude_codex_and_argon_out_still_route_every_tier() -> None:
    out = wr.Pool("argon", {"5h": 0.9, "weekly": 0.8, "argon-weekly": 0.0}, 0.0)
    state = pools(claude=pool("claude", 0.0), codex=pool("codex", 0.0), argon=out)
    assert wr.decide("low", "low", state)["worker"] == "flash"
    assert wr.decide("medium", "medium", state)["worker"] == "flash-medium"
    assert wr.decide("medium", "high", state)["worker"] == "flash-high"
    only_3p = {**state, "gemini": pool("gemini", 0.0), "agy3p": pool("agy3p", 0.6)}
    assert wr.decide("medium", "high", only_3p)["worker"] in ("agy-sonnet", "agy-opus")
    only_argon = {**state, "gemini": pool("gemini", 0.6), "argon": pool("argon", 0.6)}
    high = wr.decide("high", "high", only_argon)
    assert high["worker"] == "flash-high" and [a["worker"] for a in high["alternatives"]][:2] == ["hydrogen-high", "argon-high"]
    assert "--model argon-high" in wr.bare_command(next(m for m in wr.MODELS if m.key == "argon-high"))


def test_window_resetting_within_a_day_may_be_spent_down() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    soon = wr.Pool("codex", {"5h": 0.9, "weekly": 0.02}, 0.02, resets={"weekly": now + timedelta(hours=20)})
    far = wr.Pool("codex", {"5h": 0.9, "weekly": 0.04}, 0.04, resets={"weekly": now + timedelta(days=5)})
    empty = wr.Pool("codex", {"5h": 0.9, "weekly": 0.0}, 0.0, resets={"weekly": now + timedelta(hours=2)})
    assert not wr.below_reserve(soon, now) and wr.below_reserve(far, now) and wr.below_reserve(empty, now)
    assert wr.expiry_score(soon, now) > wr.expiry_score(far, now)


def test_review_of_prefers_another_family_but_never_blocks() -> None:
    state = pools(agy3p=pool("agy3p", 0.6))
    assert wr.decide("medium", "high", state)["worker"] == "flash-high"
    review = wr.decide("medium", "high", state, review_of="gemini")
    assert review["status"] == "ok" and wr.family(next(m for m in wr.MODELS if m.key == review["worker"])) != "gemini"
    gemini_only = pools(codex=pool("codex", 0.0), claude=pool("claude", 0.0), argon=pool("argon", 0.6))
    fallback = wr.decide("medium", "high", gemini_only, review_of="gemini")
    assert fallback["status"] == "ok" and fallback["worker"] in ("flash-high", "argon-high")
    assert wr.build_parser().parse_args(["pick", "--difficulty", "low", "--risk", "low", "--review-of", "claude"]).review_of == "claude"


# ---------------------------------------------------------------------------
# Spec 256: worker outcomes, escalation, measured adequacy and report


def test_spec_256_criterion_1_record_validation_and_append(tmp_path: Path) -> None:
    rf = tmp_path / "worker-outcomes.jsonl"
    entry = wr.record(task="255-review", kind="review", worker="flash-medium", tier=2,
                      ci_first_pass="pass", findings="0,1,0,0", rework_rounds=1,
                      escalated=False, salvage="salvaged", failure="none", coordinator_tokens=120,
                      record_file=rf)
    assert rf.is_file()
    lines = [json.loads(line) for line in rf.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 1 and lines[0] == entry
    assert lines[0]["task"] == "255-review"
    assert lines[0]["kind"] == "review"
    assert lines[0]["worker"] == "flash-medium"
    assert lines[0]["model"] == "gemini-3.8-flash-medium"
    assert lines[0]["tier"] == 2
    assert lines[0]["ci_first_pass"] == "pass"
    assert lines[0]["findings"] == {"P0": 0, "P1": 1, "P2": 0, "P3": 0}
    assert lines[0]["rework_rounds"] == 1
    assert lines[0]["escalated"] is False
    assert lines[0]["salvage"] == "salvaged"
    assert lines[0]["failure"] == "none"
    assert lines[0]["coordinator_tokens"] == 120

    assert wr.main(["record", "--task", "256-fix", "--kind", "fix", "--worker", "terra",
                    "--record-file", str(rf)]) == 0
    lines = [json.loads(line) for line in rf.read_text(encoding="utf-8").strip().splitlines()]
    assert len(lines) == 2
    assert lines[1]["worker"] == "terra"
    assert lines[1]["model"] == "gpt-6.1-terra"

    with pytest.raises(ValueError):
        wr.record(task="t1", kind="magic", worker="flash", record_file=rf)
    assert wr.main(["record", "--task", "t1", "--kind", "magic", "--worker", "flash", "--record-file", str(rf)]) != 0

    with pytest.raises(ValueError):
        wr.record(task="t1", kind="docs", worker="flash", failure="quantum", record_file=rf)
    assert wr.main(["record", "--task", "t1", "--kind", "docs", "--worker", "flash", "--failure", "quantum", "--record-file", str(rf)]) != 0

    with pytest.raises(ValueError):
        wr.record(task="t1", kind="docs", worker="flash", salvage="maybe", record_file=rf)
    assert wr.main(["record", "--task", "t1", "--kind", "docs", "--worker", "flash", "--salvage", "maybe", "--record-file", str(rf)]) != 0

    with pytest.raises(ValueError):
        wr.record(task="t1", kind="docs", worker="flash", salvage="restarted", reason=None, record_file=rf)
    with pytest.raises(ValueError):
        wr.record(task="t1", kind="docs", worker="flash", salvage="restarted", reason="   ", record_file=rf)
    assert wr.main(["record", "--task", "t1", "--kind", "docs", "--worker", "flash", "--salvage", "restarted", "--record-file", str(rf)]) != 0

    res = wr.record(task="t1", kind="docs", worker="flash", salvage="restarted", reason="wrong approach", record_file=rf)
    assert res["salvage"] == "restarted" and res["reason"] == "wrong approach"

    with pytest.raises(ValueError):
        wr.record(task="t1", kind="docs", worker="unknown-bot", record_file=rf)
    assert wr.main(["record", "--task", "t1", "--kind", "docs", "--worker", "unknown-bot", "--record-file", str(rf)]) != 0


def test_spec_256_criterion_2_model_failure_escalates_away_from_gemini(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rf = tmp_path / "worker-outcomes.jsonl"
    wr.record(task="task-A", kind="feature", worker="flash-high", failure="model", record_file=rf)

    outcomes = wr.load_records(rf)
    decision = wr.decide("high", "high", pools(), task="task-A", outcomes=outcomes)
    assert decision["status"] == "ok"
    assert wr.family(next(m for m in wr.MODELS if m.key == decision["worker"])) != "gemini"
    for entry in [decision, *decision["alternatives"]]:
        assert wr.family(next(m for m in wr.MODELS if m.key == entry["worker"])) != "gemini"
    assert next(s for s in decision["skipped"] if s["worker"] == "flash-high")["skipped"] == "escalated_from_family"

    gemini_only = pools(codex=pool("codex", 0.0), claude=pool("claude", 0.0), copilot=pool("copilot", 0.0))
    wait_decision = wr.decide("high", "high", gemini_only, task="task-A", outcomes=outcomes)
    assert wait_decision["status"] == "no_worker"
    assert wait_decision["reason"] == "wait_for_escalation_pool"

    monkeypatch.setattr(wr, "read_pools", lambda: gemini_only)
    nothing = ["--quota-file", str(tmp_path / "none.json"), "--record-file", str(rf)]
    assert wr.main(["pick", "--difficulty", "high", "--risk", "high", "--task", "task-A", "--json", *nothing]) == wr.NO_WORKER_EXIT
    cli_data = json.loads(capsys.readouterr().out)
    assert cli_data["status"] == "no_worker" and cli_data["reason"] == "wait_for_escalation_pool"

    assert wr.main(["pick", "--difficulty", "high", "--risk", "high", "--task", "task-A", *nothing]) == wr.NO_WORKER_EXIT
    out_text = capsys.readouterr().out
    assert "no_worker (wait_for_escalation_pool)" in out_text


def test_spec_256_criterion_3_harness_failure_leaves_pick_unchanged(tmp_path: Path) -> None:
    rf = tmp_path / "worker-outcomes.jsonl"
    wr.record(task="task-B", kind="feature", worker="flash-high", failure="harness", record_file=rf)

    outcomes = wr.load_records(rf)
    baseline = wr.decide("high", "high", pools())
    with_task = wr.decide("high", "high", pools(), task="task-B", outcomes=outcomes)
    assert baseline["worker"] == with_task["worker"]
    assert [a["worker"] for a in baseline["alternatives"]] == [a["worker"] for a in with_task["alternatives"]]
    assert [s["worker"] for s in baseline["skipped"]] == [s["worker"] for s in with_task["skipped"]]


def test_spec_256_criterion_4_measured_inadequate_at_five_records(tmp_path: Path) -> None:
    rf = tmp_path / "worker-outcomes.jsonl"
    for i in range(3):
        wr.record(task=f"terra-{i}", kind="feature", worker="terra", ci_first_pass="pass",
                  findings="0,0,0,0", rework_rounds=0, record_file=rf)

    for i in range(2):
        wr.record(task=f"flash-pass-{i}", kind="feature", worker="flash-medium", ci_first_pass="pass",
                  findings="0,1,0,0", rework_rounds=2, record_file=rf)
    for i in range(2):
        wr.record(task=f"flash-fail-{i}", kind="feature", worker="flash-medium", ci_first_pass="fail",
                  findings="0,1,0,0", rework_rounds=2, record_file=rf)

    outcomes_4 = wr.load_records(rf)
    decision_4 = wr.decide("medium", "low", pools(), kind="feature", outcomes=outcomes_4)
    assert decision_4["worker"] == "flash-medium"
    assert not any(s["worker"] == "flash-medium" for s in decision_4["skipped"])

    wr.record(task="flash-fail-3", kind="feature", worker="flash-medium", ci_first_pass="fail",
              findings="0,1,0,0", rework_rounds=2, record_file=rf)

    outcomes_5 = wr.load_records(rf)
    decision_5 = wr.decide("medium", "low", pools(), kind="feature", outcomes=outcomes_5)
    assert decision_5["worker"] == "hydrogen-medium"
    assert "terra" in [a["worker"] for a in decision_5["alternatives"]]
    skipped_fm = next(s for s in decision_5["skipped"] if s["worker"] == "flash-medium")
    assert skipped_fm["skipped"] == "measured_inadequate"


def test_spec_256_criterion_5_report_in_italian(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    rf = tmp_path / "worker-outcomes.jsonl"
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    wr.record(task="t1", kind="feature", worker="flash-medium", ci_first_pass="pass",
              findings="0,0,0,0", rework_rounds=0, escalated=False, coordinator_tokens=150,
              record_file=rf, now=now - timedelta(days=1))
    wr.record(task="t2", kind="fix", worker="flash-high", ci_first_pass="fail",
              findings="0,1,0,0", rework_rounds=2, escalated=True, coordinator_tokens=300,
              record_file=rf, now=now - timedelta(days=2))
    wr.record(task="t3", kind="tests", worker="terra", ci_first_pass="pass",
              findings="0,0,0,0", rework_rounds=1, escalated=False,
              record_file=rf, now=now - timedelta(days=3))

    text = wr.report(days=7, record_file=rf, now=now)
    assert "gemini-3.8-flash-medium" in text
    assert "gemini-3.8-flash-high" in text
    assert "gpt-6.1-terra" in text
    assert "compiti" in text
    assert "prima prova" in text
    assert "rilievi" in text
    assert "rilavorazioni" in text
    assert "escalation" in text
    assert "Risparmio netto: 1 compiti Claude e GPT evitati (1 compiti Gemini non escalati), meno 150 token coordinatore registrati." in text

    assert wr.main(["report", "--days", "7", "--record-file", str(rf)]) == 0
    cli_out = capsys.readouterr().out
    assert "Risparmio netto:" in cli_out
    assert "token coordinatore" in cli_out


def test_spec_256_review_records_without_a_first_pass_never_judge_a_model(tmp_path: Path) -> None:
    rf = tmp_path / "worker-outcomes.jsonl"
    for i in range(6):
        wr.record(task=f"r{i}", kind="review", worker="flash-high", rework_rounds=3, record_file=rf)
        wr.record(task=f"t{i}", kind="review", worker="terra", rework_rounds=0, record_file=rf)
    decision = wr.decide("medium", "high", pools(), kind="review", outcomes=wr.load_records(rf))
    assert decision["worker"] == "flash-high"  # n/a first pass: no evidence for or against
    wr.record(task="broken", kind="review", worker="flash-high", record_file=rf)
    with rf.open("a", encoding="utf-8") as handle:
        handle.write('{"model": "x", "at": "2099-01-01T00:00:00Z", "rework_rounds": "lots"}\nnot json\n')
    assert "Risparmio netto" in wr.report(record_file=rf, now=datetime(2099, 1, 2, tzinfo=UTC))


def test_spec_256_report_has_no_first_pass_rate_without_judged_records(tmp_path: Path) -> None:
    rf = tmp_path / "worker-outcomes.jsonl"
    wr.record(task="r1", kind="review", worker="flash-high", rework_rounds=2, record_file=rf)
    text = wr.report(record_file=rf)
    assert "prima prova n/d" in text and "rilavorazioni 2," in text


# --- Spec 272 (WORKER-COST-1): measured spend per accepted task -----------------------


def test_record_with_evidence_and_spend_calculation(tmp_path: Path) -> None:
    dir_a = tmp_path / "run_a"
    dir_a.mkdir()
    (dir_a / "result.json").write_text(json.dumps({
        "usage": {"total_tokens": 500, "input_tokens": 400, "output_tokens": 100},
        "quota": [{"id": "gemini-weekly", "remaining_fraction": 0.80}],
        "quota_after": [{"id": "gemini-weekly", "remaining_fraction": 0.78}],
        "overlap": False,
    }))

    dir_b = tmp_path / "run_b"
    dir_b.mkdir()
    (dir_b / "result.json").write_text(json.dumps({
        "usage": {"total_tokens": 300, "input_tokens": 200, "output_tokens": 100},
        "quota": [{"id": "gemini-weekly", "remaining_fraction": 0.78}],
        "quota_after": [{"id": "gemini-weekly", "remaining_fraction": 0.75}],
        "overlap": False,
    }))

    rf = tmp_path / "worker-outcomes.jsonl"
    entry = wr.record(task="t1", kind="feature", worker="flash-medium", evidence=[dir_a, dir_b], record_file=rf)
    assert "spend" in entry
    assert entry["spend"] == {
        "runs": 2,
        "tokens": 800,
        "quota": pytest.approx(0.05),
        "quota_runs": 2,
        "overlap_runs": 0,
    }


def test_record_with_overlapped_run_adds_tokens_but_no_quota(tmp_path: Path) -> None:
    dir_a = tmp_path / "run_a"
    dir_a.mkdir()
    (dir_a / "result.json").write_text(json.dumps({
        "usage": {"total_tokens": 500},
        "quota": [{"id": "gemini-weekly", "remaining_fraction": 0.80}],
        "quota_after": [{"id": "gemini-weekly", "remaining_fraction": 0.78}],
        "overlap": False,
    }))

    dir_b = tmp_path / "run_b"
    dir_b.mkdir()
    (dir_b / "result.json").write_text(json.dumps({
        "usage": {"total_tokens": 300},
        "quota": [{"id": "gemini-weekly", "remaining_fraction": 0.78}],
        "quota_after": [{"id": "gemini-weekly", "remaining_fraction": 0.75}],
        "overlap": True,
    }))

    rf = tmp_path / "worker-outcomes.jsonl"
    entry = wr.record(task="t2", kind="feature", worker="flash-medium", evidence=[dir_a, dir_b], record_file=rf)
    assert entry["spend"] == {
        "runs": 2,
        "tokens": 800,
        "quota": pytest.approx(0.02),
        "quota_runs": 1,
        "overlap_runs": 1,
    }


def test_record_missing_or_unreadable_result_refused(tmp_path: Path) -> None:
    rf = tmp_path / "worker-outcomes.jsonl"
    missing = tmp_path / "missing_dir"

    with pytest.raises(ValueError, match="missing or unreadable"):
        wr.record(task="t3", kind="feature", worker="flash-medium", evidence=[missing], record_file=rf)
    assert not rf.exists()

    ret = wr.main(["record", "--task", "t3", "--kind", "feature", "--worker", "flash-medium",
                   "--evidence", str(missing), "--record-file", str(rf)])
    assert ret == 2
    assert not rf.exists()


def test_measured_ordering_hydrogen_medium_cheaper_than_flash_medium_ranks_first() -> None:
    records_flash_5 = [
        {"task": f"f{i}", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
         "failure": "none", "spend": {"runs": 1, "tokens": 1000, "quota": 0.03, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(5)
    ]
    records_hydrogen_5 = [
        {"task": f"h{i}", "kind": "feature", "worker": "hydrogen-medium", "model": "gemini-4-hydrogen-medium",
         "failure": "none", "spend": {"runs": 1, "tokens": 1000, "quota": 0.015, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(5)
    ]

    # With 5 records for both: hydrogen-medium is cheaper and measured -> ranked first
    ranked, _ = wr.rank(2, pools(), kind="feature", outcomes=records_flash_5 + records_hydrogen_5)
    assert ranked[0]["worker"] == "hydrogen-medium"
    assert ranked[0]["measured_cost"] == pytest.approx(0.015)
    assert ranked[0]["record_count"] == 5
    assert ranked[1]["worker"] == "flash-medium"
    assert ranked[1]["measured_cost"] == pytest.approx(0.03)
    assert ranked[1]["record_count"] == 5

    # With 4 records for hydrogen: unmeasured -> today's order (flash-medium first)
    ranked_4, _ = wr.rank(2, pools(), kind="feature", outcomes=records_flash_5 + records_hydrogen_5[:4])
    assert ranked_4[0]["worker"] == "flash-medium"
    assert ranked_4[1]["worker"] == "hydrogen-medium"

    # With 4 records for flash: unmeasured -> today's order (flash-medium first)
    ranked_flash_4, _ = wr.rank(2, pools(), kind="feature", outcomes=records_flash_5[:4] + records_hydrogen_5)
    assert ranked_flash_4[0]["worker"] == "flash-medium"
    assert ranked_flash_4[1]["worker"] == "hydrogen-medium"


def test_model_with_three_accepted_and_three_model_failures_has_infinite_median() -> None:
    records = [
        {"task": f"h{i}", "kind": "feature", "worker": "hydrogen-medium", "model": "gemini-4-hydrogen-medium",
         "failure": "none", "spend": {"runs": 1, "tokens": 500, "quota": 0.01, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(3)
    ] + [
        {"task": f"hf{i}", "kind": "feature", "worker": "hydrogen-medium", "model": "gemini-4-hydrogen-medium",
         "failure": "model", "spend": {"runs": 1, "tokens": 500, "quota": 0.01, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(3)
    ] + [
        {"task": f"f{i}", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
         "failure": "none", "spend": {"runs": 1, "tokens": 1000, "quota": 0.05, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(5)
    ]

    h_cost, h_count = wr.candidate_measured_cost(
        next(m for m in wr.MODELS if m.key == "hydrogen-medium"), "feature", records
    )
    assert h_count == 6
    assert h_cost == math.inf

    ranked, _ = wr.rank(2, pools(), kind="feature", outcomes=records)
    # Measured flash-medium with finite median ranks BEFORE infinite-median hydrogen-medium
    workers = [r["worker"] for r in ranked]
    assert workers.index("flash-medium") < workers.index("hydrogen-medium")


def test_candidates_in_different_pools_never_reordered_by_measured_cost() -> None:
    # terra is codex pool, flash-medium is gemini pool
    records_terra = [
        {"task": f"t{i}", "kind": "feature", "worker": "terra", "model": "gpt-6.1-terra",
         "failure": "none", "spend": {"runs": 1, "tokens": 1000, "quota": 0.001, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(5)
    ]
    records_flash = [
        {"task": f"f{i}", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
         "failure": "none", "spend": {"runs": 1, "tokens": 1000, "quota": 0.05, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(5)
    ]
    ranked, _ = wr.rank(2, pools(), kind="feature", outcomes=records_terra + records_flash)
    workers = [r["worker"] for r in ranked]
    # gemini pool candidate still comes before codex pool candidate despite higher measured cost
    assert workers.index("flash-medium") < workers.index("terra")


def test_measured_candidate_never_jumps_review_of_split() -> None:
    # review_of="gemini": gemini candidates have same=1, codex terra has same=0
    records_flash = [
        {"task": f"f{i}", "kind": "review", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
         "failure": "none", "spend": {"runs": 1, "tokens": 1000, "quota": 0.001, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(5)
    ]
    ranked, _ = wr.rank(2, pools(), review_of="gemini", kind="review", outcomes=records_flash)
    # terra (different family than gemini) comes before flash-medium despite flash being measured cheaper
    workers = [r["worker"] for r in ranked]
    assert workers.index("terra") < workers.index("flash-medium")


def test_model_cost_deleted() -> None:
    from dataclasses import fields
    model_field_names = [f.name for f in fields(wr.Model)]
    assert "cost" not in model_field_names

    ranked, _ = wr.rank(1, pools())
    for entry in ranked:
        assert "cost" not in entry
        assert "measured_cost" in entry
        assert "record_count" in entry

    decision = wr.decide("low", "low", pools())
    assert "measured_cost" in decision
    assert "record_count" in decision


def test_render_shows_measured_cost_and_record_count() -> None:
    records = [
        {"task": f"h{i}", "kind": "feature", "worker": "hydrogen-medium", "model": "gemini-4-hydrogen-medium",
         "failure": "none", "spend": {"runs": 1, "tokens": 1000, "quota": 0.015, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(5)
    ] + [
        {"task": f"f{i}", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
         "failure": "none", "spend": {"runs": 1, "tokens": 1000, "quota": 0.03, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(5)
    ]
    decision = wr.decide("medium", "low", pools(), kind="feature", outcomes=records)
    text = wr.render(decision)
    assert "worker: hydrogen-medium (gemini-4-hydrogen-medium, pool gemini, cost 0.015, 5 records)" in text
    assert "flash-medium (gemini 99% free, cost 0.03, 5 records)" in text
    assert "terra (codex 95% free, cost unmeasured, 0 records)" in text


def test_duplicate_evidence_paths_refused(tmp_path: Path) -> None:
    dir_a = tmp_path / "run_a"
    dir_a.mkdir()
    (dir_a / "result.json").write_text(json.dumps({
        "usage": {"total_tokens": 500},
        "quota": [{"id": "gemini-weekly", "remaining_fraction": 0.80}],
        "quota_after": [{"id": "gemini-weekly", "remaining_fraction": 0.78}],
        "overlap": False,
    }))
    rf = tmp_path / "worker-outcomes.jsonl"

    # Passing same dir twice
    with pytest.raises(ValueError, match="duplicate evidence path"):
        wr.record(task="t_dup", kind="feature", worker="flash-medium", evidence=[dir_a, dir_a], record_file=rf)
    assert not rf.exists()

    # Passing dir and its result.json
    with pytest.raises(ValueError, match="duplicate evidence path"):
        wr.record(task="t_dup2", kind="feature", worker="flash-medium", evidence=[dir_a, dir_a / "result.json"], record_file=rf)
    assert not rf.exists()

    # CLI rejection with code 2
    ret = wr.main(["record", "--task", "t_dup3", "--kind", "feature", "--worker", "flash-medium",
                   "--evidence", str(dir_a), "--evidence", str(dir_a), "--record-file", str(rf)])
    assert ret == 2
    assert not rf.exists()


def test_harness_failure_excluded_from_candidate_measured_cost() -> None:
    model = next(m for m in wr.MODELS if m.key == "flash-medium")
    records = [
        {"task": f"f{i}", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
         "failure": "none", "spend": {"runs": 1, "tokens": 1000, "quota": 0.02, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(4)
    ] + [
        {"task": "harn_fail", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
         "failure": "harness", "spend": {"runs": 1, "tokens": 99999, "quota": 0.50, "quota_runs": 1, "overlap_runs": 0}}
    ]
    # 4 normal + 1 harness failure: harness failure is excluded, so count is 4 (< 5 -> unmeasured)
    cost, count = wr.candidate_measured_cost(model, "feature", records)
    assert count == 4
    assert cost is None

    # Add 5th normal record: measured cost computed from 5 normal records, harness record excluded
    records.append(
        {"task": "f4", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
         "failure": "none", "spend": {"runs": 1, "tokens": 1000, "quota": 0.02, "quota_runs": 1, "overlap_runs": 0}}
    )
    cost, count = wr.candidate_measured_cost(model, "feature", records)
    assert count == 5
    assert cost == pytest.approx(0.02)


def test_cli_record_multiple_evidence(tmp_path: Path) -> None:
    dir_a = tmp_path / "run_a"
    dir_a.mkdir()
    (dir_a / "result.json").write_text(json.dumps({
        "usage": {"total_tokens": 400},
        "quota": [{"id": "gemini-weekly", "remaining_fraction": 0.85}],
        "quota_after": [{"id": "gemini-weekly", "remaining_fraction": 0.82}],
        "overlap": False,
    }))
    dir_b = tmp_path / "run_b"
    dir_b.mkdir()
    (dir_b / "result.json").write_text(json.dumps({
        "usage": {"total_tokens": 600},
        "quota": [{"id": "gemini-weekly", "remaining_fraction": 0.82}],
        "quota_after": [{"id": "gemini-weekly", "remaining_fraction": 0.80}],
        "overlap": False,
    }))
    rf = tmp_path / "worker-outcomes.jsonl"
    ret = wr.main(["record", "--task", "t-multi", "--kind", "feature", "--worker", "flash-medium",
                   "--evidence", str(dir_a), "--evidence", str(dir_b), "--record-file", str(rf)])
    assert ret == 0
    records = wr.load_records(rf)
    assert len(records) == 1
    assert records[0]["spend"] == {
        "runs": 2,
        "tokens": 1000,
        "quota": pytest.approx(0.05),
        "quota_runs": 2,
        "overlap_runs": 0,
    }


def test_escalated_records_yield_infinite_cost() -> None:
    model = next(m for m in wr.MODELS if m.key == "flash-medium")
    records = [
        {"task": f"f{i}", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
         "failure": "none", "escalated": False,
         "spend": {"runs": 1, "tokens": 1000, "quota": 0.02, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(2)
    ] + [
        {"task": f"esc{i}", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
         "failure": "none", "escalated": True,
         "spend": {"runs": 1, "tokens": 1000, "quota": 0.02, "quota_runs": 1, "overlap_runs": 0}}
        for i in range(3)
    ]
    # 2 cheap + 3 escalated -> costs = [0.02, 0.02, inf, inf, inf] -> median is inf
    cost, count = wr.candidate_measured_cost(model, "feature", records)
    assert count == 5
    assert cost == math.inf


def test_quota_reset_boundary_does_not_increment_quota_runs(tmp_path: Path) -> None:
    # Quota reset: remaining_fraction increases or delta < 0 (fa > fb)
    dir_reset = tmp_path / "run_reset"
    dir_reset.mkdir()
    (dir_reset / "result.json").write_text(json.dumps({
        "usage": {"total_tokens": 1000},
        "quota": [{"id": "gemini-weekly", "remaining_fraction": 0.05}],
        "quota_after": [{"id": "gemini-weekly", "remaining_fraction": 1.0}],
        "overlap": False,
    }))
    rf = tmp_path / "worker-outcomes.jsonl"
    entry = wr.record(task="t_reset", kind="feature", worker="flash-medium",
                      evidence=[dir_reset], record_file=rf)
    assert entry["spend"]["runs"] == 1
    assert entry["spend"]["quota"] == 0.0
    assert entry["spend"]["quota_runs"] == 0  # NOT 1!
    assert wr._is_clean_spend(entry["spend"]) is False

    # In candidate_measured_cost without any clean run to provide a ratio, this record has no cost
    model = next(m for m in wr.MODELS if m.key == "flash-medium")
    cost, count = wr.candidate_measured_cost(model, "feature", [entry])
    assert count == 0
    assert cost is None


def test_non_clean_run_with_zero_tokens_not_costed_zero() -> None:
    model = next(m for m in wr.MODELS if m.key == "flash-medium")
    clean_record = {
        "task": "c1", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
        "failure": "none",
        "spend": {"runs": 1, "tokens": 1000, "quota": 0.05, "quota_runs": 1, "overlap_runs": 0},
    }
    # Non-clean run with 0 tokens (e.g. usage missing/null)
    non_clean_zero_tokens = {
        "task": "nc1", "kind": "feature", "worker": "flash-medium", "model": "gemini-3.8-flash-medium",
        "failure": "none",
        "spend": {"runs": 1, "tokens": 0, "quota": 0.0, "quota_runs": 0, "overlap_runs": 1},
    }
    cost, count = wr.candidate_measured_cost(model, "feature", [clean_record, non_clean_zero_tokens])
    # The zero-token non-clean record must NOT append 0.0 to costs; only the clean record is costed
    assert count == 1
    assert cost is None  # count < 5
