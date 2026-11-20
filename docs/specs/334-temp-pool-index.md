# 334 — TEMP-POOL-INDEX-1: Free pools admitted by a public benchmark score and kept live by the provider's free list

Status: ready
Depends on: 333

## Goal

A free model joins the worker routing when its score on a public intelligence index is at least a named reference
worker's, at a tier read off the same index, and leaves by itself when its provider stops listing it as free. No
hand-written expiry date and no per-model qualification by the coordinator. A failing smoke run marks the bridge as
suspect instead of grading the model.

## Why

Directive `L20261120084344-afc2` amends spec 333 and `L20261119163228-83fc`: expiry dates are guesses, our own three
tasks are a weaker signal than an independent index, and qualifying every free model spends coordinator quota. Gemini
jobs that all failed were a broken connection, not a weak model: a strong model that fails simple tasks must point at
the bridge first. Offers are live now, so the change is small.

## Code facts checked at `03959b9a`

- `scripts/worker_routing.py::load_temp_pools` requires `expires_at`, `base_url` (https) and an absolute `key_file`
  per entry; a malformed entry becomes `invalid#<n>`.
- `scripts/worker_routing.py::temp_pool_read` gives free `1.0` before `expires_at` with the key file present, else
  `expired` / `no_key` / `invalid`.
- `scripts/worker_routing.py::qualify_temp_pool` derives the tier only from the three `tempqual-*` records;
  `temp_models` builds `Model(key="temp-<name>", kind="opencode", ...)` with that tier; `rank` skips a tier-0
  temporary model as `unqualified`.
- `scripts/worker_routing.py::copilot_effective_capability` adjusts a starting tier from recorded outcomes and ignores
  records with `failure == "harness"`.
- `scripts/opencode_worker.py::validate_pool` and `build_config` require a custom base URL and key;
  `execute_qualification` runs the three tasks and computes a tier.
- OpenCode's built-in Zen provider serves its free models without a key through the OpenCode client only (direct
  calls are refused, per the directive); the exact provider and model id form is checked by the implementation.

## Scope

In scope:
- `temp-pools.json` (private control directory, unchanged location) gains top-level fields: `index` (name and URL of
  the index), `reference` (worker key whose score is the admission bar, `luna` today), `scores` (map of existing
  worker key to index score, read by the coordinator) and `read_at` (date of that reading). Each pool entry gains
  `index_score` (integer) and `free_list` (`openai_models`, `openrouter_public` or `opencode_zen`). `expires_at`
  becomes optional; `base_url` and `key_file` are required only when the source needs them (`opencode_zen` needs
  neither; `openrouter_public` needs a key to call but not to read the list).
- Admission in `worker_routing.py`: a pool without `index_score` is skipped `unindexed`; below the reference worker's
  score it is skipped `below_index`. Its starting tier is the highest tier of any existing worker whose recorded score
  is at most the pool's score (no such worker: tier 1). The tier then moves with real outcomes by the same rule as
  `copilot_effective_capability`, records marked `failure: harness` excluded. `qualify_temp_pool` and its
  `tempqual-*` tier logic are deleted.
- Liveness: a cache `temp-pool-lists.json` beside `temp-pools.json` holds, per source, the free model ids and the
  time read. `pick` uses the cache; when it is older than one hour it refreshes it (10 s timeout per source; standard
  library HTTP for the two model-list endpoints, the OpenCode CLI's model list for Zen). A pool whose id is absent is
  skipped `not_listed`; a list that cannot be read and has no entry younger than one hour is skipped
  `list_unreadable`. A past `expires_at`, when present, still skips `expired`.
- `opencode_worker.py`: supports `opencode_zen` (built-in provider, no key, no base URL in the config); `qualify`
  becomes `smoke`: it runs the existing task set once, records each run with `failure: harness` on a non-success run
  status, and writes `bridge: ok|suspect` for the pool into the cache file. A pool with `bridge: suspect` is skipped
  `bridge_suspect` until a later smoke run is `ok`. The smoke result never sets a tier.
- `cached_summary` names the skip reason and, for a live pool, its index score.

Out of scope (binding non-goals):
- No scraping of the index site: the coordinator reads it and writes scores (directive point 2, an indicative check).
- No reading of promotion feeds in code (directive point 5d stays a coordinator read).
- No account, login or key creation by any agent; no use of a key that the maintainer marked not to route (the billed
  Gemini key of `L20261120090052-143e`); no private pack, maintainer data or private repository reaches a free pool.
- No supervisor or relay change; no daemon (the hourly refresh happens inside `pick`).

## Files likely touched

- `scripts/worker_routing.py`, `scripts/opencode_worker.py`
- `backend/tests/test_worker_routing.py`, `backend/tests/test_opencode_worker.py`

## Design constraints

- Standard library only. The key is never printed, logged, cached or written into evidence or config.
- Fail closed per pool: any doubt (file, schema, clock, list) skips that pool, never the others, never raises.
- The model-list requests carry no prompt and no repository content; only the Nous-style `/models` call sends the key.
- Delete over wrap: the old qualification tier path goes, not kept beside the new one.

## Acceptance criteria

1. With a fixture `temp-pools.json` (reference `luna` at 28, a tier-3 worker at 40) and a fresh list cache, a pool at
   43 listed by its source is picked for a tier-3 task; at 30 it gets tier 1 or 2 by the same table and is never
   picked above it; at 20 it is skipped `below_index`; with no score `unindexed`.
2. The same pool absent from the cached list is skipped `not_listed`; with a stale cache and a failing fetch,
   `list_unreadable`; an entry without `expires_at` is valid; a past `expires_at` is `expired`.
3. An `opencode_zen` entry without `base_url` and `key_file` loads; `opencode_worker.py run` builds a config naming the
   built-in provider with no key variable in the child environment (captured by a fake `opencode` on `PATH`).
4. `smoke` with a fake `opencode` that fails every run writes `bridge: suspect`, records `failure: harness`, and the
   pool is skipped `bridge_suspect`; a later passing smoke clears it. No tier changes in either case.
5. `qualify_temp_pool` and the `tempqual-*` tier rule no longer exist; the spec 333 tests that asserted them are
   rewritten to this behaviour, not deleted silently.
6. Live proof: one Nous free model and one Zen free model admitted from the maintainer's file, listed by their real
   sources, one smoke run each, results in the ledger.

## Required tests

- Offline unit tests for criteria 1–5 with fixture files, a fake HTTP opener and a fake `opencode` binary.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, high-risk evidence (second-session diff review, live proof
of criterion 6), STATUS row updated.
