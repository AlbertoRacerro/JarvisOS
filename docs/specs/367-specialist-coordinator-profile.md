# 367 — SPECIALIST-COORDINATOR-PROFILE-1: Dedicated specialist coordinator profile for complex migrations and major architecture changes

Status: ready
Depends on: 360

## Goal

Add a vendor-neutral coordinator profile (`standard` default vs `specialist`) to the continuity supervisor and account-local model resolver so ordinary roadmap cycles use the standard coordinator role while complex migrations or major architecture missions can opt into a dedicated specialist coordinator role, resolved fail-closed from the account's live CLI catalog without hardcoding permanent governance around vendor or model names.

## Why

Directive `L20261124085849-d342` (item 2): after spec 360 replaced hardcoded versioned model IDs with fail-closed account-local resolution, the supervisor needs a clean, profile-based way to route ordinary cycles to the latest standard coordinator and explicitly marked complex migrations or major architecture missions to a dedicated specialist coordinator profile, while keeping `AGENTS.md` free of vendor/model-specific governance.

## Code facts checked at `5060a233`

- `scripts/account_model_resolution.py::ROLES` maps role names (including `coordinator_codex`, `coordinator_claude`, `coordinator_neon`, `coordinator_hydrogen`) to `Role(cli, stem, effort, preview)`.
- `scripts/account_model_resolution.py::resolve` resolves the highest `(major, minor)` account-available model ID for a named role or raises `Refused`.
- `scripts/relay_continuity_adapters.py::RelayAdapter.start` resolves `coordinator_codex` (or native agent roles) when `JARVISOS_ACCOUNT_MODEL_RESOLUTION=1` and launches the coordinator process.
- `scripts/relay_continuity_missions.py::validate_mission` validates immutable submitted mission definitions.
- `scripts/relay_continuity_missions.py::serve_step` schedules queued missions one at a time.
- `scripts/relay_continuity_turns.py::task_marker` and `scripts/relay_continuity_turns.py` record turn metadata and verify init/session events.

## Scope

In scope:
- **Role mapping for coordinator profiles (`scripts/account_model_resolution.py`)**:
  - Support an explicit optional coordinator profile (`"standard"` default, `"specialist"`) with a dedicated role key (e.g. `coordinator_codex_specialist`, configurable via a bounded environment/config override for the role stem without hardcoding permanent vendor governance in docs or mission contracts).
  - Add a pure helper `coordinator_role_name(agent: str, profile: str = "standard") -> str` that returns the specialist role when `profile == "specialist"` and a specialist role exists for that agent, otherwise falls back cleanly to `coordinator_{agent}` (never inventing unconfigured roles; fail-closed on unknown profile names).
- **Mission & launch wiring (`scripts/relay_continuity_missions.py`, `scripts/relay_continuity_adapters.py`, `scripts/relay_continuity_supervisor.py`, `scripts/relay_continuity_turns.py`)**:
  - Allow an optional `coordinator_profile` field (`"standard" | "specialist"`, default `"standard"`) on mission definitions in `validate_mission` (immutable once submitted; invalid values rejected fail-closed).
  - Thread `coordinator_profile` from the active mission into `RelayAdapter.start` (and `limits` preflight when checking catalog readiness) so specialist missions resolve the specialist role through `account_model_resolution.resolve(...)` and record `coordinator_profile` in the attempt/session metadata.
  - Preserve resume pinning: resuming a specialist turn keeps the exact `selected-model.json` / `resume_model` verified against the specialist role pattern, never silently switching profiles mid-session.
  - Keep `standard` as the default for all roadmap cycles and existing missions with no `coordinator_profile` field (full backward compatibility with stored state and existing missions).

Out of scope (binding non-goals):
- No automatic heuristic that guesses whether a mission is "complex" from free text; specialist profile is chosen explicitly on the mission definition.
- No permanent vendor/model names encoded into `AGENTS.md`, `docs/PROCESS.md`, or mission governance rules.
- No changes to quota reserve thresholds, worker routing tiers, or sandbox containment boundaries.
- No live activation of `JARVISOS_ACCOUNT_MODEL_RESOLUTION=1` or specialist profile until tests and independent second-session review pass.

## Files likely touched

- `scripts/account_model_resolution.py`
- `scripts/relay_continuity_missions.py`
- `scripts/relay_continuity_adapters.py`
- `scripts/relay_continuity_supervisor.py`
- `scripts/relay_continuity_turns.py`
- `backend/tests/test_specialist_coordinator_profile_367.py` (new)

## Design constraints

- High risk (supervisor/relay orchestration code): requires full independent second-session review of the diff, CI green on exact head, and real-environment proof before enabling.
- Fail-closed: unknown `coordinator_profile` values are rejected by `validate_mission` and `coordinator_role_name`; if the specialist role has no matching model in the account catalog, `CatalogRefused("catalog_no_match")` is raised before any turn launches.
- Backward-compatible state loading: existing mission definitions without `coordinator_profile` default to `"standard"`.

## Acceptance criteria

1. `validate_mission` accepts `coordinator_profile` in `{"standard", "specialist"}` (defaulting to `"standard"` when absent) and rejects any other value.
2. `coordinator_role_name(agent, profile)` maps `"standard"` to `coordinator_{agent}`, `"specialist"` to the configured specialist role for agents that define one, and raises `Refused("profile_not_allowed")` for unknown profiles.
3. `RelayAdapter.start` resolves the specialist role when `coordinator_profile="specialist"` and `JARVISOS_ACCOUNT_MODEL_RESOLUTION=1`, pins the resolved exact model on resume, and fails closed with `CatalogRefused` if the account catalog lacks a matching specialist model.
4. Ordinary missions and roadmap cycles without `coordinator_profile` continue to resolve and run identically to spec 360.
5. Existing spec-360 (`test_account_model_resolution_360.py`) and continuity supervisor tests remain green.

## Required tests

- Offline tests in `backend/tests/test_specialist_coordinator_profile_367.py` covering criteria 1–4 with fake CLI catalogs and mission stores (no live provider calls).

## Definition of done

Test gate green (see `AGENTS.md`), full independent second-session diff review complete, real-environment dry-run proof recorded, CI green on exact head, STATUS row reconciled.
