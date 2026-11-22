# 346 — QUOTA-PANEL-GRID-1: Full-grid provider quota panel with free models and read-only prepaid balances

Status: merged
Depends on: 340

## Goal

Redesign `ProviderQuotaPanel.tsx` and the `/coding/quotas` payload into a dense, aligned provider-by-row grid in plain Italian with three clear sections: (1) subscription quota grid (columns = providers, rows = semantic quota kinds with consistent empty-state cells), (2) `"Modelli gratis ora"` showing every currently admitted free pool with its source, Artificial Analysis Intelligence Index score, and limit/live indicator, and (3) a strictly read-only `"Saldi prepagati"` section displaying untouched prepaid balances and monthly spend caps so any accidental spend is immediately visible.

## Why

Directives `L20261120104844-9d5d` and `L20261120110957-14a2`: the current `ProviderQuotaPanel` renders sparse single-cell rows (e.g., Argon or Copilot pools isolated with mostly blank columns), mixes English and Italian labels, and does not show the active admitted free worker pools or the read-only prepaid balance/cap watchdog. Under `L20261120110957-14a2` (FREE MODELS ONLY), prepaid keys are never routed for worker execution; the prepaid section is strictly read-only monitoring.

## Code facts checked at `95e165e3`

- `backend/app/modules/coding/provider_quota.py::read_all` collects provider usage readings with TTL caching.
- `backend/app/modules/coding/provider_quota.py::normalize` transforms raw readings into normalized `providers[]` and `windows[]`.
- `backend/app/modules/coding/provider_quota.py::window_kind` classifies windows into `five_hour`, `weekly`, `frontier`, and `pool`.
- `backend/app/modules/coding/provider_quota.py::quotas` serves the cached quota response for the Coding control-room endpoint.
- `frontend/src/components/coding/ProviderQuotaPanel.tsx` renders `QuotaRing` and `ProviderQuotaView`.
- `frontend/src/api/coding.ts` defines `ProviderQuotas`, `ProviderQuota`, and `ProviderQuotaWindow`.

## Scope

In scope:
- **Backend `/coding/quotas` payload extension (`provider_quota.py`)**:
  - Keep existing `providers[]` fields backward-compatible while adding two optional read-only lists (never returning API keys, tokens, or host filesystem paths):
    - `free_models[]`: read from `<control_root>/temp-pools.json` (plus bridge state from `worker_routing` state if present). Each entry carries `{ id, label, source, model_id, intelligence_index, status, limit_kind, remaining_percent, exhausted }` where `status` is one of `"active" | "exhausted" | "throttled" | "unqualified"`, `limit_kind` is `"ring"` (known daily/request limit) or `"live_dot"` (unknown limit), and never includes `key_file`, `api_key`, or raw URLs with credentials.
    - `prepaid_balances[]`: strictly read-only metadata for configured prepaid accounts (`provider`, `label`, `mode: "read_only"`, `balance_usd` or `spent_eur` / `cap_eur` from local supervisor spend records or optional read-only balance cache, `status: "ok" | "unavailable"`, `reason` in plain Italian). Never initiates a paid model turn or spends credit; offline tests mock any HTTP balance probe.
- **Frontend full-grid redesign (`ProviderQuotaPanel.tsx`, `coding.ts`, CSS)**:
  - Plain Italian labels throughout (`"Utilizzo provider"`, `"Aggiorna"`, `"5 ore"`, `"Settimanale"`, `"Modello di frontiera"`, `"Extra usage"`, `"Esaurito"`, `"rimasto"`, `"Non riportato"`, `"Non disponibile"`).
  - **Section 1 — Subscription grid**: columns = providers (`Claude`, `OpenAI / Codex`, `Google`, `GitHub`); rows = semantic quota kinds (`5 ore`, `Settimanale`, `Modello di frontiera`, `Extra usage`), aligning `Claude via AGY` in the Claude column, `Argon` in the Google column, and the Copilot monthly pool in the GitHub column so rows stay dense. Where a provider genuinely lacks a row, render the consistent empty-state cell (`dot` + `"Non applicabile"` / `"Non riportato"`), never a blank square.
  - **Section 2 — `"Modelli gratis ora"`**: compact list/grid of admitted free pools with source badge (Nous, OpenCode Zen, OpenRouter, Gemini free, Z.ai free), Intelligence Index score, ring when a known limit exists, live dot when limit is unknown, and `"Esaurito"` state when exhausted. Empty state `"Nessun modello gratis attivo"` when none configured.
  - **Section 3 — `"Saldi prepagati (sola lettura)"`**: compact read-only strip showing untouched balances and supervisor-recorded spend vs cap (e.g., DeepSeek balance, Z.ai recorded spend, Gemini paid key vs 3 EUR cap), with `"Sola lettura — modelli a pagamento disattivati"` caption so the maintainer can spot any accidental spend.
- **Static frontend tests & AI browser walk**:
  - Update `frontend/tests/` static `.mjs` tests for the new Italian labels and sections, and run an `e2e/` browser walk proving the Coding > Runtime quota panel renders cleanly with zero console errors.

Out of scope (binding non-goals):
- Routing workers to any paid or prepaid model (`L20261120110957-14a2` strictly forbids spending prepaid credit).
- Changing `scripts/worker_routing.py` admission rules or `scripts/opencode_worker.py` (handled separately).
- Any database schema migration.

## Files likely touched

- `backend/app/modules/coding/provider_quota.py`
- `backend/tests/test_provider_quota.py`
- `frontend/src/api/coding.ts`
- `frontend/src/components/coding/ProviderQuotaPanel.tsx`
- `frontend/src/styles/lumen.css` (or existing coding quota stylesheet)
- `frontend/tests/` (relevant `.mjs` test files)
- `docs/MODULES.md` (if line counts cross rounding boundary)

## Design constraints

- Strictly read-only: refreshing usage never executes a model turn and never spends paid/prepaid credit.
- Zero secrets or host paths in `/coding/quotas` responses or frontend DOM.
- Core runs without private control files: when `temp-pools.json` or balance files are absent, `free_models: []` and `prepaid_balances: []` render clean Italian empty states.
- Medium risk (backend read-only payload + UI redesign): requires coordinator full-diff review, green CI, and an `e2e/` browser walk before asking the maintainer to look.

## Acceptance criteria

1. `ProviderQuotaPanel` displays aligned provider columns (`Claude`, `OpenAI / Codex`, `Google`, `GitHub`) and semantic rows (`5 ore`, `Settimanale`, `Modello di frontiera`, `Extra usage`) with consistent empty-state cells and plain Italian labels.
2. `"Modelli gratis ora"` renders admitted free pools from `/coding/quotas` with source, Intelligence Index score, and ring / live-dot / exhausted state without leaking key paths or credentials.
3. `"Saldi prepagati"` renders read-only balance/spend/cap entries with zero spend or model turns triggered by refresh.
4. Backend pytest (`test_provider_quota.py`), frontend `npm run build` (including `.mjs` tests and `tsc`), and an `e2e/` browser walk pass cleanly.

## Required tests

- Offline backend unit tests in `backend/tests/test_provider_quota.py` verifying `free_models` and `prepaid_balances` serialization, secret/path stripping, empty-file fallback, and zero external network calls in default offline mode.
- Frontend `.mjs` tests updated and passing under `npm run build`.
- `e2e/` browser walk capturing the Coding > Runtime `ProviderQuotaPanel` surface from a clean profile at `/`.

## Definition of done

Test gate green (see `AGENTS.md`), browser walk evidence attached, acceptance criteria met, coordinator full-diff review, spec status updated.
