# 095 — OPERATOR-WORKSTATION-AUTHORITY-1

Status: **definition / queue re-derivation authority**  
Date: 2026-08-17  
Depends on: 029, 035, 054, 071, 081, 083, 085, 088, 089, 090, 091

## 1. Purpose

Freeze the maintainer-approved operator-first workstation contract before any further frontend-beta implementation. This spec supersedes the previous assumption that the next runtime slice is the old standalone interpretation of 092. It does not itself authorize runtime implementation; it defines the acceptance authority from which the remaining queue is re-derived.

The product is an engineering workstation, not a database/debug viewer. Normal UI must expose physical meaning, effective values and units, active model choices, real blockers and operator actions. JarvisOS may retain opaque identity, lineage and audit precision behind progressive disclosure without making those implementation details the primary operator experience.

This is a later definition-only authority under the controlled re-derivation clause of spec 081. It preserves every already-merged frontend slice, the 066–068 and 080 freezes, the backend/provider/egress/promotion boundaries, and the separately removable visual-identity lane.

## 2. Information hierarchy

Three explicit levels apply across BLUECAD, Runs, Review, Engineering Data, Jarvis and Properties.

- **L0 OPERATE**: human engineering tag/name; physical meaning; effective value; declared unit; active model; required/optional state; blocker/warning; direct action.
- **L1 INSPECT**: semantic source/provenance; dependencies; formula/model; effective inputs; evidence and validity/domain information when authoritative.
- **L2 AUDIT**: UUIDs, digests, request/job IDs, exact ISO timestamps, raw JSON/log payloads, schema/policy internals and copyable machine identifiers. L2 is collapsed/secondary by default.

Human tags such as `R-101`, stream names, model names and run labels are primary. Opaque IDs must not dominate normal rows, cards or headers. Normal timestamps are human-readable; exact UTC remains available in Audit.

Semantic provenance labels are operator-facing: `User`, `Measured`, `CAD`, `Linked stream`, `Calculated`, `Scenario override`, `Previous successful run`, `Jarvis proposal`. Raw source-record identifiers remain Inspect/Audit.

## 3. Canonical desktop sidecar

There is one right sidecar. On normal desktop it is vertically split:

- **Jarvis above: approximately 40–45% of available sidecar height**, stable in normal operation;
- **Properties below: approximately 55–60%**, also bounded.

Both regions scroll internally and must not increase page height. Jarvis transcript scrolls while its compact header/thread control and composer remain reachable. Properties keeps an object header and internally scrolling property grid.

At effective 200% zoom or compact width where the split would create unusable panes, the same sidecar may degrade to `Jarvis | Properties` tabs. This is responsive degradation, not a second product architecture. No horizontal page overflow is permitted.

Jarvis contains AI interaction only: transcript, compact thread history/disclosure, composer, human-readable context chip, and reasoning/tool/agent controls only when real backend authority exists. Geometry dumps, lifecycle/debug status, UUIDs and engineering property tables do not belong in Jarvis.

## 4. Properties is an engineering model editor

Properties is object-centric and contract/model-driven, not a generic record viewer. Selecting a real engineering object resolves an engineering property contract and renders only relevant groups such as Geometry, Operating, Hydraulics, Thermal, Optical and Fouling.

A property row can expose: label, effective value, unit, semantic source, editability, dirty marker, blocker/warning, and derived/formula affordance.

`Empty` has semantics:

- optional empty: neutral and non-blocking;
- required empty for the active contract/model: blocker;
- fields belonging only to inactive models are normally hidden, not rendered as an enormous empty form.

Linked values may be visible contextually, but authority remains at the real source object/stream. The UI must not create a duplicate parameter store merely to populate Properties.

### 4.1 Model choice

Mutually incompatible alternatives such as `Constant pressure` and `Ergun equation` are one model selector, not competing assumption rows the operator must reconcile mentally. Exactly one option is active in normal UI. Selecting a model determines the visible input sub-contract. Values belonging to an inactive model are retained and restored if that model is reselected. Imported/legacy state with multiple incompatible active models is a blocker, not a normal state.

### 4.2 Derived values and formulas

Calculated values are read-only unless their authoritative contract says otherwise. An `fx` or equivalent affordance opens Inspect information containing the authoritative formula/model, effective inputs with units, result, and validity/domain evidence when available. Hashes/version IDs never substitute for an engineering formula. The frontend must not invent equations, validity ranges or evidence.

## 5. Working configuration authority

Four states are distinct:

1. canonical project data;
2. mutable **working configuration**;
3. immutable run snapshot created when execution actually starts;
4. run results/evidence.

UI edits and approved Jarvis actions mutate the working configuration, not canonical project records. Version 1 may keep the working configuration in memory; leaving before a run may lose the draft. There is no hidden autosave on every field.

A successful run becomes the next working baseline, but does **not** automatically rewrite/promote canonical Parameters, Assumptions or Decisions. Promotion to project authority is a separate explicit operation. A previous run may later be loaded as a working configuration without changing canonical project data.

The UI shows a quiet dirty state such as `4 unsaved changes · Baseline: Run 41`, with discreet per-field dirty indicators. Recent changes support Undo; the working configuration supports Revert all. After a successful run establishes a new baseline, dirty markers clear. After an execution failure, the working configuration remains available for correction.

The working configuration has a revision/precondition token sufficient to reject stale structured actions.

## 6. Run and deterministic preflight semantics

`Run` first invokes deterministic preview/preflight over the effective working configuration.

- Missing required input/DOF, incompatible units/model choice, stale required dependency or another authoritative non-ready state is a **pre-execution blocker**. It prevents execution and **does not create a simulation run record**.
- The blocker surface names the real issue and offers `Go to first issue`/equivalent navigation.
- Only after preflight is ready may execution start and an immutable snapshot of effective inputs, units and model choices be created.
- If solver/runtime execution then fails or does not converge, that is a real failed run and is persisted.
- If execution succeeds, the run is persisted and may become the new working baseline.
- No run automatically promotes working values into canonical project records.

This distinction prevents history pollution from repeated premature Run clicks while preserving genuine engineering failure evidence.

## 7. Quiet when valid

Do not permanently display rows of `DOF PASS`, `UNITS PASS`, `DEPENDENCIES PASS`, etc. When authoritative checks are satisfied, a compact `Ready`/`Valid` state is sufficient.

When a problem exists, make the real issue prominent at the affected field and in a bounded summary. Severity is never invented by the frontend:

- **Blocker**: execution cannot start;
- **Warning**: execution may proceed only if backend/domain authority allows it, with caveat;
- **Info**: no operator intervention required.

## 8. Engineering Data

Properties and Engineering Data are two projections of the same engineering authority:

- Properties = object-centric;
- Engineering Data = project-centric.

Engineering Data uses bounded groups for Parameters, Specifications, Assumptions, Constraints and Decisions when those record kinds are supported. The group heading supplies the type; rows do not repeat `ASSUMPTION`/`PARAMETER` on every line. A dense row leads with engineering identity and substance, for example `R-101: Catalyst bulk density = 1775 kg/m³ — User specified`, followed by actions.

Future lifecycle mutation authority must distinguish:

- `Active`: used by the current model/project state;
- `Inactive`: valid alternative not currently used;
- `Superseded`: historical value/model replaced by a newer one;
- `Archived`: legitimate historical information no longer operational;
- `Deleted`: error/duplicate removed from normal operator experience while retaining server-owned tombstone/audit when integrity requires it.

The operator action is labelled `Delete`; `Show deleted` belongs only in Advanced/Audit. Delete, Archive and Supersede are not synonyms.

## 9. Jarvis is an AI operator over the working configuration

Jarvis supports two output classes:

1. normal conversation;
2. structured decision/action requests with typed options and `Other`, rendered from structured backend data rather than parsing generated Markdown.

Deterministic validator/preflight authority owns blocker discovery. When blockers exist, Jarvis can proactively explain them in chat and ask whether the operator wants to edit manually or let Jarvis prepare a fix. A deterministic blocker notification remains available even if no AI provider can be called. AI recommendation uses the existing routing/budget/egress authority; frontend code never calls providers directly.

### 9.1 Mutation rules

A precise unambiguous operator command such as `set inlet pressure to 34 bar` is consent to mutate that field in the working configuration, subject to contract/unit/precondition validation and Undo. It is not consent to mutate canonical project data.

A generic request such as `fix the blockers`, an inferred recommendation, a model switch, or a multi-field change requires a structured preview showing `old → proposed`, unit, target, basis/source and reason before confirmation.

A value reused from a previous successful run is labelled with that provenance. A value generated/recommended by AI is labelled **`AI suggested — not validated`**, requires confirmation, and must never be presented as measured, validated or accepted merely because the model proposed it.

After a confirmed patch, Properties updates immediately from the same working-state authority and deterministic preflight reruns. Resolving blockers may offer `Run now`; execution does not start automatically unless the operator explicitly requested both modification and run.

### 9.2 Safe fixes

`Apply safe fixes` may cover only changes with a deterministic/credible basis such as a compatible canonical value or previous successful run. Jarvis must not invent arbitrary numbers merely to make all blockers disappear. Blockers without such a basis remain operator questions or explicitly confirmed AI suggestions.

### 9.3 Stale and atomic actions

Every structured change-set is bound to semantic target identity plus working-config revision/preconditions. If the operator changes the target/state after proposal generation, the stale action fails closed or is regenerated; it never silently overwrites newer work.

A multi-field patch presented as one action is atomic/fail-closed where practical: no silent partial apply. Free text is not converted into mutations by heuristic Markdown/text parsing. Undo/Revert operates on working state.

If the operator explicitly requests `restore the last successful values and rerun`, Jarvis may apply a validated patch, run deterministic preflight, and execute only if ready; otherwise it stops and reports the remaining blockers.

## 10. Immediate corrective UI requirements

Before deeper model/scene authority is expanded, the current workstation needs a bounded corrective slice:

- fixed Jarvis-over-Properties sidecar and internal scroll containment;
- Runs raw JSON/log/result/artifact areas use max-height/internal scroll plus `View raw`/`Expand`, so Analysis Dock is not pushed far below the work area;
- Review becomes a decision surface: proposal, human scope, current→proposed when real, authoritative difference/impact/evidence, actions; UUID/job/timestamp internals under Technical details;
- long tokens/digests cannot escape cards or cause page-level horizontal overflow;
- human-readable timestamps at L0/L1;
- keyboard/focus/reduced-motion/light/dark and effective-200% behavior remain preserved.

This corrective slice must not invent backend engineering contracts that do not yet exist.

## 11. 062 and Notes decisions

The normal Jarvis conversation surface must **not** add a permanent `Was this useful?` grading control under every response. Existing 062 backend/evaluation evidence remains valid; routine frontend grading is deferred and may later become a secondary Evaluation/Audit surface. It does not block the operator-first frontend queue.

An engineering Notes/scratchpad concept is approved as future direction but deliberately deferred from the first rework. A future Note is not a Parameter, Assumption or Decision; is not a calculation input; and is not automatically sent to AI providers. Inclusion in Jarvis context must be explicit.

## 12. Dense-editing compatibility

The first corrective/property slices do not need spreadsheet editing, but contracts must not preclude later Tab/Shift+Tab/focus navigation, copy/paste from Excel, bulk edit or `apply to selected similar objects`. These are operator-efficiency capabilities, not justification for duplicated engineering authority.

## 13. Re-derived ordered queue and canonical IDs

The remaining frontend-beta queue is re-derived in this exact order. This authority assigns the previously unnumbered new slices only now, after verifying that 095–098 are unused and that the registry already permits the existing `b` suffix form. Exact implementation authorization remains one slice at a time through definition/readiness/reconciliation discipline.

1. **096 OPERATOR-WORKBENCH-CORRECTION-0** — containment, Runs/Review corrections, fixed Jarvis/Properties sidecar. No new engineering backend semantics.
2. **071b ENGINEERING-PROPERTIES-1** — additive re-derivation of the already-merged 071 authority: contract-driven Properties, working configuration, dirty/undo/revert state and deterministic preflight/run-start semantics. The merged 071 row remains historical authority and is not reopened or relabelled.
3. **092 SCENE-BINDING-0, re-derived** — scene selection resolves stable real engineering object/property context; mesh/exporter order is not engineering identity.
4. **058c SCENE-SEMANTICS-A1, re-derived** — engineering object semantics, property groups, mutually exclusive model choice, formula/dependency Inspect semantics.
5. **097 JARVIS-ENGINEERING-ACTIONS-0** — only after working-state/property mutation authority exists; structured stale-safe actions, blocker assistance and safe fixes.
6. **098 ENGINEERING-RECORD-LIFECYCLE-0** — explicit server-owned mutation authority for Edit / Active-Inactive / Archive / Supersede / Delete and project-centric Engineering Data actions.
7. **006b PARAMETRIC-VARIANTS-1, re-derived** — variants as working configurations/run snapshots; load prior run without implicit project promotion.
8. **058b VARIANT-COMPARISON-1, re-derived** — unit-aware engineering comparison/history, not raw JSON/UUID comparison.

The old 092 wording remains historical/planned until its re-derived definition is merged. 062 remains blocked/deferred as its own evaluation surface but does not block these operator-workstation slices. Global visual-identity lane C stays separate and independently removable.

### 13.1 Dependency discipline

Spec 095 is definition-only and must not become a hard `Depends on` value for runtime rows because its registry status remains `planned`, exactly like 081. The downstream rows instead depend mechanically on the already-merged runtime foundations and, where needed, on preceding runtime slices in the ordered queue.

The planned mechanical shape to be verified again in each full definition is:

- 096: existing shell/Runs/Review/Jarvis surfaces only;
- 071b: merged 071 plus merged 096;
- 092: existing BLUECAD binding foundations plus merged 071b;
- 058c: merged 092 plus 071b and existing evidence/workbench authority;
- 097: merged 091 plus 071b/058c;
- 098: existing MemoryStore/Engineering Data/lineage authority plus 071b;
- 006b: 071b/092/058c plus existing BLUECAD shell/workbench;
- 058b: 006b plus existing analytics/workbench and 071b.

A downstream full definition may reduce a dependency proven unnecessary, but may not create a circular dependency back into 095 or silently reorder the queue.

## 14. Required completeness of each downstream spec

Each downstream definition/readiness package must state and test, as applicable:

- scope implemented now versus prepared/deferred behavior;
- non-goals;
- backend/frontend authority and state ownership;
- data contract and semantic provenance;
- mutation and confirmation rules;
- failure, stale/race and partial-failure behavior;
- responsive layout, local scroll containment and page overflow;
- keyboard/focus/accessibility/reduced-motion/effective-200% behavior;
- migration/legacy-state handling;
- rollback/removability;
- deterministic checker and real browser acceptance;
- downstream seam.

No decision in this spec may survive only as automation/chat context; the package of downstream canonical specs must cover it explicitly before the corresponding runtime behavior is implemented.

## 15. Visual boundary

Functional slices preserve the approved engineering-workstation direction: desktop-first, viewport-dominant, compact technical chrome, light/off-white surfaces, natural leaf-green accents, fine separators and minimal shadow. They must not smuggle a global identity redesign into functional work. Lane C remains separately reviewable/removable.

## 16. Definition acceptance

This authority is complete when:

1. 029 is reconciled as merged in STATUS;
2. STATUS records 095 as definition-only and replaces the old 092-next path with the eight canonical downstream IDs above;
3. 071 remains merged while 071b owns the new additive Properties/working-state implementation, avoiding false reopening of historical implementation evidence;
4. downstream implementation remains unauthorized until the next slice receives its own complete definition/readiness authority;
5. all maintainer-confirmed 2026-08-17 decisions listed here are represented as testable contracts or explicit deferrals;
6. 062 is recorded as frontend-grading deferred/non-blocking for this operator queue, without altering its backend evidence;
7. 066–068 and 080 remain frozen and no global visual-identity work is absorbed.
