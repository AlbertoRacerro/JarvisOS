# 122 — JARVIS-DEVELOPMENT-ACTIONS-1

Status: planning/readiness contract; implementation authority begins only after canonical `STATUS.md` is truthfully `ready`.

Exact derivation base: `74594515e6ece89dba78923230f263bc6091d9f6`.

## Objective

Activate the accepted 111 Jarvis context/action foundation for Development using the canonical 116 Roadmap/Calendar and 117 Brainstorm owners. The operator can explicitly assemble multi-record Development context and ask Jarvis for bounded scheduling, reconciliation, Roadmap and promotion proposals without granting Jarvis direct domain `COMMIT` or `EXECUTE` authority.

122 is a thin adapter/action slice. It creates no second Development store, context store, proposal queue, scheduler, workflow engine, or orchestration runtime.

## Existing owners

122 reuses and does not replace:

- 111 for exact refs, explicit context basket/preview/digest/source manifest, stale refusal and route-scoped `CONTEXT`/`PROPOSE` capability contracts;
- 116 for canonical Roadmap work items, Calendar allocations, dependencies, lifecycle/CAS and accepted manual Development mutations;
- 117 for immutable RAW Brainstorm capture, reconciled revisions/lineage and proposal-only Roadmap/Design/Coding promotions;
- existing Jarvis AI execution policy for any semantic proposal generation.

Canonical Development routes remain the existing `/development/roadmap`, `/development/roadmap/calendar`, and `/development/brainstorm` surfaces. Browsing/opening/listing remains context-neutral. Only an explicit operator add/select action changes Jarvis context.

## Accepted capability boundary

122 may register only Development-scoped `CONTEXT` and `PROPOSE` actions.

### CONTEXT

Explicitly add one or more exact current Development refs to the existing 111 basket. Minimum supported refs are:

- Roadmap work item identity + current server revision;
- Calendar allocation identity + current server revision;
- Brainstorm RAW identity/state;
- Brainstorm reconciled idea + exact immutable revision;
- Brainstorm promotion proposal identity + exact source revision/state when useful.

The adapter returns only bounded serializable owner-projected content/provenance. Unknown, cross-workspace, stale, deleted, superseded-as-current, or conflicting identities fail closed. Multi-record context preserves deterministic ordering/deduplication and the existing 111 budget/digest behavior.

### PROPOSE

A proposal is ephemeral advisory output grounded in an inspected exact context digest/source manifest. Closed proposal kinds are:

1. `schedule` — propose Calendar allocations or time-window adjustments linked to existing Roadmap work; never create/move an allocation directly.
2. `roadmap_change` — propose bounded Roadmap create/edit/dependency/lifecycle changes; 116 owns acceptance and deterministic done/dependency/CAS checks.
3. `brainstorm_reconciliation` — propose synthesis/reconciliation/supersession intent over exact 117 sources; 117 owns acceptance and lineage/CAS.
4. `promotion` — propose a 117-style downstream Roadmap/Design/Coding promotion/handoff grounded in an exact Brainstorm revision; downstream acceptance remains with the target owner.

No proposal response is an actuator token. 122 registers no `COMMIT` or `EXECUTE` capability and exposes no endpoint that applies a proposal.

## Exactness and stale safety

For every context/proposal request:

- `workspace_id` must match the active workspace;
- owner/kind/id and owner-required revision/version identity must resolve to the same canonical object;
- inspected context carries the 111 deterministic digest/source manifest;
- immediately before semantic proposal generation or returning a current proposal, all refs are re-resolved and the expected context digest is revalidated;
- any moved/deleted/stale/conflicting ref returns a typed stale/unavailable result and no current proposal;
- no retry loop chases moving truth;
- opening or browsing a Development object never implicitly adds it to context.

## Proposal response contract

A successful response contains at minimum:

- `state = proposed`;
- workspace and originating Development route;
- closed `proposal_kind`;
- bounded operator intent;
- ordered exact refs actually used;
- inspected `context_digest` and source/provenance manifest;
- concise summary and bounded proposed operations;
- assumptions/warnings plus the authoritative owner/action required for acceptance;
- deterministic/template or normal Jarvis AI-task provenance without secrets/hidden prompts.

122 adds no durable proposal table. Existing 117 promotion records remain 117-owned and are not duplicated. If an operator accepts a proposal, the owning domain independently re-resolves current truth and applies its existing CAS/validation contract.

## AI / egress boundary

Context resolution and deterministic preview require no provider call. Semantic proposal generation, when useful, may run only through the existing product AI execution spine and current routing/egress/sensitivity/accounting/budget controls. `route_class="auto"` remains non-external. Model output is advisory and must validate against a closed bounded schema before presentation.

No direct provider binding, new credential, arbitrary URL fetch, filesystem read, Git/repository mutation, shell/PTY, background scheduler, or Hermes runtime is authorized.

## Minimum implementation shape

Fresh master supports a thin implementation over existing owners:

- Development exact-ref adapter(s) registered through the existing 111 production context registry;
- route-scoped `CONTEXT`/`PROPOSE` capability descriptors for Roadmap/Calendar/Brainstorm;
- one bounded proposal service/route that consumes inspected 111 context and revalidates refs/digest before returning;
- existing Jarvis sidecar/context affordance wired to Development routes without changing browsing semantics;
- deterministic backend/frontend contract tests;
- a declarative trusted Chromium proof plan using the existing generic browser-proof toolbox.

Implementation method/file placement remains coordinator-owned provided these boundaries hold.

## Acceptance matrix

Implementation must prove at least:

1. Development routes advertise only the accepted 122 `CONTEXT`/`PROPOSE` capability set;
2. explicit multi-record Roadmap + Calendar + Brainstorm refs produce one deterministic inspected 111 context preview/digest/source manifest;
3. ordinary browse/open/list actions leave Jarvis context unchanged;
4. workspace mismatch, unknown kind/id, stale Roadmap/Calendar revision, deleted target, superseded Brainstorm current identity, or conflicting exact identity fails closed;
5. duplicate refs preserve deterministic 111 dedup/order/budget behavior;
6. schedule proposals cannot create/move Calendar allocations and clearly name 116 as acceptance owner;
7. Roadmap proposals cannot bypass dependency/done/CAS rules and expose no apply actuator;
8. Brainstorm reconciliation proposals cannot rewrite RAW content or bypass 117 lineage/CAS;
9. promotion proposals remain proposal/handoff only and cannot directly mutate Roadmap, Design, Coding or repository state;
10. semantic generation, when used, goes only through the normal Jarvis AI execution spine and closed output schema;
11. malformed/oversized/out-of-schema model output is refused without secret/raw-provider leakage;
12. no `COMMIT` or `EXECUTE` capability is registered by 122;
13. removing 122 leaves 111/116/117 stores and canonical behavior unchanged;
14. exact-head trusted Chromium proof at the accepted desktop viewport demonstrates explicit multi-record add-to-context, inspected source/digest, at least one proposal, visible domain-owned acceptance boundary, and no implicit context/domain mutation during browsing.

## Non-goals

- direct Roadmap/Calendar/Brainstorm/Design/Coding `COMMIT` or `EXECUTE`;
- automatic scheduling/rescheduling or background jobs;
- a second context basket, Development database, proposal ledger, queue or workflow engine;
- semantic/vector retrieval or broad project search expansion;
- new provider/credential/budget/egress/filesystem/Git/runner/desktop authority;
- Hermes runtime or DEV integration;
- redesign of Development surfaces unrelated to explicit Jarvis context/proposal affordances.

## Readiness decision — 2026-09-15

Readiness was derived from exact fresh master `74594515e6ece89dba78923230f263bc6091d9f6`.

Fresh evidence confirms:

- canonical `STATUS.md` has 122 `planned` with hard dependencies 111, 116 and 117, all now `merged`;
- no open planning/readiness/implementation PR for 122 exists, so there is no active lane to recover;
- 111 already owns the exact-ref/context/capability mechanism and rejects `COMMIT`/`EXECUTE`;
- 116 already owns durable Roadmap/Calendar truth, revisions/CAS, lifecycle/dependency/time semantics and manual acceptance;
- 117 already owns Brainstorm RAW/revision/lineage truth and promotion proposal identity;
- current Development routes are already canonical, so 122 needs activation/adapters rather than a second route/store;
- no destructive migration, new credential, spend, repository permission or security-boundary choice is required by this slice;
- the generic declarative browser-proof toolbox already exists and 117 proves Development-route use without spec-coded privileged execution.

### Failure-mode disposition

- implicit context from browsing: closed by explicit-add-only contract and browser proof;
- stale multi-record context: closed by exact owner revisions, pre-proposal re-resolution and digest revalidation;
- proposal laundering domain writes: closed by `CONTEXT`/`PROPOSE` only and owner-named acceptance;
- second Development/context/proposal store: explicitly prohibited and unnecessary;
- provider/egress bypass: closed by existing Jarvis AI execution spine only;
- automatic scheduler/workflow creep: not needed for proposal-only outcome and PARKED;
- Hermes coupling: outside this phase and non-goal.

### Minimum-necessary test

Criterion: make accepted Roadmap/Calendar/Brainstorm truth explicitly usable together by Jarvis for Development context and bounded proposal-only assistance.

- Is 122 necessary? **Yes.** 111 provides the generic mechanism while 116/117 deliberately leave Development Jarvis activation to 122.
- Can the criterion be met without new stores/queues/schedulers/provider authority? **Yes.** Thin adapters/actions/UI affordance over existing owners are sufficient.
- Concrete risk justifying the new surface: without a bounded common adapter/action slice, Development assistance would either remain unavailable or drift into page-specific implicit context/direct-write shortcuts.

No unresolved readiness blocker remains. The planning/readiness change may therefore atomically transition canonical 122 from `planned` to `ready`; implementation begins only after that transition is merged on `master`.
