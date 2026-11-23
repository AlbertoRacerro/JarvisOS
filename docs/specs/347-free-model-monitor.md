# 347 — FREE-MODEL-MONITOR-1: Daily free-model list monitor, automatic spec-334 admission, and Italian Slack alerts

Status: ready
Depends on: 334, 346

## Goal

Monitor free model lists continuously (at least once per day) across public and configured OpenAI-compatible sources, automatically admit qualifying free-only models into `temp-pools.json` under Spec 334's Intelligence Index bar (and prune delisted ones), watch for newly free flagship releases (including GLM 5.4), and notify the maintainer on Slack in one plain Italian line whenever a strong model becomes free or stops being free.

## Why

Directive `L20261123225111-e7c1` (item 4): temporary free periods expire without warning (e.g., `qwen/qwen4-max:free` returned 404 after its free period ended) while new free models appear across Nous, OpenRouter, OpenCode Zen, Z.ai free Flash, and Gemini free tier. Keeping `temp-pools.json` current by hand misses short windows and leaves stale entries behind. Automating the daily check in the supervisor loop keeps free capacity live without spending coordinator turns or risking paid-model calls.

## Code facts checked at `f3435475`

- `scripts/worker_routing.py::load_temp_pools`, `fetch_free_ids`, `listed_ids`, `temp_pool_read`, and `FREE_LISTS` (`openai_models`, `openrouter_public`, `opencode_zen`) implement Spec 334 admission (`index_score >= reference`) and hourly liveness caching in `temp-pool-lists.json`.
- `scripts/relay_continuity_roadmap.py::command_serve` runs the continuous supervisor loop inside `jarvis-sandbox` with `serve_store = Store(root / "serve")` and `Notifier(state, sender=sender, log=log)`.
- `scripts/relay_continuity_notify.py::Notifier.send_text` posts plain-Italian messages to Slack via the configured bot/webhook with deduplication.
- `backend/app/modules/coding/provider_quota.py::_read_temp_pools` surfaces admitted pools from `temp-pools.json` in the `"Modelli gratis ora"` UI section (Spec 346).

## Scope

In scope:
- New stdlib-only module `scripts/free_model_monitor.py` with CLI `check [--force] [--notify] [--control-dir ...]`:
  - **Sources**: reads free model IDs from:
    1. OpenRouter public models list (`openrouter_public`, `:free` suffix and verified zero prompt/completion pricing when pricing metadata is present),
    2. OpenCode Zen public models list (`opencode_zen`, `-free` suffix),
    3. Configured OpenAI-compatible sources (e.g. Nous `:free` with price=0 check, Z.ai free Flash, Gemini free tier) declared in an optional private `free-monitor-sources.json` beside `temp-pools.json` (each entry specifies `source_id`, `provider`, `free_list`, `base_url`, `key_file`, `free_suffix`, `rate_limits`, `data_policy`). Any source without a readable key file or whose endpoint fails is skipped fail-closed without blocking other sources.
  - **Free-only safety**: only model IDs matching the source's free-tier rule (e.g. `:free`, `-free`, or explicit zero-price metadata) are ever considered; paid model IDs are never admitted or called.
  - **Automatic Spec-334 admission & pruning**:
    - Reads `reference`, `scores`, and optional `catalog_scores` (known model ID -> Intelligence Index integer score) and `watchlist` (e.g. `glm-5.4` / `glm5.4`) from `temp-pools.json` / `free-monitor-sources.json`.
    - Automatically adds/updates `auto_managed: true` entries in `temp-pools.json` for discovered free models whose `index_score >= reference`, preserving top-level metadata (`index`, `reference`, `scores`, `read_at`) and any manually managed entries (`auto_managed` not true).
    - Removes `auto_managed: true` entries (and flags delisted manual entries) when their model ID disappears from its source's free list or its `expires_at` has passed.
    - Writes `temp-pools.json` atomically (`tempfile` + `os.replace`) and updates `free-model-monitor-state.json` beside it (`last_checked_at`, `seen_free_by_source`, `admitted_strong`, `watched_hits`).
  - **Plain-Italian Slack alerts on transitions**:
    - Emits at most one concise plain-Italian line per transition when a strong qualifying model (or a watched model such as GLM 5.4) newly appears as free or stops being free:
      - Added: `"Nuovo modello gratuito disponibile: <model> su <provider>."`
      - Removed: `"Il modello gratuito <model> su <provider> non è più disponibile."`
    - Never includes spec numbers, PR numbers, SHAs, API keys, or internal IDs.
    - Deduplicated via `free-model-monitor-state.json` so repeated runs never re-send the same transition.
- **Daily supervisor hook**:
  - `scripts/relay_continuity_roadmap.py::command_serve` invokes `free_model_monitor.maybe_run_daily(...)` (default interval 24 hours, persisted in `free-model-monitor-state.json`) and passes the active `Notifier` callback when `--no-notify` is not set. Any exception is logged and swallowed so monitoring can never crash or hold the supervisor.

Out of scope (binding non-goals):
- No web scraping of Artificial Analysis or third-party leaderboards in code; index scores stay in the curated `temp-pools.json` / `free-monitor-sources.json` score map.
- No account creation, login, or use of billed keys marked not-to-route; no private pack or repository content sent to free endpoints.
- No frontend changes (Spec 346 already renders admitted pools from `temp-pools.json`).

## Files likely touched

- `scripts/free_model_monitor.py` (new)
- `scripts/relay_continuity_roadmap.py` (daily hook in `command_serve`)
- `backend/tests/test_free_model_monitor_347.py` (new)
- `docs/MODULES.md` (if regenerated by `scripts/gen_modules.py`)

## Design constraints

- Standard library only; never print, log, or write API keys into state, evidence, or notifications.
- Fail-closed per source: one unreachable endpoint or malformed JSON never aborts other sources or corrupts `temp-pools.json`.
- Zero-price verification: when a source returns `pricing` fields (e.g. OpenRouter/Nous `prompt`/`completion`), reject any model whose price is non-zero even if its ID ends with `:free`.
- High risk (touches supervisor loop and pool admission): requires coordinator full-diff review, independent second-session full-diff review, and green CI.

## Acceptance criteria

1. Given fixture free lists (OpenRouter, Zen, and an OpenAI-compatible source with pricing metadata), `free_model_monitor.check` admits only free-only zero-priced models with `index_score >= reference` into `temp-pools.json` as `auto_managed: true`, skips non-zero-priced or below-index models, and preserves top-level fields and manual entries.
2. On a subsequent check where a previously admitted model disappears from its source's free list, it is pruned from `temp-pools.json` and a plain-Italian removal alert is emitted once.
3. On a check where a strong qualifying model or a watched model (e.g. `glm-5.4:free`) newly appears, a plain-Italian availability alert is emitted once (and not repeated on the next check with unchanged lists).
4. `maybe_run_daily` skips network fetches when `last_checked_at` is younger than 24 hours (unless `force=True`) and swallows/logs fetch failures without raising into `command_serve`.
5. No API key or secret appears in `temp-pools.json`, `free-model-monitor-state.json`, stdout/stderr, or emitted alert text.

## Required tests

- Offline unit tests in `backend/tests/test_free_model_monitor_347.py` with fake HTTP openers covering criteria 1–5, including zero-price enforcement, GLM 5.4 watchlist match, atomic state persistence, 24h cadence gating, fail-closed single-source error handling, and supervisor hook safety.
- Existing `test_worker_routing.py`, `test_relay_continuity.py` remain green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator full-diff review + independent second-session full-diff review, spec status updated.
