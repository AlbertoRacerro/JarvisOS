# 345 — JARVIS-CLOUD-IP-BOUNDARY-1: IP egress boundary for chat read tools on cloud routes

Status: in_review
Depends on: 344

## Goal

Enforce a fail-closed, server-owned IP egress boundary across the Jarvis chat read-only tools (`jarvis_repo_read`, `jarvis_knowledge_read`, `jarvis_retrieval_query`) whenever a chat turn runs on a cloud/external model route, while keeping local-model reads unrestricted and public JarvisOS reads unrestricted on all routes.

## Why

Directive `L20261120222744-75d9` (item 5, under umbrella Spec 059): Specs 341–344 give Jarvis chat full read-only access to dev state, public and private-pack repositories, Second Brain memory, literature, and SQL views. On a local model (`local:*`), Jarvis may read everything. When the operator selects a cloud/external model, private-pack and S2/S3/S4 workspace content must follow the existing accepted egress policy (umbrella 059, `docs/DECISIONS.md` ADR-059) instead of leaking raw private project material to external providers, while public JarvisOS content remains unrestricted. Directive `L20261123232512-a0c3` (item 6) binds this spec to implement that policy, not to restate it: raw S2/S3/unknown content never enters an external packet, and no operator setting or confirmation makes it eligible.

## Code facts checked at `cf44818e`

- `backend/app/modules/ai/sensitivity.py::deterministic_floor` computes deterministic sensitivity floors (`S0`–`S4`) including loaded pack IP patterns (`_pack_ip_patterns`).
- `backend/app/modules/ai/sensitivity.py::deterministic_floor_trigger` returns the floor level and triggering phrase for operator explanation.
- `backend/app/modules/ai/egress_policy.py::load_default_egress_policy` loads server-owned egress policy configuration from `configs/ai_egress_policy.json`.
- `backend/app/modules/ai/egress_sanitizer.py::create_prompt_derivative` persists provenance-bound S0/S1 derivatives.
- `backend/app/modules/ai/thread_service.py::_jarvis_system_envelope` advertises the active route boundary (`local` vs `external`) in the chat turn envelope.
- `backend/app/modules/ai/thread_service.py::_install_hermes_retrieval_grant` issues scoped read-only tool grants for Hermes turns.
- `backend/app/modules/agents/hermes/supervisor.py::dispatch_tool` validates capability grants and dispatches Hermes read-only tools.

## Scope

In scope:
- **Server-owned IP egress mode for chat read tools** (`no_cloud`, `sanitized`; there is no mode that returns raw S2/S3/unknown content to a cloud route):
  - Default mode is `no_cloud` (fail-closed safe default; never writable by models, tool arguments, or request-body flags).
  - Read from server-owned workspace/runtime egress settings (and surfaced as read-only metadata in `_jarvis_system_envelope`).
  - Route classification is strictly server-owned: local routes (`local:*` and Hermes turns backed by a local provider) bypass cloud egress restrictions and may read everything (still scrubbing S4 secrets in dev/repo outputs per Specs 341–342). Any route backed by an external/cloud provider enters the cloud IP boundary.
- **Public JarvisOS content unrestricted**:
  - `jarvis_repo_read(scope="public")` and public `jarvis_dev_read` ops remain allowed on both local and cloud routes (with existing secret/host-path scrubbing intact).
- **Private-pack and sensitive workspace content gating on cloud routes**:
  - `no_cloud` (default): `jarvis_repo_read(scope="pack")` is refused fail-closed (`egress_pack_blocked`) with zero pack bytes returned; `jarvis_knowledge_read` and `jarvis_retrieval_query` withhold any record/passage whose `sensitivity.py::deterministic_floor` is `S2`, `S3`, or `S4`, returning an explicit `withheld_count` and `egress_mode: "no_cloud"`.
  - `sanitized`: applies deterministic redaction of pack IP patterns and secrets, then re-checks `sensitivity.py::deterministic_floor`. Only results whose post-redaction floor is effective `S0` or `S1` are returned (marked `sanitized: true`); any result still floored at `S2`/`S3`/`S4` is withheld fail-closed (`egress_sanitization_required`) with zero raw content returned.
  - No `full` or other raw-passthrough mode exists (ADR-059: raw S2/S3/unknown never enters an external packet; human confirmation cannot make it eligible). An unknown or removed mode value, including `full`, resolves to `no_cloud`.
- **Audit & envelope visibility**:
  - Every cloud-route refusal or withholding records a safe metadata event through the existing `backend/app/modules/events/service.py::log_event` (tool name, op, egress mode, withheld count, reason code; never raw private content or secrets). No separate in-memory audit store.
  - `_jarvis_system_envelope` includes `Cloud IP mode: <no_cloud|sanitized>` when on an external route so the assistant explains withheld private-pack results honestly instead of hallucinating.

Out of scope (binding non-goals):
- Modifying merged Spec 059a sensitivity tables or reopening ADR-059.
- Adding new cloud providers, enabling paid AI, or spending budget (safe defaults remain paid AI disabled, budget zero, provider mode `fake`).
- Changing `route_class="auto"` (remains local-only).
- Frontend UI changes (handled in a separate UI slice if needed).

## Files likely touched

- `backend/app/modules/agents/hermes/ip_boundary.py` (new — small, pure server-owned gate helper)
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/agents/hermes/repo_read_tool.py` (from Spec 342)
- `backend/app/modules/agents/hermes/knowledge_read_tool.py` (from Spec 343)
- `backend/app/modules/ai/retrieval_query.py`
- `backend/app/modules/ai/thread_service.py`
- `backend/tests/test_jarvis_cloud_ip_boundary_345.py` (new)
- `docs/MODULES.md` (regenerated via `scripts/gen_modules.py`)

## Design constraints

- Strictly fail-closed: unknown/missing/malformed egress mode defaults to `no_cloud`; model arguments or HTTP request fields can never raise permission to `sanitized`.
- Implements ADR-059; it does not widen it. Deterministic redaction output is a candidate whose deterministic floor is re-checked; it cannot lower a floor by assertion, and anything still above S1 stays local.
- Raw `S2`/`S3`/unknown and `S4` / secret-bearing content is never returned to a cloud route under any mode.
- Core runs without a pack: tests use the fake pack in `backend/tests/fixtures/packs/zephyr` and run 100% offline with fake providers.
- High risk (egress and security boundary): requires coordinator full-diff review, independent second-session full-diff review, and green CI.

## Acceptance criteria

1. On a local route, `jarvis_repo_read(scope="pack")`, `jarvis_knowledge_read`, and `jarvis_retrieval_query` read all workspace and fake-pack content without cloud-egress refusal.
2. On a cloud/external route with default `no_cloud`, `jarvis_repo_read(scope="public")` succeeds, `jarvis_repo_read(scope="pack")` is refused with `egress_pack_blocked` and zero pack bytes, and `S2`/`S3`/`S4` knowledge/retrieval hits are withheld with `withheld_count > 0`.
3. On a cloud/external route with `sanitized`, deterministic redaction allows only post-check `S0`/`S1` outputs and withholds anything still floored above `S1` fail-closed.
4. A configured mode of `full` (or any unknown value) behaves exactly as `no_cloud`; no configuration returns raw S2/S3/S4 content to a cloud route.
5. Client/model arguments attempting to override egress mode are ignored or rejected; `_jarvis_system_envelope` accurately reports the active cloud IP mode.

## Required tests

- Offline unit and integration tests in `backend/tests/test_jarvis_cloud_ip_boundary_345.py` using the `zephyr` fake pack covering local vs cloud routes across both modes (`no_cloud`, `sanitized`), `full`/unknown resolving to `no_cloud`, public-repo unrestricted reads, S4 hard block under all modes, invalid/missing mode fail-closed to `no_cloud`, and refusal of caller-supplied mode overrides.
- Existing `test_hermes_runtime.py`, `test_ai_threads.py`, and sensitivity/egress suites remain green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator full-diff review + independent second-session full-diff review, spec status updated.
