# 253 — Simulation results and requirement verification

Status: ready (Q1 execution complete; final host receipt `L20261106232818-a475` verified the default model was
restored. The batch Slack line remains a later slice subject to maintainer question `L20261106172945-c054`.)
Depends on: none (the approved [248](248-design-value-path.md) contract binds through Authority; 248 is an umbrella
that stays `planned` until its slices land, so it is not a hard implementation dependency)

## Goal

Complete slice A of the approved design-value path: automatically ingest eligible simulation results, validate and
place each value, surface exceptions in one batch notification, and use the values in requirement verification.
Expose the current applicability-set digest on the existing revalidation read. Risk: high (additive migration); the
Slack line of the batch notification is split out because the product has no Slack path today (see open question).

## Authority

Maintainer directive `L20261105222120-78af` item 5 and answer `L20261106093156-1712` approve the binding contract in
[248](248-design-value-path.md). Q1 execution is complete and the final host receipt verified that the default model
was restored. This spec is reviewed and ready; it has no unmerged hard dependency. The batch Slack line remains a later slice
subject to maintainer question `L20261106172945-c054`.

## Why

Simulations should feed the same deterministic engineering-value path as later sources without per-value approval.
Exceptions remain visible and actionable, without silently overwriting a maintainer's design decisions.

## Scope

- One simulation run's eligible result payload is one ingestion batch. Extract values automatically after a run finishes
  with validated results; validate once per source value and version, then place passing and flagged values.
- Show a red dot and failed checks for exceptions; one Jarvis batch notification for a batch with
  exceptions, with navigation to each flagged value. Correct or confirm an exception in one action.
- First supported source (coordinator decision): `calc_v0` runs, whose outputs are validated by
  `_validate_calc_v0_output` in both the runner and Studio completion paths. Other runner families stay unsupported
  until they have an explicit output contract.
- Propagate unvalidated-value indication to calculations and requirement checks; support pass, fail and not evaluable
  with reasons and conditions. Expose `applicability_set_digest` read-only on `RevalidationRead`.

## Non-goals

- Slices B–H: version comparison/history UI, pre-run coherence and degrees-of-freedom checks, literature ingestion,
  property libraries, Docling, PaperQA2 and online source ingestion. No new provenance or workflow framework.
- Slack delivery of the batch line: a later slice after the maintainer answers the open question. This slice keeps the
  batch notification durable and readable so that slice adds only delivery.
- Per-value approval of passing values, a snapshot step, changes to reconcile semantics, automatic changes to Project
  Basis parameters, provider calls, paid services or new unrestricted egress. Preserve existing validation outcomes
  `no_material_effect` and `recomputation_required` where applicable rather than relabeling them.

## Design

### Existing anchors and gaps

- `backend/app/core/schema.py` owns `simulation_runs` with `status`, `input_payload`, `parameter_payload` and
  `output_payload`. `backend/app/modules/runner/service.py` `run_runner_job` validates supported outputs, then writes
  `succeeded` and the payload; `_validate_calc_v0_output` is the explicit output validator on both the ordinary
  `calc_v0` runner completion path and the Studio calculation completion path. It requires named outputs with finite
  numeric values and non-empty units. A general persisted “validated results” status is not present on
  `simulation_runs`: success alone is not proof that every output is eligible. For this slice, only outputs from those
  completion paths that pass this validator are candidates for ingestion; unsupported, failed, unvalidated and other
  successful runs must not silently ingest. Ingestion still applies its own scalar, conditions and exception checks,
  so passing this runner validator does not make every output eligible.
- `backend/app/core/project_knowledge_schema.py` owns `simulation_run_scalar_results`, uniquely keyed by run and output,
  with value, unit, payload digest and extractor identity/version. `backend/app/modules/project_knowledge/service.py`
  `_extract_scalar_from_run_payload` and `admit_scalar_result` rederive numeric values and verify the digest; the
  existing `POST /project-knowledge/scalars` in `routes.py` is manual and rejects unprovable evidence. Reuse its
  deterministic extraction, not its rejection-only behavior as the exception store. Current storage has no exception
  state, conditions, failed-check details or resolution history; a new ingestion path is required.
- `requirements` in `backend/app/core/schema.py` and the project-knowledge migrations, plus
  `backend/app/modules/modeling/project_knowledge_owner.py`, own Project Basis requirements. The existing
  `POST /project-knowledge/validation` calls `evaluate_requirement` and stores `project_knowledge_validation`; it
  checks working-revision basis and applicability digests and has more outcomes than the three numeric verdicts.
  Reuse that owner and its binding checks; extend it for admitted values and conditions without bypassing revision,
  freshness or criterion checks. Missing evidence or conditions outside the value's domain is `not_evaluable` with a
  reason, not a pass. A flagged value may yield a numerical verdict only with an explicit unvalidated marker.
- `revalidation_status_from_connection` already computes the applicability digest through
  `_applicable_required_requirements`; return that same digest in `RevalidationRead` from
  `GET /project-knowledge/workspaces/{workspace_id}/revisions/{revision_id}/revalidation`. Do not accept a new write
  authority or confuse it with `selected_validation_set_digest`.
- Existing graph and freshness machinery includes `backend/app/modules/flowsheet/freshness.py`
  `prepare_freshness_invalidation` and `persist_freshness_invalidation`, and `freshness_invalidations` and
  `freshness_marks` in `backend/app/core/schema.py`. Reuse applicable lineage and invalidation paths when a resolution
  changes evidence; do not claim they already model scalar versions or build slice B's comparison UI.
- `frontend/src/components/fusion/ProjectKnowledgePanel.tsx` displays revalidation and hands recomputation to Process;
  it is an existing entry point, not an existing red-dot or batch notification surface. No product notification or Slack
  digest path was found under `backend/app` or `frontend/src`. The operational `scripts/relay_observe.py` `digest_text`
  and `scripts/relay_continuity.py` `slack_post` serve the host relay, not an established product notification API.
  Do not treat them as one or bypass egress controls; a minimal authorized product delivery integration is a gap.

### Data flow and boundaries

1. At a proven validated completion, extract a deterministic batch from the persisted run payload, with its digest,
   output identity, extractor id/version and known conditions. Never mutate source payload or basis parameters. Persist
   a stable batch/value identity so repeated completion, restart and retry cannot duplicate admission or notices.
2. For each candidate validate source presence and digest, quantity/unit consistency, plausible range and
   duplicates/contradictions against the same quantity and conditions, including disagreement with an existing basis
   decision. Record each check and reason. Missing trustworthy quantity metadata, bounds or conditions is not a pass;
   keep an explicit exception rather than inventing a range or conversion. Duplicate and contradiction rules must be
   deterministic; do not overwrite either source or decision. A passing value is usable immediately. A failure is
   still indexed and visible, even if it cannot be admitted by the present strict scalar endpoint.
3. Retain a durable batch-to-value association and one in-app notification for a batch containing exceptions; each
   exception has an exact workspace-scoped target and button. Repeated completion or restart never creates a second
   notice for the same batch. No Slack or other egress in this slice.
4. Correct or confirm is one explicit action on one exception, recorded with identity and reason; correction must not
   rewrite the run. Revalidate a corrected value deterministically, preserve its provenance, and invalidate dependent
   evidence through the existing graph where connected. Confirmation is an explicit human exception decision, not a
   fabricated validation pass; downstream results retain an accurate indication of its status.
5. Calculations and requirement checks consuming a flagged value carry an unvalidated-value indication through their
   result and read surfaces. A source value cannot be used outside its stated conditions: requirement verdict is
   `not_evaluable` with reason. Avoid recalculating the one-time ingestion validation on ordinary reads or uses.

### Schema and API changes

An additive migration is needed: extend the project-knowledge-owned persistence (or add its smallest necessary tables)
for per-value conditions, checks, exception/resolution records, batch identity and notification state; existing
scalar rows and their uniqueness must remain valid. Existing scalar constraints alone cannot represent all failures or
multiple corrected versions. Final field/table names are implementation choices, not existing code. Follow
`docs/MIGRATIONS.md`: fresh and upgrade shapes, unique migration record, migration ID and discipline baseline/digest,
idempotent backfill where needed; do not change startup order. Persist any result marker necessary to prevent it being
lost on reread. Reuse existing routes where their contracts fit; this slice requires a minimal new workspace-scoped
batch/exception read and a resolution mutation, plus whatever notification read is needed by the chosen UI. Their names
do not exist yet. The only specified existing response addition is read-only `applicability_set_digest` on
`RevalidationRead`; regenerate frontend contracts rather than editing generated files. No endpoint may bypass workspace
ownership or user authority.

## Files likely touched

Implementation is expected to inspect the existing owners named above, their routes and models, frontend Project
Knowledge/Process surfaces, and their tests. New persistence, delivery and UI files/routes are gaps, not asserted
existing paths. Update module manifests and generated artifacts if implementation requires them.

## Acceptance criteria

All criteria are verifiable offline with isolated data and fake providers/transports.

1. A proven eligible run completion automatically ingests its batch exactly once across repeat completion and restart;
   failed, unsupported or unvalidated runs do not silently ingest. Source payload remains unchanged.
2. Every extracted value records source digest, output identity, extractor identity/version, value, unit and known
   conditions. Source mismatch, incompatible unit, implausibility, duplicate and contradiction each produce a visible
   specific exception; unavailable evidence never becomes a fabricated pass.
3. Passing values are usable immediately without a click. Every failed value remains indexed at its proper place with
   a red dot and the failed checks; a conflicting Project Basis decision remains unchanged.
4. A batch with multiple exceptions creates one Jarvis notification, not one per value; each value can be opened at
   its exact location. Repeated completion and restart create no duplicate notice.
5. One action corrects or confirms each exception; correction preserves the original source and is checked again;
   confirmation records the human decision without falsely claiming an automatic pass. Dependent evidence becomes
   stale where linked. Passing values never require confirmation.
6. A calculation and a requirement check using a flagged value visibly identify that dependency; a check returns pass,
   fail or not evaluable with a reason as appropriate, including not evaluable outside stated conditions. Existing
   revision-binding, additional lifecycle outcomes and stale-evidence protections remain intact.
7. Revalidation returns the exact current computed `applicability_set_digest`, distinct from the selected validation
   set digest, and changes with the applicable set; it is not a writable authority. Existing callers remain compatible.
8. A fresh and upgraded isolated SQLite database have equivalent additive schema, and migration initialization is
   idempotent; no live data, paid provider, network or external Slack is required for tests.

## Tests

- Add `backend/tests/test_sim_results_requirement_verification_253.py` for completion eligibility, extraction,
  validation, conditions, duplicates, contradictions, retries, resolution and verdict/marker propagation.
- Extend `backend/tests/test_project_knowledge_core_112.py` and
  `backend/tests/test_migration_discipline.py` as needed for digest compatibility and additive fresh/upgrade shape;
  use fake providers. Add focused notification tests alongside the implementation.
- Add `frontend/tests/253-sim-results-requirement-verification.mjs` to the frontend build test chain for red dots,
  exact-target buttons, batch notification, resolution, markers and requirement verdicts. Regenerate contracts.

## Browser walk

Add an `e2e/` walk using isolated fake data and fake delivery. From a clean browser profile at `/`, use visible controls
to reach a completed eligible run, observe passing and multiple red-dot values, open each from the single batch
notification, correct one and confirm another, and observe pass, fail and not-evaluable requirement verdicts and the
unvalidated marker. Capture the evidence directory. Follow `e2e/README.md`; never use
the live data root or port 8000.

## Risks

- Result validation differs by runner and output format; marking all successful runs validated would promote unproven
  data. Missing unit, range and condition metadata must fail safely.
- Correcting values and stale dependencies may require additive schema changes; preserve the basis and migration
  discipline. The migration makes this high risk: exact-head CI, a severe review, a full diff review by a second
  qualified session and the migration proven on a copy of a real data root (`AGENTS.md`).

## Open questions for the maintainer

1. How does the one Slack line per batch reach the maintainer? Recommended: the host relay, which already owns the
   maintainer's Slack channel, posts one line per new batch notification read from the product, with a link and no
   values; the product gains no egress. Alternative: a product-owned Slack sender (new egress, high risk, after Q5).
   Only the delivery slice waits for this answer.

## Definition of done

All acceptance criteria, offline tests, migration gates and the browser walk pass; required risk review and exact-head
CI evidence exist, and the spec registry is updated under the lifecycle rules.
