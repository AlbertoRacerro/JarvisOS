# 382 — CONSULT-ZONE-C-HARDENING-1: Zone-C inference judge, mosaic guard and leak benchmark

Status: ready
Depends on: 381

## Goal

Harden every public search query and page fetch (zone C) — whether issued by Inkling or by the consultant's guarded
MCP server — so that neither a single query nor a sequence of queries over time can leak a specific project value or a
`local_only` item. Add an offline leak-benchmark runner where Sage acts as the attacker against single queries and whole
query logs so the maintainer can grade zone-C egress before relaxing strict mode.

## Why

Directive `L20261125114331-2bd1` (consult design v3) §5 and §9 step 3: "Zone-C hardening (§5) and the leak benchmark".
Implementation starts after spec 381 (step 2: `local_only` judge and Sage path) is merged and measured.

## Code facts checked at `3e457e14`

- `scripts/search_guard.py::evaluate_query` runs the deterministic spec-364 checks (pack terms, exact combinations,
  pack + vault `protected_dimensions`, `min_groups`, `sanitize_text`, `check_sensitivity_floor`) and
  `scripts/search_guard.py::log_query` appends a JSONL summary (hash prefix + reason code, never raw query text).
- `backend/app/modules/agents/hermes/paper_search_tool.py::execute_paper_search` checks `check_sensitivity_floor` and
  `sanitize_text` before calling OpenAlex, Semantic Scholar, Europe PMC, and Crossref.
- `backend/app/modules/agents/hermes/browser_page_tool.py::execute_browser_page_read` validates HTTPS/SSRF and checks
  pack terms and `check_sensitivity_floor` before launching headless Chromium.
- `backend/app/modules/ai/search_guard_summary.py::REASON_MESSAGES_IT` maps blocked reason codes to plain Italian
  messages for the operator summary.
- `backend/app/core/packs.py::SearchGuardConfig` defines `groups` (concept synonym groups) and `min_groups`.
- `backend/app/core/vault.py::local_only_items` and `backend/app/core/vault.py::protected_propositions` expose the
  vault's `(id, text)` items.
- `backend/app/modules/consult/config.py::read_consult_config` reads maintainer-only `consult.toml` in
  `JARVISOS_VAULT_PATH`.
- `backend/app/modules/ai/execution.py::run_ai_task` is the single product AI execution boundary that creates an
  `ai_jobs` row.

## Scope

1. **Unified zone-C guard (`backend/app/modules/consult/zone_c_guard.py`)** (§5):
   - Single fail-closed evaluator `evaluate_zone_c_request(kind, text, *, target_url=None, connection=None)` invoked by
     all zone-C egress doors:
     - `scripts/search_guard.py` (`/search` proxy);
     - `paper_search_tool.py::execute_paper_search`;
     - `browser_page_tool.py::execute_browser_page_read`;
     - `consult/mcp_server.py` (`web_search`, `paper_search`, `read_page`, introduced by spec 380 part b).
   - Evaluates three ordered stages (all fail-closed):
     1. **Deterministic search guard**: calls `scripts/search_guard.py::evaluate_query` (spec 364 + vault dimensions).
     2. **Local inference judge**: invokes the fresh-context local judge (reusing spec 381's `consult/judge.py` via
        `run_ai_task`, local route only, `ai_jobs` row) over the query/target text with `vault.local_only_items()`,
        `vault.protected_propositions()`, and pack sensitivity terms.
     3. **Mosaic guard**:
        - Computes which pack `SearchGuardConfig.groups` indices the current request touches.
        - Applies a **rolling 30-day per-group counter** over persisted allowed zone-C queries: if the union of distinct
          concept groups touched in the last 30 days (including the current request) reaches `guard_cfg.min_groups`,
          blocks with `mosaic_group_threshold`.
        - Retrieves up to the **last 20 allowed queries** touching any of the same pack concept groups and runs the
          fresh-context local judge over `(current_query, recent_queries, local_only_items)` via `run_ai_task`. Any
          flagged mosaic inference blocks with `zone_c_mosaic_judge`.
   - **Strict mode until benchmark passes**:
     - By default (`strict_mode=True`), any flag, uncertainty, parse error, or unavailable local judge blocks the
       request (`zone_c_inference_judge`, `zone_c_mosaic_judge`, or `zone_c_judge_error`).
     - Optional maintainer-only key `zone_c_benchmark_passed_on` (ISO date, rejecting `datetime`) in `consult.toml`
       records when the leak benchmark passed. Only when present and valid may strict mode relax to standard hit
       matching; missing/invalid date keeps strict mode active.
   - **Actionable, leak-free refusal**:
     - Blocked requests return a structured refusal (`reason_code`, plain guidance to rephrase generically) so Inkling
       or the consultant MCP can rephrase, without ever echoing `local_only` text, `protected_propositions`, or pack
       literals.
     - Add the new reason codes (`zone_c_inference_judge`, `zone_c_mosaic_judge`, `mosaic_group_threshold`,
       `zone_c_judge_error`) to `backend/app/modules/ai/search_guard_summary.py::REASON_MESSAGES_IT`.
2. **Zone-C query history store** (additive migration `0039_zone_c_query_history` in `backend/app/core/consult_schema.py`
   and `backend/app/core/schema.py`):
   - Append-only table `zone_c_query_history` (`id`, `kind`, `query_text`, `query_sha256`, `matched_groups_json`,
     `decision`, `reason_code`, `ai_job_id`, `created_at`) stored only inside the local data root SQLite DB (never
     exported, never logged to `search_guard.log`, never sent to cloud).
   - Indexed on `created_at` so the 30-day rolling group union and last-20 same-group lookup are bounded and fast.
3. **Offline zone-C leak benchmark (`scripts/zone_c_leak_bench.py`)**:
   - Reads a query corpus file (100–200 real queries supplied outside the public repo in the pack or vault; offline tests
     use a small synthetic fixture with the `zephyr` pack and a fake vault).
   - Runs every query sequentially through `evaluate_zone_c_request` against a temporary isolated data root, recording
     allowed vs blocked decisions and reasons.
   - Runs Sage (local model via `run_ai_task`, or injectable fake attacker in tests) as an offline attacker in two
     passes:
     - **Single-query pass**: given each allowed query alone, attempts to infer any specific value or `local_only` item.
     - **Whole-log pass**: given the full sequence of allowed queries, attempts to reconstruct any specific value or
       `local_only` item.
   - Writes `result.json` and a human-readable `summary.md` to an operator-chosen output directory outside the repo for
     grading by the maintainer's chat session, reporting allow/block rates and any attacker inference hits (release
     criterion: zero specific values or `local_only` items inferred).

Out of scope (binding non-goals):

- Objective automatic consult triggers (§8): step 4 (next spec).
- Relaxing zone-A gate rules or adding new trusted cloud providers.
- Committing any private query corpus or real project data into the public repository.
- Any new third-party dependency.

## Files likely touched

- `backend/app/modules/consult/zone_c_guard.py` (new), `backend/app/modules/consult/config.py`,
  `backend/app/modules/consult/mcp_server.py`
- `backend/app/core/consult_schema.py`, `backend/app/core/schema.py`,
  `backend/tests/test_migration_discipline.py`
- `scripts/search_guard.py`, `backend/app/modules/ai/search_guard_summary.py`
- `backend/app/modules/agents/hermes/paper_search_tool.py`,
  `backend/app/modules/agents/hermes/browser_page_tool.py`
- `scripts/zone_c_leak_bench.py` (new)
- `backend/tests/test_consult_382_zone_c.py` (new)

## Design constraints

- Fail closed at every stage: missing/corrupt vault when `JARVISOS_VAULT_PATH` is set, judge timeout, malformed judge
  JSON, or DB write error blocks the outbound query.
- Local inference judge and Sage attacker calls go through `run_ai_task` on local routes only and write `ai_jobs` rows
  (invariants 1 and 2).
- Raw `local_only` / `protected_propositions` text and raw blocked queries never appear in `search_guard.log`, API
  responses, or refusal messages.
- Tests run 100% offline with the fake `zephyr` pack, fake vault, and fake local judge/attacker.

## Acceptance criteria

1. Every zone-C door (`search_guard.py`, `paper_search_tool`, `browser_page_tool`, and `consult/mcp_server.py`) passes
   through `evaluate_zone_c_request` and blocks when any of the 3 stages fails, returning a secret-free reason so the
   caller can rephrase.
2. A query that passes deterministic spec-364 rules but is flagged by the local inference judge is blocked with
   `zone_c_inference_judge` and logs an `ai_jobs` row; malformed judge output blocks with `zone_c_judge_error`.
3. Rolling 30-day per-group counter blocks with `mosaic_group_threshold` once the union of touched pack concept groups
   reaches `min_groups`, and expires entries older than 30 days.
4. Contextual mosaic judge receives up to the last 20 allowed queries sharing a pack concept group with the current
   query and blocks with `zone_c_mosaic_judge` when the judge flags cross-query inference.
5. Strict mode is active by default and stays active unless `consult.toml` contains a valid ISO date for
   `zone_c_benchmark_passed_on`.
6. `scripts/zone_c_leak_bench.py` runs end-to-end offline with a synthetic corpus and fake attacker, producing
   `result.json` and `summary.md` with single-query and whole-log attacker evaluations.
7. Lint, mypy, focused tests, migration discipline gate, repo gates, and CI pass on exact head.

## Required tests

- `backend/tests/test_consult_382_zone_c.py`: all 3 stages and their order across all 4 zone-C doors, strict mode vs
  `zone_c_benchmark_passed_on`, rolling 30-day group counter and expiry, last-20 same-group mosaic lookup, fail-closed
  judge/DB errors, secret-free refusal messages, and offline `zone_c_leak_bench.py` run.
- `backend/tests/test_migration_discipline.py` digest and baseline for `0039_zone_c_query_history`.

## Definition of done

Merged with green CI on exact head after coordinator full-diff review, independent second-session full-diff review
(high risk: egress + additive migration), and a host proof running `scripts/zone_c_leak_bench.py` on a temporary data
root and fake vault with a real local judge and Sage attacker.
