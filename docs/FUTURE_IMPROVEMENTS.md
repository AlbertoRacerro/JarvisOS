# Future Improvements — advisory engineering parking lot

Status: advisory only; no implementation, queue, priority, dependency, readiness, merge, or roadmap authority.

`docs/specs/STATUS.md` is the sole live authority for JarvisOS work state and implementation authorization. An item in this file is not `planned`, `ready`, queued, prioritized, or approved for implementation. A parked item becomes executable only after the normal definition/specification/readiness/`STATUS.md` lifecycle grants that authority.

## Purpose

Preserve concrete, independently useful observations that are discovered during implementation, semantic review, or the bounded pre-merge quality check but are genuinely outside the accepted scope. This prevents useful evidence from being lost without contaminating the current slice or turning review into speculative refactoring.

This file is not a second backlog. Do not use it to sequence work, infer dependencies, reserve identifiers, justify implementation, or delay an otherwise valid merge.

## Pre-merge classification

For every observation that might affect the merge decision, classify it before acting:

1. **FIX** — a material correctness, security, maintainability, scope, or acceptance defect inside the current accepted correctness envelope. Repair the smallest causal issue now. A material defect may not be parked to bypass a merge gate.
2. **PARK** — a concrete, independently useful improvement that is genuinely outside the current accepted scope. Record it here if preserving it has clear future value; it does not block merge once current objective gates pass.
3. **DROP** — a vague idea, cosmetic preference, speculative refactor, elegance-only thought, or duplicate of live/parked work. Do not create registry noise.

After objective exact-head gates pass and no FIX item remains, the bounded semantic quality check should stop: PARK worthwhile out-of-scope improvements, DROP low-value ideas, and merge rather than chase perfection.

## Entry rules

Before adding an entry:

- deduplicate against this file and live `docs/specs/STATUS.md` / accepted specs;
- verify the observation against fresh enough repository evidence;
- do not assign priority, queue position, dependency semantics, readiness, or implementation authority;
- do not manufacture a dedicated PR solely to preserve a low-value note;
- prefer carrying a worthwhile entry only in an already-authorized bounded docs/reconciliation mutation.

Each entry must contain:

- stable short ID/title;
- origin: spec/PR/exact head or master context;
- concrete observation;
- why it is outside the accepted current scope;
- expected benefit or risk reduced;
- likely affected files/components when known;
- optional freshness invalidators.

## Entries

### F10 — calc_v0 artifact/proposal partial-finalization — PARK

- **Origin:** fresh master `23bee946c06307ef77087ce7f3212424033fa831`; accepted spec 043 / merged PR #52.
- **Observation:** `backend/app/modules/runner/service.py` intentionally registers and commits the validated `result.json` run artifact before calling the spec-040 batch facade that creates calc-origin Parameter proposals. If that later facade call fails, `_finish_failed(...)` marks the runner job and simulation run failed while the already-registered artifact remains queryable for that exact run.
- **Disposition:** PARK / non-blocking. This is the ordering explicitly frozen by spec 043: artifact registration precedes the memory-facade call, while the all-or-nothing guarantee applies to Parameter proposal creation. Fresh evidence does not prove a current accepted-requirement violation, current in-scope bypass, regression, or material P0/P1 correctness/security defect beyond that accepted ordering.
- **Expected benefit if revisited:** a future product requirement could choose stronger finalization/visibility semantics for failed calc runs, but that would be a new contract rather than a repair of accepted 043 behavior.
- **Likely surface:** `backend/app/modules/runner/service.py`, calc-run artifact visibility, and focused `backend/tests/test_python_runner_calc_v0.py` failure fixtures.
- **Freshness invalidators:** reopen only if a concrete consumer treats failed-run artifacts as successful/authoritative output, a new accepted contract requires transactional artifact+proposal finalization, or evidence shows stale artifacts can cross an existing authority/promotion boundary.

### F11 — retire the `tools/` Tool/ToolResult stub in favor of the 145 tool contracts — PARK

- **Origin:** spec 145 / FOUNDATION-CONTRACTS-1 implementation on base `b623fa863b0e58739a4664f7aadad0202f3f8126`.
- **Observation:** `backend/app/modules/tools/base.py` still defines an unused placeholder `Tool` protocol and `ToolResult(status, output)` dataclass (only referenced by `tools/registry.py`). 145 freezes the governed `StructuredToolCall`/`StructuredToolResult` in `backend/app/modules/ai/agent_contracts.py`.
- **Disposition:** PARK / non-blocking. The stub is not a wire contract and has no product consumer; replacing or deleting it belongs with the H-146 capability/tool broker that will actually register executable tools.
- **Expected benefit if revisited:** one tool-exchange vocabulary; removes a misleading second `ToolResult` shape before any tool is registered.
- **Likely surface:** `backend/app/modules/tools/`, the H-146 broker.
- **Freshness invalidators:** drop if H-146 deletes or rewrites `app/modules/tools/`.

### F12 — restart-durable or cross-process resource leases — PARK

- **Origin:** spec 145 / FOUNDATION-CONTRACTS-1 resource-lease decision on base `b623fa863b0e58739a4664f7aadad0202f3f8126`.
- **Observation:** 145 freezes `RuntimeResourceSnapshot`, `ResourceReservationRequest`, `ResourceLease` and the pure `grant_lease`/`end_lease` generation+version CAS state machine in `backend/app/modules/local_ai/resource_contracts.py`, with no SQLite table. Every local GPU dispatch currently happens inside the single backend process through `run_ai_task`; after a restart the arbiter re-observes runtime truth rather than trusting surviving rows.
- **Disposition:** PARK / non-blocking. No accepted requirement needs a lease to outlive the backend process, and no existing owner arbitrates local GPU use.
- **Expected benefit if revisited:** correct admission if L-147/H-146 evidence shows admission spanning independent processes (for example a solver or worker that allocates GPU/RAM outside the backend) or restart-surviving occupancy that runtime observation cannot see.
- **Likely surface:** a K-owned additive migration storing exactly the frozen lease fields (id, request/owner/correlation, resources, snapshot generation, state, version, granted/expires/ended timestamps, release reason), with `WHERE id=? AND state=? AND version=?` transitions and startup reconciliation against observed runtime state.
- **Freshness invalidators:** reopen only with concrete L/H evidence of cross-process GPU/RAM admission or unobservable restart-surviving occupancy.

### F13 — BLUECAD logical-design version history UX — PARK

- **Origin:** maintainer product direction, 2026-10-03; master `f46122db` after 171 reconciliation.
- **Observation:** iterative Jarvis edits should update one logical design in the left navigation, showing only its current/latest artifact there. The design needs `View history`, open/restore of earlier immutable revisions, automatic viewer switching to a newly generated revision, and a safe `Delete superseded versions` action that preserves the current and referenced revisions.
- **Disposition:** PARK; outside the accepted 168 mixed solve and 170 PBR unit contracts. No implementation authority is implied.
- **Expected benefit:** a usable revision workflow without clutter or accidental deletion of referenced designs.
- **Likely surface:** BLUECAD artifact/revision owner and workbench navigation/viewer.

### F14 — end-to-end cancellation of active AI requests — PARK

- **Origin:** maintainer product direction, 2026-10-03; master `f46122db` after 171 reconciliation.
- **Observation:** `Stop while generating` must abort local inference, terminate Relay where supported, and stop subsequent tool/action steps. It must preserve emitted transcript and provenance, report a truthful cancelled state, and prevent post-cancel workspace mutation unless an action crossed a recorded apply/commit boundary before cancellation.
- **Disposition:** PARK; outside the accepted 168 and 170 contracts. A frontend-only stop affordance would not satisfy this outcome.
- **Expected benefit:** real operator control over active model work and its downstream actions.
- **Likely surface:** Sidecar UI, AI job/execution spine, Relay/local provider adapters, action loop and apply ledger.

### F15 — Sidecar file and image ingestion after 180 — PARK

- **Origin:** maintainer product direction, 2026-10-03; master `f46122db` after 171 reconciliation.
- **Observation:** after 180, Sidecar should accept images, PDFs, papers, spreadsheets and documents for the current turn under policy and persist originals, digests, metadata, extracted representations, provenance, trust state and conversation links. Scientific papers belong in Literature/evidence; project files belong in workspace/project context. Upload never automatically verifies literature. The UI should show storage location and support inspect, reclassify, detach and delete.
- **Disposition:** PARK; outside the accepted 168 and 170 contracts, with no current implementation authority.
- **Expected benefit:** durable, inspectable context with explicit trust and ownership.
- **Likely surface:** Sidecar upload/context UI, files/knowledge/literature owners, conversation attachment records and policy enforcement.

### F16 — Studio contract edits after the scaffold — PARK

- **Origin:** spec 188 delivery, 2026-10-13; master `b1b32824`.
- **Observation:** the scaffold is generated once, at creation. When the operator later adds, renames or removes a contract variable, the `evaluate` signature and the `main` wrapper in the source are not updated, and the first sign is a failing Test. An explicit "regenerate signature" action or a contract-versus-signature diagnostic, both computed with `ast` and never executing the source, would close this.
- **Disposition:** PARK; outside the accepted 188 contract, with no current implementation authority.
- **Expected benefit:** contract edits stop failing late and for a non-obvious reason.
- **Likely surface:** `backend/app/modules/studio` (scaffold, derive), Studio Contract and Code tabs.

### F17 — Canonical Parameter edit from a separate frontend origin — PARK

- **Origin:** spec 188 browser proof, 2026-10-13; master `b1b32824`.
- **Observation:** backend CORS allows GET, POST, PUT and DELETE. The canonical Parameter edit uses PATCH, so it works when the backend serves the built frontend and fails preflight from a separate Vite dev origin. Spec 188 used PUT for its own draft save rather than widening the policy.
- **Disposition:** PARK; a CORS policy change needs its own accepted decision.
- **Expected benefit:** Engineering Data edits work in the development setup as they do when deployed.
- **Likely surface:** `backend/app/main.py` CORS configuration, or the Parameter edit route method.

### F18 — Acyclic mixed-solve summary wording — PARK

- **Origin:** spec 190 real-browser acceptance on repaired code HEAD `a4aa37013863ce2e50f09936294dc2ff70b16021`, 2026-10-14.
- **Observation:** an acyclic StudioUnit → Heater solve converged with closed balances, but its summary called it a “culture-only loop.” The wording misstates the graph shape; the calculation and detailed results were correct.
- **Disposition:** PARK; a label correction is outside the accepted 190 outcome and does not block its delivery.
- **Expected benefit:** operators can read the solve summary without inferring a recycle loop that is absent.
- **Likely surface:** Process mixed-solve result summary formatting.

### F19 — CFD field-slice plane label — PARK

- **Origin:** spec 229 operator discovery walk, 2026-10-28.
- **Observation:** every stored C1 CFD run returns its field slice with plane `symmetry_z` and normal `[0, 0, 1]`, but the slice points lie on y = 0 (spans 2.6 m in x, 0 in y, 0.023 m in z). The Design viewport followed the declared normal and drew a zero-height line. Spec 229 made the frontend project on the axis the points are flat along; no value changes.
- **Disposition:** PARK; correcting the backend plane metadata (or the patch the case samples) is a 216 backend change outside the 229 frontend scope.
- **Expected benefit:** other readers of the field payload get a plane label that matches the data.
- **Likely surface:** `backend/app/modules/bluecad/cfd_pipe/casegen.py` (`FIELD_SURFACE`, symmetry patch faces) and `field_export.py`.

### F20 — Cross-version candidate lineage after a Design fork — PARK

- **Origin:** spec 234 independent review of PR #10, 2026-11-04.
- **Observation:** a forked Design version inherits the parent version's attached BlueCAD candidate, which lives in the parent's data workspace. The fork's first feature-history regeneration therefore records `parent_candidate_id = NULL` instead of the inherited candidate, because the workspace lineage graph (`flowsheet/service.py`) only resolves edges inside one data workspace and would report a `dangling_reference`. The copied `design_component_feature_history` row still records the inherited candidate, and in-version lineage is complete.
- **Disposition:** PARK; a cross-workspace lineage edge needs a graph/authority decision outside the 234 scope.
- **Expected benefit:** candidate ancestry can be followed across Design versions.
- **Likely surface:** `backend/app/modules/workspaces/design_components.py` (`put_component_feature_history` parent selection) and the workspace graph builder in `backend/app/modules/flowsheet/service.py`.

### F21 — Slack answer confirmation durability and watch-state pruning — PARK

- **Origin:** spec 244 independent re-review of PR #41, 2026-11-05.
- **Observation:** the watcher records a Slack answer in the ledger before it queues a failed "Ricevuto" confirmation in its observation state, which is saved later in the watch loop; a crash in between loses that one confirmation (the answer itself is recorded). Answered questions also stay in `sent_questions` and `slack_cursors`, so the watch state grows by one small entry per question.
- **Disposition:** PARK; both are P2 with no authority or correctness effect on recorded answers.
- **Expected benefit:** a confirmation survives a watcher crash; the watch state stays bounded.
- **Likely surface:** `poll_slack_questions` in `scripts/relay_continuity.py` and `observe_step` in `scripts/relay_observe.py`.
