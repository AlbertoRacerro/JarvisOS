# 364 — SEARCH-GUARD-SPECIFICITY-1: Specificity-based IP search guard and plain-Italian block transparency

Status: ready
Depends on: 353

## Goal

Refine how `scripts/search_guard.py` applies the existing IP egress policy (umbrella 059 / spec 353) so general phenomenon, physics, state-of-the-art, and broad-range engineering queries pass through cleanly, while queries exposing protected project terms, exact protected dimensions, or exact protected design combinations are blocked fail-closed. Surface recent blocked-query reasons (in plain Italian, without leaking secrets or protected pack terms into public logs) so the operator can inspect and tune pack guard rules.

## Why

Directive `L20261124094804-a929` (item 6): pure synonym-group co-occurrence counting blocks legitimate state-of-the-art and physics searches that use generic geometry words or wide dimension ranges, hurting research quality while adding no real IP protection. Replacing co-occurrence-only blocking with a pack-driven specificity rule (exact protected dimensions/values or exact protected design combinations, while keeping direct pack-term, sanitizer, and S2/S3/S4 floor blocks intact) preserves the existing egress boundary while unblocking legitimate scientific search.

## Code facts checked at `8e9f4831`

- `scripts/search_guard.py::evaluate_query` evaluates each incoming query against `pack_is_ready`, `packs.mentions_pack_term`, `matches_min_groups`, `sanitize_text`, and `check_sensitivity_floor`.
- `scripts/search_guard.py::matches_min_groups` counts case-insensitive substring matches across `SearchGuardConfig.groups` and blocks at `>= min_groups`.
- `scripts/search_guard.py::log_query` appends `timestamp status sha256_prefix` to `paths.build_paths().search_guard_log_file` without writing raw query text.
- `backend/app/core/packs.py::SearchGuardConfig` parses `[search_guard]` from `jarvis_pack.toml` (`groups`, `min_groups`).
- `backend/app/core/packs.py::search_guard` aggregates `SearchGuardConfig` across loaded packs.
- `backend/tests/fixtures/packs/zephyr/jarvis_pack.toml` defines the offline fake pack `[search_guard]` fixture.

## Scope

In scope:
- **Pack-driven specificity rules** (`backend/app/core/packs.py`, `backend/tests/fixtures/packs/zephyr/jarvis_pack.toml`):
  - Extend `SearchGuardConfig` additively (preserving backward compatibility with existing `groups` and `min_groups`) with optional specificity fields:
    - `exact_combinations`: list of lists of terms where a query must match ALL terms in a single protected combination to trigger `exact_combination` block;
    - `protected_dimensions`: list of `{value: float, unit: str, tolerance_rel?: float}` (or normalized exact-value specs) so a query mentioning an exact protected dimension (not a broad range like `40–60 mm`) alongside a protected context group triggers `protected_dimension` block;
    - `allow_generic_ranges`: boolean (default `True`) so explicit numeric ranges (`A-B unit` / `A–B unit` / `A to B unit` spanning > 20% width) are not mistaken for exact protected dimensions.
- **Specificity evaluation in `scripts/search_guard.py`**:
  - Keep fail-closed invariants unchanged: `pack_load_error`, `pack_term` (`packs.mentions_pack_term`), `sanitized_diff`, and `sensitivity_floor` (`S2`/`S3`/`S4`) always block.
  - When a pack defines `exact_combinations` or `protected_dimensions`, evaluate those specificity rules instead of blocking on generic term co-occurrence alone; retain `matches_min_groups` fallback only when a pack defines legacy `groups` without specificity rules.
  - Map every block reason code (`pack_term`, `exact_combination`, `protected_dimension`, `guard_groups`, `sanitized_diff`, `sensitivity_floor`, `invalid_query`, `pack_load_error`, `guard_error`) to a concise plain-Italian human explanation (`reason_it`) that names the rule category without echoing secrets or protected pack literals.
- **Operator visibility for blocked queries** (`scripts/search_guard.py`, `backend/app/modules/settings/` or `backend/app/modules/ai/` read-only endpoint + existing Settings/Search or AI panel):
  - Record structured JSONL entries (timestamp, status, sha256 prefix, `reason_code`, `reason_it`, and optional safe rule index — never raw query text or pack literals) in the local data-root search-guard log, while keeping the existing log path and fail-closed write requirement.
  - Provide a bounded read-only API endpoint returning recent blocked-query counts and plain-Italian reasons (latest 50 entries) for operator tuning, surfaced in the existing Settings/AI diagnostics view (or a compact section in the existing Search/Research status card) with a static frontend test.

Out of scope (binding non-goals):
- No redefinition of the cloud IP egress policy (umbrella 059 / ADR-059) or sensitivity tiers.
- No private pack contents or real project dimensions in this public repository; tests use the fake `zephyr` pack only.
- No SQLite schema migration.
- No logging of raw query text, secrets, or protected pack literals.

## Files likely touched

- `backend/app/core/packs.py`
- `scripts/search_guard.py`
- `backend/tests/fixtures/packs/zephyr/jarvis_pack.toml`
- `backend/tests/test_search_guard_353.py`
- `backend/tests/test_search_guard_specificity_364.py` (new)
- Read-only status endpoint & existing Settings UI component + frontend `.mjs` test (and `e2e/` walk if UI changes)

## Design constraints

- High risk (egress/IP boundary): requires full independent second-session diff review, CI green on exact head, and live proof on an isolated temporary data root / loopback guard instance.
- Backward-compatible pack schema: packs without the new optional `[search_guard]` keys continue to load and enforce existing rules without error; core still runs without any pack loaded.
- Fail-closed on malformed pack config, log write failure, or evaluation exception.

## Acceptance criteria

1. Generic physics/engineering queries with broad dimension ranges (e.g. `40-60 mm`) pass through the search guard when no protected pack term, exact combination, or exact protected dimension is present.
2. Queries matching a pack's `exact_combinations` or `protected_dimensions` (or `pack_term` / `sanitized_diff` / `S2`-`S4`) are blocked fail-closed with empty results and the corresponding `reason_code` + plain-Italian `reason_it`.
3. The local search-guard log records `reason_code` and `reason_it` with the query SHA-256 prefix only (never raw query text or pack terms), and a write failure blocks egress fail-closed.
4. A bounded read-only endpoint exposes recent blocked-query summaries in plain Italian for operator tuning without leaking raw queries or secrets.
5. All existing spec 353 tests and new spec 364 offline tests pass; core runs with no pack loaded.

## Required tests

- Offline backend tests in `backend/tests/test_search_guard_specificity_364.py` covering criteria 1–4 with the fake `zephyr` pack and no-pack mode.
- Existing `backend/tests/test_search_guard_353.py` remains green.

## Definition of done

Test gate green (see `AGENTS.md`), full independent second-session diff review (high risk), isolated live loopback guard proof, CI green on exact head, STATUS row reconciled.
