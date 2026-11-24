# 349 — QUOTA-PANEL-LIVE-FIX-1: Fix Claude session refresh, Codex reader resilience, and live-verified free models

Status: ready
Depends on: 340, 346, 347

## Goal

Fix the two live quota-panel failures observed on 2026-11-23 (Claude `session_expired` when the sandbox access token has expired while a valid refresh token exists; Codex `codex_no_json` when `codex app-server` fails or returns non-JSON), and enforce the shared live free-model rule so neither the quota panel nor the worker router ever shows or routes a model unless its free ID is listed at zero price, a bounded free-only request to that ID has succeeded recently, and its Intelligence and coding index scores both meet the GPT-6 Luna bar.

## Why

Directive `L20261123232512-a0c3` (item 2): on 2026-11-23 Coding > Runtime showed Claude `"aggiornamento fallito"` (`session_expired`, stale windows), Codex `"non disponibile"` (`codex_no_json`), and `nous-qwen4-max` still listed as `"Attivo"` based on an expiry date after Nous ended its free period (404).

## Code facts checked at `20bdbfdc`

- `backend/app/modules/coding/provider_quota.py::_resolve_claude_credentials_path` resolves the sandbox Claude credentials file (`~/.local/share/jarvisos-sandbox/state/claude/.credentials.json`).
- `backend/app/modules/coding/provider_quota.py::read_claude_limits` immediately returns `session_expired` when `claudeAiOauth.expiresAt <= now`, even when `refreshToken` is present and valid or when a fresh supervisor quota snapshot exists.
- `backend/app/modules/coding/provider_quota.py::read_codex_limits` spawns `codex app-server` with `stderr=DEVNULL` and returns `"no_json"` whenever `response(1)` or `response(2)` yields `None`, without retrying with fallback `CODEX_HOME` or distinguishing startup/auth errors.
- `backend/app/modules/coding/provider_quota.py::read_free_models` lists entries from `temp-pools.json` without verifying that each model is currently present in a fresh live free-list check (`temp-pool-lists.json` younger than TTL) and meets both Intelligence Index and coding index thresholds.
- `scripts/worker_routing.py::temp_pool_read` checks `index_score >= reference` and `listed_ids`, but a listed ID can still return 404 on a model request. It does not check a coding-index bar or a recent successful free-only response.

## Scope

In scope:
- **Claude live refresh (`read_claude_limits`)**:
  - When `expiresAt <= now` (or `_fetch_claude_usage` returns 401/403) and a `refreshToken` is present in the configured/sandbox credentials file, attempt a bounded refresh through `agent-relay --json limits --refresh` with `CLAUDE_CONFIG_DIR` pointing to the credentials directory (without creating a second login, copying tokens, or logging secrets).
  - If refresh succeeds, return fresh `status: "ok"` windows; if refresh fails because the refresh token itself is expired/revoked, return `session_expired` clearly.
- **Codex reader resilience (`read_codex_limits`)**:
  - Try the configured/sandbox `CODEX_HOME` first; if `codex app-server` exits without JSON or fails auth, retry once with the default user `CODEX_HOME` (if distinct and present) or read a fresh supervisor `worker-quota.json` Codex reading (preserving its exact `as_of` timestamp so age is shown honestly).
  - Return a clear, accurate reason code (`not_found`, `timeout`, `auth_required`, `no_json`) instead of hiding all errors under `codex_no_json`.
- **Shared live free-model rule (panel + router)**:
  - A model is shown in `"Modelli gratis ora"` (`read_free_models`) AND routed by `worker_routing.py::temp_pool_read` ONLY when:
    1. Its explicitly free-tier model ID is verified in a fresh source free list with zero prompt and completion price where the source publishes prices; a bounded request to that exact free ID has recently returned a usable answer. A 404, payment response, timeout, missing proof, or stale proof removes it from both surfaces. `expires_at` alone is never proof. The probe never falls back to a paid ID and never sends private content.
    2. Curated `index_score` and `coding_index` are both present and meet their respective GPT-6 Luna reference scores. Missing scores or reference values fail closed; neither index is optional.
  - Models that fail either check are omitted completely from `free_models` in the quota panel response (not shown as `"Attivo"` or `"Scaduto"`).

Out of scope (binding non-goals):
- No second Claude login in `~/.claude`; no copying tokens between files; no logging or returning tokens.
- No paid-model calls; safe defaults stay safe.

## Files likely touched

- `backend/app/modules/coding/provider_quota.py`
- `scripts/worker_routing.py`
- `backend/tests/test_provider_quota_live_fix_349.py` (new)
- `backend/tests/test_provider_quota_340.py`, `backend/tests/test_quota_panel_grid_346.py`, `backend/tests/test_worker_routing.py` (if expectations need alignment)

## Design constraints

- Never log, return, or copy OAuth access/refresh tokens.
- Fail closed on free-model liveness and quality: missing or stale free-list, successful-response proof, price evidence where published, or either index score/reference means zero affected models shown/routed. Keep the proof bounded and cached so panel requests do not call models.
- High risk (credentials/quota reading and routing): requires coordinator full-diff review, independent second-session full-diff review, live verification on the running backend, and green CI.

## Acceptance criteria

1. When sandbox Claude `.credentials.json` has an expired `accessToken` and a valid `refreshToken`, `read_claude_limits(refresh=True)` refreshes via `agent-relay` with `CLAUDE_CONFIG_DIR` set and returns `status: "ok"` with live windows (and returns `session_expired` only when refresh genuinely fails), with zero token leakage.
2. When `codex app-server` fails on one `CODEX_HOME`, `read_codex_limits` recovers via fallback/snapshot with accurate `as_of`, and returns an accurate reason when genuinely unavailable.
3. `read_free_models` and `worker_routing.py::temp_pool_read` omit a pool when its free-list evidence is stale, its exact free ID returns 404 or another unusable response, its successful-response proof is stale or absent, its published price is nonzero or unknown, or either curated index score/reference is absent or below GPT-6 Luna. A future `expires_at` cannot make it eligible.
4. Live verification on the backend confirms Claude, Codex, and `free_models` report truthful live status.

## Required tests

- Offline unit tests in `backend/tests/test_provider_quota_live_fix_349.py` covering criteria 1–3 with fake credentials, fake subprocess/opener, and fixture free-list, zero-price, successful-response, 404, and index evidence. No test calls a paid provider.
- Existing `test_provider_quota_340.py`, `test_quota_panel_grid_346.py`, `test_worker_routing.py` remain green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator full-diff review + independent second-session full-diff review, live backend proof, spec status updated.
