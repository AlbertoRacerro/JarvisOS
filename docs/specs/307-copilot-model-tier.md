# 307 — COPILOT-MODEL-TIER-1: Record actual model selected by Copilot CLI auto mode and token usage, and set Copilot routing tier from measured outcomes

Status: merged
Depends on: 306

## Goal

`scripts/copilot_worker.py` isolates each run's Copilot CLI session directory (`COPILOT_HOME` under the evidence directory) so `session-state/<session>/events.jsonl` is captured inside the evidence directory, extracts the actual model resolved by Copilot's `auto` router (`actual_model` from `session.auto_mode_resolved`, `session.shutdown` or `session.usage_record`) and its token/request usage (`usage`) into `result.json`, and `scripts/worker_routing.py` records `actual_model` from worker evidence in `worker-outcomes.jsonl`, displays the actual model(s) used under `copilot-auto` in `report`, and sets `copilot-auto`'s effective routing capability tier from its measured outcomes (allowing tier 2 trials so Copilot can be pushed on medium tasks and promoted or kept at tier 1 based on measured outcomes instead of being permanently fixed at tier 1).

## Why

Point 2 of maintainer directive `L20261118164933-7605` ("Copilot. Push Copilot harder: give it harder tasks too, test which model it picks (whether it switches to a stronger model on harder work), record which model it actually used (from the CLI output or usage data) and the outcome, and set its routing tier from those results instead of fixing it at 1."). Today `scripts/copilot_worker.py` always records `"model": "auto"` and no `"usage"` or `"actual_model"` in `result.json`, while the actual model chosen by Copilot Fusion (`"chosenModel"`, e.g. `"mai-code-2-flash"`, along with `candidateModels`, `selectionReason` and token counts) is written to `~/.copilot/session-state/<id>/events.jsonl`. Furthermore, `scripts/worker_routing.py::MODELS` hardcodes `Model("copilot-auto", "copilot", "auto", "copilot", 1)`, preventing `worker_routing.py pick` from ever selecting `copilot-auto` on tier 2 tasks or adjusting its tier from measured outcomes.

## Code facts checked at `b7a84d29`

- `scripts/copilot_worker.py::prepare_copilot_auth` resolves the Copilot token and symlinks `~/.copilot` to `~/.local/share/jarvisos-sandbox/state/copilot/copilot-home` when present.
- `scripts/copilot_worker.py::child_env` constructs the sanitized child environment for the `copilot` subprocess, and `scripts/copilot_worker.py::_run` executes `scripts/copilot_worker.py::run_argv` and writes `result.json` via `scripts/copilot_worker.py::execute`.
- In real Copilot CLI v1.0.112 runs (`events.jsonl` under `<copilot-home>/session-state/<session-id>/events.jsonl`), Copilot emits JSONL events including:
  - `"type": "session.auto_mode_resolved"` with `data.chosenModel` (str), `data.candidateModels` (list of str), and `data.selectionReason` (str);
  - `"type": "session.usage_record"` with `data.usage.model`, `data.usage.inputTokens`, `data.usage.outputTokens`, `data.usage.cacheReadTokens`, `data.usage.cacheWriteTokens`;
  - `"type": "session.shutdown"` with `data.currentModel`, `data.totalPremiumRequests`, `data.tokenDetails` (`input.tokenCount`, `output.tokenCount`, `cache_read.tokenCount`, `cache_write.tokenCount`), and `data.modelMetrics` (map of model ID to `requests` and `usage`).
- `scripts/worker_routing.py::MODELS` defines `Model("copilot-auto", "copilot", "auto", "copilot", 1)`.
- `scripts/worker_routing.py::record` reads each evidence `result.json` to extract `usage.total_tokens` into `spend.tokens` and writes the outcome record to `worker-outcomes.jsonl`.
- `scripts/worker_routing.py::rank` filters `MODELS` by `model.capability < required` and `check_measured_inadequate`, and `scripts/worker_routing.py::report` groups outcomes by `r.get("model") or r.get("worker")`.

## Scope

In scope:
1. **Per-run Copilot session capture and usage/model extraction (`scripts/copilot_worker.py`).**
   - In `child_env` / `_run`, set `COPILOT_HOME` in the child environment to an isolated directory `<evidence>/copilot-home` (copying `config.json` from `~/.copilot/config.json` or `~/.local/share/jarvisos-sandbox/state/copilot/copilot-home/config.json` if present, without copying historical `session-state` or `logs`), so every `copilot_worker.py` run writes its `session-state/<session-id>/events.jsonl` inside `<evidence>/copilot-home/`.
   - Add a helper `extract_copilot_session(copilot_home: Path) -> dict[str, Any]` that scans `<copilot_home>/session-state/*/events.jsonl` (or falls back to the newest session in `~/.copilot/session-state/*/events.jsonl` modified at or after `started_at` when the child binary ignores `COPILOT_HOME`) and extracts:
     - `actual_model: str | None`: from `session.auto_mode_resolved` (`data.chosenModel`), `session.shutdown` (`data.currentModel` or single key of `data.modelMetrics`), or the last `session.usage_record` (`data.usage.model` or `data.usage.responseModel`).
     - `auto_routing: dict[str, Any] | None`: when `session.auto_mode_resolved` is present, `{"chosen_model": ..., "candidate_models": ..., "selection_reason": ...}`.
     - `usage: dict[str, int] | None`: `{"input_tokens": ..., "output_tokens": ..., "cache_tokens": ..., "total_tokens": ..., "premium_requests": ...}` extracted from `session.shutdown` (`tokenDetails` or summed `modelMetrics[*].usage`, where `input_tokens` is non-cached input, `cache_tokens = cache_read + cache_write`, `total_tokens = input_tokens + output_tokens + cache_tokens`, and `premium_requests` from `totalPremiumRequests`), falling back to summing `session.usage_record` events when `session.shutdown` is absent.
   - Store `"actual_model"`, `"auto_routing"`, and `"usage"` on `result` in `result.json` (defaulting to `None` when no session events exist).
2. **Recording `actual_model` and outcome-driven Copilot tier in `scripts/worker_routing.py`.**
   - In `worker_routing.py::record`, when `evidence` is provided and any `result.json` contains a non-empty string `"actual_model"`, record `"actual_model"` on the outcome entry in `worker-outcomes.jsonl` (when multiple evidence files have distinct actual models, join them with `","` in order of appearance).
   - Add `copilot_effective_capability(outcomes: list[dict[str, Any]] | None) -> int` in `worker_routing.py`:
     - When `outcomes is None` (no outcome history provided), return the baseline capability `1`.
     - When `outcomes` is provided (such as `cmd_pick` loading `worker-outcomes.jsonl` or callers passing an `outcomes` list):
       - Inspect all non-harness `copilot-auto` records (`worker == "copilot-auto"` or `model == "auto"`, `failure != "harness"`).
       - If there are no non-harness `copilot-auto` records yet, return `1`.
       - Define a record as clean when `failure == "none"`, `not escalated`, `ci_first_pass != "fail"`, and `(ci_first_pass == "pass" or rework_rounds == 0)`.
       - If there are fewer than 2 non-harness `copilot-auto` records at `tier >= 2`: promote `copilot-auto` to capability `2` for tier-2 trials once `copilot-auto` has at least one clean record (at any tier) or an explicit tier-2 trial record; otherwise keep capability `1`.
       - Once there are at least 2 non-harness `copilot-auto` records at `tier >= 2`:
         - If at least 50% of tier-2+ records are clean and at least one tier-2+ record has `rework_rounds == 0`, return `2` (or `3` if there are at least 3 clean tier-2+ records with 0 escalations and `actual_model` resolved to a non-flash/non-mini/non-haiku model).
         - Otherwise (tier-2+ clean rate `< 50%` or no zero-rework tier-2+ record), demote `copilot-auto` back to capability `1`.
   - In `rank()`, use `copilot_effective_capability(outcomes)` (when `model.key == "copilot-auto"`) as the effective capability of `copilot-auto`.
   - In `report()`, when an outcome group has records with `"actual_model"`, include the observed actual model counts in parentheses on the worker line, e.g. `- auto (copilot-auto, modelli: mai-code-2-flash×2): ...`.

Out of scope (binding non-goals):
- Bypassing `copilot_worker.py`'s allowlist to pass explicit non-`auto` models on the command line (spec 238 keeps `--model auto`).
- Changing non-Copilot worker capability tiers.

## Files likely touched

- `scripts/copilot_worker.py`
- `scripts/worker_routing.py`
- `backend/tests/test_copilot_worker_238.py`
- `backend/tests/test_worker_routing.py`

## Design constraints

- Standard library only; no new dependencies.
- `copilot_worker.py` must never leak credentials or fail a worker run if `events.jsonl` is absent or malformed (`actual_model`, `auto_routing`, and `usage` simply stay `None`).
- All existing spec-238 security invariants (sandbox check, clean linked worktree, stripped environment, git metadata fingerprint check, quota preflight) remain intact.
- High risk (touches `scripts/copilot_worker.py` and `scripts/worker_routing.py`): requires second-session review and real-environment proof by running `copilot-auto` on a real multi-file task and recording its actual model and outcome.

## Acceptance criteria

1. `copilot_worker.py` sets `COPILOT_HOME` to `<evidence>/copilot-home`, parses `events.jsonl` after the run, and populates `actual_model`, `auto_routing`, and `usage` (`input_tokens`, `output_tokens`, `cache_tokens`, `total_tokens`, `premium_requests`) in `result.json`.
2. `worker_routing.py record` extracts `actual_model` from evidence `result.json` into the `worker-outcomes.jsonl` record, and `worker_routing.py report` displays the observed `actual_model` breakdown and includes `spend.tokens` from Copilot `usage.total_tokens` in the `Pool copilot:` line.
3. `copilot_effective_capability(outcomes)` sets `copilot-auto`'s effective routing tier from measured outcomes (promoting to tier 2 for trials once outcomes are provided and a clean or trial record exists, and demoting to tier 1 or promoting to tier 3 based on measured tier-2+ outcomes).
4. Existing `test_copilot_worker_238.py` and `test_worker_routing.py` tests pass alongside new unit tests covering session event extraction, `actual_model` recording, and dynamic Copilot tier calculation.

## Required tests

- Unit tests in `backend/tests/test_copilot_worker_238.py` verifying `COPILOT_HOME` isolation and `extract_copilot_session` parsing of real-shaped `session.auto_mode_resolved`, `session.usage_record`, and `session.shutdown` events into `result.json`.
- Unit tests in `backend/tests/test_worker_routing.py` verifying `actual_model` recording from evidence, `report()` formatting of `modelli: ...`, and `copilot_effective_capability` tier transitions (trial at tier 2, demotion to tier 1 on failures/escalations, retention at tier 2 on clean outcomes).

## Definition of done

Focused tests and lint pass, `check_spec_status.py` and `check_code_facts.py` pass, CI green on exact head, second-session review of the diff, and live proof running `copilot-auto` on a multi-file diagnosis task with its `actual_model` and `usage` recorded.
