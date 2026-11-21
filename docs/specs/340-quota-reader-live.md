# 340 — QUOTA-READER-LIVE-1: Read Claude quota from the supervisor's live config dir, refresh Codex limits, and show clear unavailable reasons

Status: ready
Depends on: none

## Goal

The Coding > Runtime usage panel reads Claude subscription quotas from the live credentials directory maintained by the supervisor (`JARVISOS_CLAUDE_CONFIG_DIR` / `CLAUDE_CONFIG_DIR` / default sandbox state dir) without copying tokens or requiring a second login, refreshes Codex limits using the live Codex home when configured and shows old reading ages clearly, and displays plain Italian reasons when any provider is unavailable.

## Why

Directive `L20261120221141-829f`: `agent-relay --json limits` hardcodes `~/.claude/.credentials.json` and ignores `CLAUDE_CONFIG_DIR`, so Claude showed `"Not available"` whenever `~/.claude` held an expired login while the supervisor kept its own sandbox login fresh. Codex readings can also go stale, and generic `"Not available"` labels hide why a provider failed.

## Code facts checked at `a3371e45`

- `backend/app/modules/coding/provider_quota.py::read_relay_limits` reads Claude usage only through `agent-relay --json limits`, which reads `~/.claude/.credentials.json` and ignores `CLAUDE_CONFIG_DIR`.
- `backend/app/modules/coding/provider_quota.py::read_codex_limits` invokes `codex app-server` and queries `account/rateLimits/read` without setting `CODEX_HOME` to the supervisor's sandbox state when `JARVISOS_CODEX_HOME` is unset.
- `backend/app/modules/coding/provider_quota.py::read_all` and `backend/app/modules/coding/provider_quota.py::normalize` assemble the per-provider payloads (`claude`, `codex`, `antigravity`, `copilot`).
- `backend/app/modules/coding/provider_quota.py::normalize_agent` maps non-`ok` provider statuses to short reason codes (`not_authenticated`, `no_reading`, `no_usage_windows`, etc.).
- `frontend/src/components/coding/ProviderQuotaPanel.tsx` maps `REASONS` in English and formats `age()` only in minutes (`read N min ago`), even when a reading is hours or days old.

## Scope

In scope:
- **Live Claude usage reader in `backend/app/modules/coding/provider_quota.py`**:
  - Resolve the Claude credentials file from `JARVISOS_CLAUDE_CONFIG_DIR` or `CLAUDE_CONFIG_DIR` (if set), else `~/.local/share/jarvisos-sandbox/state/claude/.credentials.json` when present, falling back to `read_relay_limits` only when no configured/sandbox credentials file exists.
  - Read `claudeAiOauth.accessToken` (and check `expiresAt` if present) without copying or modifying any credential file; never log, return, or store the token.
  - Query `https://api.anthropic.com/api/oauth/usage` with `Authorization: Bearer <token>` and `anthropic-beta: oauth-2025-04-20` using standard-library `urllib` with a 10 s timeout, no redirects, no ambient proxies, and bounded response read (64 KiB).
  - Parse `five_hour`, `seven_day`, and any frontier/model window (`seven_day_opus`, etc.) into the existing normalized window list.
  - Classify errors distinctly: `credentials_missing`, `session_expired` (expired `expiresAt` or HTTP 401/403), `rate_limited` (HTTP 429), `provider_unreachable`, `invalid_response`.
- **Codex home resolution and plain age display**:
  - In `read_codex_limits`, pass `CODEX_HOME` from `JARVISOS_CODEX_HOME` or `CODEX_HOME`, or `~/.local/share/jarvisos-sandbox/state/codex` when that directory exists, so `codex app-server` reads the supervisor's active login.
  - In `ProviderQuotaPanel.tsx`, format `age()` clearly in Italian across minutes, hours, and days (`letto ora`, `letto N min fa`, `letto N h fa`, `letto N giorni fa`) so an old reading is immediately obvious.
- **Plain Italian unavailable reasons**:
  - Update `REASONS` in `ProviderQuotaPanel.tsx` (and matching frontend `.mjs` tests) to plain Italian explanations (`session_expired` -> `"Login scaduto"`, `credentials_missing` -> `"Credenziali assenti"`, `not_authenticated` -> `"Accesso non effettuato"`, `rate_limited` -> `"Limite richieste superato"`, `provider_unreachable` -> `"Servizio non raggiungibile"`, etc.) instead of a generic `"Not available"`.

Out of scope (binding non-goals):
- Full grid redesign and free-model / prepaid-balance sections (`L20261120104844-9d5d`, separate spec).
- Refreshing or writing OAuth tokens (read-only; the supervisor owns token refresh).
- Creating a second login in `~/.claude`.

## Files likely touched

- `backend/app/modules/coding/provider_quota.py`
- `frontend/src/components/coding/ProviderQuotaPanel.tsx`
- `configs/architecture_enforcement.json` (if an AE002 allowlist entry is needed for the direct HTTPS usage read)
- `backend/tests/test_provider_quota_217.py` (or the existing provider quota test file)
- `frontend/tests/` (matching `.mjs` assertions)

## Design constraints

- Standard library only; no new dependencies.
- Never log, return, cache on disk, or copy any OAuth token or credential file.
- Fail closed to `status: "unavailable"` with a clear reason code on any unreadable file, expired token, redirect, proxy, timeout, or malformed JSON.
- High risk (reads credentials file + supervisor/quota surface): requires second-session diff review and live verification plus an `e2e/` browser walk for the UI change.

## Acceptance criteria

1. With a fixture credentials file under a temporary `JARVISOS_CLAUDE_CONFIG_DIR` and a fake HTTPS opener returning `five_hour` and `seven_day` utilization, `provider_quota.py` returns `status: "ok"` with both windows and never includes the token in the payload, logs, or error strings; with an expired `expiresAt` or HTTP 401 it returns `status: "unavailable", reason: "session_expired"`.
2. When `JARVISOS_CLAUDE_CONFIG_DIR` is unset and the default sandbox credentials file is absent, `provider_quota.py` falls back cleanly to `read_relay_limits` (or `credentials_missing`).
3. `read_codex_limits` passes `CODEX_HOME` when `JARVISOS_CODEX_HOME` or the sandbox Codex dir exists; `ProviderQuotaPanel.tsx` displays multi-hour and multi-day reading ages in plain Italian.
4. `ProviderQuotaPanel.tsx` renders plain Italian unavailable reasons (including `"Login scaduto"` for `session_expired`) instead of generic `"Not available"`.

## Required tests

- Offline backend unit tests for criteria 1–3 with fixture credentials files, fake HTTP opener (including redirect/error/401/429 cases), and fake `codex` binary.
- Frontend `.mjs` test updates and `npm run build` green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, second-session diff review, `e2e/` browser walk covering Coding > Runtime provider usage, spec status updated.
