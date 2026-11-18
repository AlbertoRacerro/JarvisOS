# 306 — COORDINATOR-TOKENS-1: Coordinator token usage recorded per settled turn and reported per pool next to worker outcomes

Status: ready
Depends on: none

## Goal

Every settled supervised coordinator turn records the tokens it consumed in the mission's `attempts.jsonl` `settle` event (`usage`), and `worker_routing.py report` reads those settled mission turns alongside the worker outcome records to report how many coordinator tokens were consumed per subscription pool (and the share of recorded tokens between coordination and workers where worker tokens exist), as well as the total coordinator tokens in the net-savings line.

## Why

Point 1 of maintainer directive `L20261118164933-7605` ("Measure first. The worker outcome report shows '0 token coordinatore registrati': the coordinator's own consumption is not recorded. Record coordinator tokens per mission next to worker outcomes, so the weekly report can say what share of each pool goes to coordination versus workers."). Today `worker_routing.py::report` only sums the optional `--coordinator-tokens` field on non-escalated Gemini worker outcome rows, which coordinators never pass, while the actual coordinator turns run under `relay_continuity_supervisor.py` and write their token usage into each turn's `output.jsonl` without recording it in the mission attempt journal.

## Code facts checked at `f015fd0d`

- `scripts/relay_continuity_turns.py::classify_turn` and `scripts/relay_continuity_turns.py::classify_argon_turn` build a `scripts/relay_continuity_turns.py::TurnReport` from a turn directory's `output.jsonl` and `state.json`.
- `scripts/relay_continuity_supervisor.py::Supervisor._settle` classifies the finished turn and appends an `"event": "settle"` record to the mission's `attempts.jsonl` via `self.store.journal`.
- In real coordinator turn artifacts (`output.jsonl`):
  - Claude turns emit a top-level `"type": "result"` event with `modelUsage` (a dict per model with `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`) and `usage` (`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`), and per-call `"type": "assistant"` events with `message.usage` (read by `scripts/relay_continuity_probes.py::_extract_usage_tokens`).
  - Codex turns emit `"type": "turn.completed"` with `usage` (`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`).
  - Native Antigravity turns (`argon`, `hydrogen`, `agy_opus`) emit `"event": "result"` whose `result.usage` has `input_tokens`, `output_tokens`, `thinking_tokens`, `cache_read_tokens`, `total_tokens`.
- `scripts/worker_routing.py::report` formats the 7-day Italian worker outcome summary and currently computes `tokens` only from `r["coordinator_tokens"]` on non-escalated Gemini records in `worker-outcomes.jsonl`.

## Scope

In scope:
1. **Per-turn coordinator token extraction (`relay_continuity_turns.py`).**
   - `TurnReport` gains an optional `usage: dict[str, int] | None = None` field (`{"input_tokens": ..., "output_tokens": ..., "cache_tokens": ..., "total_tokens": ...}`, all non-negative integers).
   - For Claude turns: extract from the terminal `"type": "result"` event if present (`modelUsage` summed across models: `input_tokens = inputTokens`, `output_tokens = outputTokens`, `cache_tokens = cacheReadInputTokens + cacheCreationInputTokens`; fallback to top-level `terminal["usage"]`: `input_tokens`, `output_tokens`, `cache_tokens = cache_read_input_tokens + cache_creation_input_tokens`); if the turn had no terminal `result` event, sum `message.usage` across top-level `"type": "assistant"` events. `total_tokens = input_tokens + output_tokens + cache_tokens`.
   - For Codex turns: extract from `"type": "turn.completed"` `usage`: `input_tokens` (non-cached input: `max(0, input_tokens - cached_input_tokens)` when `cached_input_tokens` is part of `input_tokens`, so `input_tokens + cache_tokens` equals Codex's reported total input plus `cache_write_input_tokens`), `output_tokens`, `cache_tokens = cached_input_tokens + cache_write_input_tokens`, `total_tokens = input_tokens + output_tokens + cache_tokens`.
   - For native turns (`argon`, `hydrogen`, `agy_opus`): extract from `"event": "result"` `result["usage"]`: `input_tokens`, `output_tokens`, `cache_tokens = cache_read_tokens`, and `total_tokens` (using ` result["usage"]["total_tokens"]` when valid, or `input_tokens + output_tokens`).
   - When no valid usage is present in `output.jsonl`, fall back to `report.context_used_tokens` if positive (`{"input_tokens": context_used_tokens, "output_tokens": 0, "cache_tokens": 0, "total_tokens": context_used_tokens}`), else `None`.
2. **Recording in mission `attempts.jsonl` (`relay_continuity_supervisor.py`).**
   - `Supervisor._settle` includes `"at": iso(self.host.now())` and `"usage": report.usage` (and `"coordinator_tokens": report.usage["total_tokens"]` when `report.usage` is a dict, else `None`) in the `"event": "settle"` journal record written to `self.store.journal`.
3. **Pool share and coordinator token reporting (`worker_routing.py`).**
   - Map each coordinator `agent` to its subscription pool (`claude` -> `claude`, `codex` -> `codex`, `argon` -> `argon`, `hydrogen` -> `gemini`, `agy_opus` -> `agy3p`).
   - `worker_routing.py::report` (and `cmd_report`, which gains `--control-dir` defaulting to `~/jarvis-control/work/continuity` or `RELAY_CONTINUITY_DIR`) reads `"event": "settle"` records from `missions/*/attempts.jsonl` (and root `attempts.jsonl`) within the `days` window (using the record's `"at"` timestamp, or the timestamp encoded in `"nonce"` `rc-YYYYMMDDTHHMMSSZ-...` for older records, and backfilling `"usage"` from the turn directory `<repo>/.agent-relay/sessions/<session>/turns/turn-001` when `"usage"` is not yet stored on the settle record and the session directory exists).
   - In `report()`, add a section in plain Italian listing per pool the coordinator turns and tokens versus worker tasks and recorded worker tokens (with the coordinator share in percent when the pool's total recorded tokens > 0), and use the total coordinator tokens (from mission turns plus any `coordinator_tokens` field on outcome records when no mission turns are present) in the `Risparmio netto:` summary line.

Out of scope (binding non-goals):
- Changing how coordinator failover or worker selection (`pick`, `rank`) works.
- Billing or USD cost estimation.
- Modifying historical `attempts.jsonl` files on disk.

## Files likely touched

- `scripts/relay_continuity_turns.py`
- `scripts/relay_continuity_supervisor.py`
- `scripts/worker_routing.py`
- `backend/tests/test_relay_continuity.py`
- `backend/tests/test_worker_routing.py`

## Design constraints

- Standard library only; no new dependencies.
- Reading mission journals or session turn directories in `worker_routing.py::report` must never fail or raise if the continuity directory or a session directory is missing or unreadable.
- Per `docs/PROCESS.md` section 5 ("Supervisor changes meet real inputs"), tests must include fixtures copied from real Claude, Codex and native `output.jsonl` files, and completion requires a second-session diff review plus real-environment verification.

## Acceptance criteria

1. `classify_turn` populates `report.usage` (`input_tokens`, `output_tokens`, `cache_tokens`, `total_tokens`) on real-shaped Claude, Codex and native (`argon`/`hydrogen`/`agy_opus`) turn outputs, and `None` when no usage is present.
2. `Supervisor._settle` writes `"at"`, `"usage"` and `"coordinator_tokens"` into the `"event": "settle"` record in `attempts.jsonl`.
3. `worker_routing.py::report` aggregates coordinator tokens per pool from settled mission turns in the time window (including fallback to nonce timestamp and session turn dir when `usage` is absent from older settle records) and outputs a per-pool coordination-vs-workers line in Italian plus the updated `Risparmio netto:` token total.
4. Existing `worker_routing` and `relay_continuity` tests continue to pass.

## Required tests

- Unit tests in `backend/tests/test_relay_continuity.py` with real-file fixtures for Claude, Codex and native turn usage extraction and `_settle` journal emission.
- Unit tests in `backend/tests/test_worker_routing.py` verifying per-pool coordinator vs worker token aggregation and Italian report formatting.

## Definition of done

Focused tests and lint pass, `check_spec_status.py` and `check_code_facts.py` pass, CI green on exact head, second-session review of the diff (high risk: relay/supervisor code), and real-environment proof on the live mission data.
