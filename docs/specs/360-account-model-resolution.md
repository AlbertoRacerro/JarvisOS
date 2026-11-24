# 360 — ACCOUNT-MODEL-RESOLUTION-1: Resolve coordinator and worker models from the local account catalog

Status: ready
Depends on: none

## Goal

Coordinator and worker launch paths select the newest available numeric major/minor release within their configured model family and role, using a bounded local CLI/account catalog read before launch. An unreadable or malformed catalog never authorizes an invented model ID. The role, effort tier, quota pool, and sandbox authority remain unchanged by model selection.

## Why

Directive `L20261124085849-d342` asks to stop pinning versioned model IDs in coordinator and worker routes so an account's newly available release can be used without a code change. The resolver must preserve the existing allowlist boundary and independent review evidence when the catalog changes.

## Code facts checked at `cfff63bd`

- `scripts/relay_argon.py::model_for` selects native coordinator IDs from fixed versioned sets, and `scripts/relay_argon.py::run` passes the selected ID to the native CLI.
- `scripts/relay_continuity_adapters.py::RelayAdapter.start` launches Relay coordinators without a `--model` argument; the installed Relay CLI supports that argument.
- `scripts/agy_worker.py::model_ids` and `scripts/agy_worker.py::resolve_model` map worker aliases to fixed versioned IDs before launch.
- `scripts/worker_routing.py::MODELS`, `scripts/worker_routing.py::bare_command`, and `scripts/worker_routing.py::launch_command` select and emit worker launch commands.
- `scripts/relay_continuity_turns.py::classify_argon_turn` checks native turn metadata and init events against fixed versioned sets.

## Scope

In scope:

- A small shared, offline-testable resolver reads the local account's available model IDs without a model turn, validates a bounded response, matches only configured family and role patterns, and selects the greatest numeric `(major, minor)` release at the requested effort. Stable release wins over a preview at the same version unless the role explicitly allows previews.
- Wire the resolver through the native coordinator launcher, Relay coordinator `--model` override, worker router, worker preflight, and native turn evidence validator. The selected exact ID is recorded with each launch so later catalog changes do not rewrite historical evidence or resume a different model silently.
- Keep an explicit role-to-family mapping in configuration and test it. Effort (`low`, `medium`, `high`, etc.) is part of the role request and cannot be silently upgraded or downgraded. A model version never changes a worker's capability tier or quota pool.
- If a catalog is missing, stale, malformed, or does not contain a matching family/effort, refuse that route with a typed reason and let existing provider fallback choose another authorized route. Reuse a previously verified ID only while its bounded cache is fresh for the same account and CLI; never use an unverified hardcoded version as fallback.

Out of scope (binding non-goals):

- Enabling a dedicated specialist coordinator profile; that is a separate slice after this resolver.
- Changing sandbox, credential, review, quota, or routing authority; benchmark-based capability ranks; adding providers or spending.
- Probing models with a paid or content-generating turn.

## Files likely touched

- `scripts/relay_argon.py`
- `scripts/relay_continuity_adapters.py`
- `scripts/relay_continuity_turns.py`
- `scripts/worker_routing.py`
- `scripts/agy_worker.py`
- A shared resolver under `scripts/` and corresponding offline tests in `backend/tests/`.

## Design constraints

- Catalog reads are local CLI/account metadata only, bounded in time and bytes, and do not echo credentials or raw catalog errors into public logs.
- Exact model IDs reaching any CLI must have been present in a successful current or fresh cached catalog for that account. Unknown family, suffix, malformed version, and cross-role model IDs are refused.
- An explicit per-run model override is accepted only when it also appears in the matching catalog and role. Resume uses the recorded exact model ID if still authorized; otherwise it pauses with a typed refusal.
- No permanent model/vendor ranking is added to `AGENTS.md` or process prose. Review and capability gates remain role based.

## Acceptance criteria

1. Synthetic catalogs with old and new major/minor IDs choose the newest matching release for each role and requested effort; previews, aliases, lookalike family names, and nonnumeric versions cannot outrank an eligible stable release.
2. All coordinator and worker launch paths use one resolved exact ID and native evidence validation accepts that recorded ID only. A changed catalog between selection and launch cannot silently substitute another ID.
3. Missing, malformed, stale, cross-account, or no-match catalogs refuse before a model turn. Existing provider fallback can continue where another authorized route has quota.
4. Existing quota, sandbox, credential stripping, review tier, and capability tests pass. No launch authority or unsafe default changes.

## Required tests

Offline fixture tests for parsing, ordering, family/effort isolation, cache scope/expiry, catalog failure, override and resume validation, worker launch consistency, and native turn classification. Focused Relay and worker tests, Ruff, architecture enforcement, and exact-head CI. One live no-model-turn catalog probe and one isolated launch/metadata proof precede enabling the new resolver in the supervisor.

## Definition of done

All criteria and high-risk review gates pass on the exact head; the live proof confirms one selection on the installed account; STATUS is reconciled. The supervisor keeps its previous route until the proof is complete.
