# Final operator interaction contract — 2026-08-27

Status: maintainer-approved product/interaction preservation contract; **not** runtime implementation authority and **not** a parallel queue.

## 1. Purpose

This contract freezes the user-facing behavior approved during the final JarvisOS operator-workstation design session so that future builders do not reconstruct product semantics from chat memory, screenshots, generic UX conventions, or current backend limitations.

The canonical HTML files remain the normative **visual/composition** targets. This file defines the matching **interaction/state-transition** intent. `docs/spec-drafts/FINAL_OPERATOR_CAPABILITY_MATRIX_2026-08-27.md` (not carried into this repository) and `docs/spec-drafts/FINAL_VISUAL_IMPLEMENTATION_PACK_2026-08-27.md` (not carried into this repository) define the capability-preservation and pseudo-spec decomposition used by future authority re-derivation.

No builder may use this document to implement runtime work unless live `docs/specs/STATUS.md`, a canonical full spec, and readiness authority permit that work.

## 2. Authority and precedence

For user-facing composition and interaction, resolve conflicts in this order:

1. explicit later maintainer-approved cross-surface decisions in `APPROVED_OPERATOR_UI_MANIFEST_2026-08-27.md`;
2. the canonical HTML for the selected surface at its reference viewport;
3. this interaction contract;
4. the most-specific approved surface reference document under `docs/design-references/`;
5. PD-08 and the relevant PD-03/PD-04/PD-05/PD-07 product contracts;
6. earlier product-direction material only where not superseded above.

For **implementation permission and runtime ownership**, the above files never outrank `docs/specs/STATUS.md`, the active canonical specification/readiness record, accepted ADR/architecture authority, or exact current code.

Screenshots are evidence only. If a screenshot/rendering error conflicts with the canonical HTML, the canonical HTML wins.

## 3. Builder interpretation rule

A production implementation may translate prototype HTML/CSS/JS into React/components and replace fixture data with truthful backend state. It may not redesign the approved product because:

- a component library offers a different default;
- the current backend lacks a capability;
- an old route/store already exists;
- a different dashboard/card/Kanban/IDE pattern appears easier;
- a builder considers another layout cleaner;
- an AI reviewer suggests a different information architecture without maintainer approval.

When backend authority is missing, the capability is **pending implementation**, not deleted. A staged frontend may expose the approved affordance only as a clearly disabled/unavailable/future control when the active spec explicitly permits frontend-first delivery.

## 4. Interaction vocabulary

The following action classes are binding. An implementation may change internal code, not the semantic class.

- **PRESENTATION** — selection, expansion/collapse, filtering, zooming, view switching, previewing. It changes no canonical domain record.
- **READ** — retrieves exact backend/repository/runtime evidence.
- **CONTEXT** — explicitly adds/removes an exact record/reference to/from Jarvis active context. Browsing alone never performs this action.
- **PROPOSE** — creates a draft/change proposal. It does not mutate canonical target state.
- **COMMIT** — explicit operator-authorized domain mutation through the owning backend contract.
- **EXECUTE** — explicit deterministic/engineering/runtime action such as validation, solver run, update phase, or terminal process interaction through a backend authority boundary.
- **NAVIGATE** — opens the owning workspace, exact record, GitHub page, or external/native file viewer.

If a UI control's class is not yet backed by an accepted owner, it must not be silently downgraded to a different behavior.

## 5. Global operator shell

### 5.1 Primary information architecture

Normal primary navigation is exactly:

`Design | Memory | Development | Coding | Settings`

There is no normal `Home`.

Normal peer destinations are exactly:

- Design: `Process | BLUECAD`
- Memory: `Project Basis | Models | Literature`
- Development: `Roadmap | Brainstorm`
- Roadmap: `Timeline | Calendar`
- Coding: `Repository | Runtime`
- Settings: `Appearance | AI | System`

Legacy `Runs`, `Engineering Data`, `Review`, `Results`, `Evidence`, `Files`, `History`, `Lineage`, standalone `Board`, and permanent Architecture are not peer primary pages. Their useful capabilities remain contextual to the record/workspace that owns them.

### 5.2 Visual identity

The byte-exact canonical HTML files own detailed colors, geometry, spacing and component styling.

Cross-surface rules are:

- light-first warm limestone/near-white application surfaces;
- restrained chlorophyll/green-accent role rather than green page backgrounds;
- Inter / Inter Display regular/medium for normal application text;
- IBM Plex Mono for code/log/path/hash/terminal-like technical text;
- the narrow/tall/light condensed treatment is reserved for colored Roadmap workstream bars;
- Phosphor is the generic icon family;
- controls/surfaces are predominantly square/nearly square with small radii;
- semantic engineering/status colors are independent of user-selected appearance accent;
- meaning never relies on color alone.

### 5.3 Jarvis behavior shared across workspaces

Where a canonical surface has Jarvis:

- Jarvis may read exact visible/selected context through backend-owned contracts;
- active context is explicit and removable;
- selecting/opening/browsing a record does not silently add it to context;
- Jarvis output remains proposal/explanation until the owning deterministic/user promotion boundary accepts it;
- a stale target invalidates a proposed mutation rather than applying it to a different revision/head;
- provider/model choice is not frontend authority.

Settings does not require the persistent Jarvis sidecar.

### 5.4 Truthful unavailable/loading/error behavior

For every surface:

- fixture/demo values from HTML are never production truth;
- unknown is shown as unknown, not inferred success;
- unsupported preview/action is explicit;
- loading does not reuse stale fixture state as if current;
- backend failure preserves exact failure identity/message/evidence where safe;
- no hard-coded `Healthy`, `PASS`, `Ready`, `Current`, `Aligned`, confidence, spend, token count, source count, run count, proposal count or version state.

---

# 6. Design

## 6.1 Shared Design semantics

Process and BLUECAD use the same global shell and right-side inspector language.

A thin contextual anchor strip may show exact current model/version, reconciled/working state, last run, proposal count and source count where authoritative data exists. These anchors are READ/NAVIGATE projections, not a duplicate model/run/source store.

`Model`, `Results`, `Runs`, `Evidence`, `Review`, `Lineage`, `Files`, and `History` remain contextual to the selected model/version/object rather than becoming peer Design destinations.

## 6.2 Process

Canonical visual reference:
`docs/design-references/process-beta/process-beta-approved-2026-08-26.html`

Normal composition is:

`process equipment navigator | dominant engineering canvas | Jarvis over Properties`

with the approved compact icon-first toolbar.

Required interaction semantics:

- selecting an equipment/object is PRESENTATION plus READ and drives the exact selected-object Properties view;
- editable Properties values are visibly editable only when an accepted backend owner allows that field to be changed;
- editing a real value must enter the owning canonical model/change authority; the frontend does not hold a private authoritative copy;
- toolbar affordances such as Select, Pan, Add equipment, Connect, Disconnect, Multi-select, Duplicate, Delete, Fit view, Zoom, Undo, Redo, Auto-layout, Validate and Solve preserve their visible roles from the approved reference;
- an affordance that lacks accepted process-topology/evaluator authority remains truthful disabled/unavailable rather than executing fake local semantics;
- Validate/Solve are EXECUTE actions and may report only real deterministic/solver results bound to exact input/model identity;
- Jarvis engineering changes are PROPOSE actions until accepted through the owning Design/model boundary;
- the current exclusion of editable Aspen-like process topology in existing queue authority is not silently bypassed by the visual reference. The affordance survives until a later engineering spec explicitly reopens/owns that behavior.

## 6.3 BLUECAD

Canonical visual reference:
`docs/design-references/bluecad-beta/bluecad-beta-approved-2026-08-26.html`

Normal composition is:

`model/feature navigator | dominant 3D viewport | Jarvis over CAD Properties`

plus a low/subordinate technical dock.

Required interaction semantics:

- object/feature selection drives exact CAD-compatible Properties such as geometry/material/dimension data where supported;
- the left navigator is a model/feature/version hierarchy, not Process equipment;
- toolbar operations are CAD-specific but retain the same icon-first density/language as Process;
- constraints, measurements, validation, analysis and messages remain subordinate dock concepts, not new peer pages;
- CAD operations/artifacts/validation/results are real only when backed by accepted BLUECAD/service contracts;
- exports/studies/versions remain contextual to exact CAD/model identity;
- Jarvis CAD changes are proposals until the owning CAD/model authority accepts them;
- Process and BLUECAD share the same visual inspector component; semantic fields differ, not the shell structure.

---

# 7. Memory

Memory is authoritative **engineering-project knowledge**. JarvisOS software self-knowledge belongs in Coding.

## 7.1 Project Basis

Canonical visual reference:
`docs/design-references/memory-beta/memory-project-basis-beta-approved-2026-08-26.html`

Normal composition is:

`Project search | Project Basis | Jarvis`

Required interaction/state semantics:

- Project Basis owns project-level objective/question, requirements, acceptance criteria, stable constraints, global decisions, boundary conditions, standards/regulations, and resource/capability constraints;
- model versions reference applicable Project Basis records rather than duplicating them wholesale;
- dossier sections are PRESENTATION disclosure controls; collapsed state remains scannable;
- engineering rows stay horizontally compact; semantic chips such as Approved/Proposed/Critical/Working remain visually adjacent to the item they describe;
- evaluable rows expose exact `Value`, `Rule/Target/Threshold`, and truthful state when canonical data supports them;
- Project search is READ/NAVIGATE over canonical Project Basis/Models/Literature/runs/artifacts/indexed content; it is never another truth store.

### Proposed change and working-revision flow

A bounded Jarvis change batch follows:

`proposal visible -> operator inspects exact diff/batch -> Approve all -> accepted batch atomically creates/advances an inspectable working revision -> proposal panel disappears -> visible values update from the new working revision`

Rules:

- `Approve all` applies only to the exact displayed bounded batch;
- approval does not overwrite the reconciled parent in place;
- user-facing labels may appear as `v13.01`, `v13.02`, `v13.03`, while backend identity remains stable/exact and is not a floating-point version;
- subsequent accepted changes derive from the exact selected working parent;
- discarded working revisions do not destroy prior reconciled snapshots/evidence;
- final reconciliation explicitly promotes a validated exact working revision while preserving immutable history and provenance.

### Deterministic impact/revalidation

After an accepted Project Basis/model change, each affected criterion/item is classified from dependency/evidence, not by generic UI guess:

1. **existing exact outputs sufficient** -> re-evaluate immediately and deterministically against the new rule/criterion; do not leave it generically stale;
2. **Process recomputation required** -> truthful recomputation-required/STALE state plus contextual `Validate in Process`;
3. **BLUECAD recomputation required** -> equivalent `Validate in BLUECAD`;
4. **multiple domains required** -> identify the required domains/chain;
5. **no material effect deterministically proven** -> record that evidence.

`STALE` is reserved for genuine recomputation requirement. LLM confidence/prose is never the validator when exact deterministic evidence is sufficient.

A future batch Validate may orchestrate the required chain only after owning backend authority exists.

## 7.2 Models

Canonical visual reference:
`docs/design-references/memory-beta/memory-models-beta-approved-2026-08-26.html`

A selected exact model/version is one engineering dossier containing, when applicable:

`Definition | Assumptions | Methods & Equations | Parameters & Inputs | Process | BLUECAD | Results & Validation | Criticalities | Sources | Artifacts | Runs | Changelog/Lineage`

Required interaction semantics:

- selected model/version/reconciled-vs-working identity remains explicit at all times;
- every result, validation, run, artifact and source usage remains bound to exact version identity;
- each major section is a PRESENTATION disclosure control;
- collapsed state shows a truthful compact summary/count/status;
- expand reveals bounded detail in place;
- collapse returns to overview without route change;
- `Collapse all` restores full overview;
- large sections use internal scroll/pagination/filter/virtualization as necessary instead of unbounded page growth;
- expanding one section must not make unrelated sections scale or lose exact selected-version context;
- disclosure state is UI presentation only.

A version-specific `Process PASS` or `BLUECAD FAIL` never becomes a timeless global evidence fact.

## 7.3 Literature

Canonical visual reference:
`docs/design-references/memory-beta/memory-literature-beta-approved-2026-08-26.html`

Normal composition is:

`Project search | Literature | Jarvis`

Required interaction semantics:

- normal state is a compact vertical list of human-readable source/file rows, not permanent Source/Document/Claim cards;
- backend may retain Source -> Document -> Claim/Datum -> Citation semantics, but compact UI does not force that taxonomy;
- clicking a row toggles that same row expanded inline;
- multiple rows may remain expanded simultaneously;
- rows after an expanded source continue immediately below it, enabling normal top-to-bottom page scrolling/comparison;
- expanded detail places source knowledge/metadata/claims/values/usages on the left and a bounded preview on the right;
- preview height stays proportionate to the textual detail block rather than becoming a full-page viewer;
- PDFs use a truthful relevant/first-page preview where available; images use a truthful thumbnail; unsupported types use explicit restrained fallback;
- clicking preview/`Open file` is NAVIGATE to a separate browser/native viewer and must not destroy Memory route/context;
- extracted values/claims preserve URL/DOI/original source and exact page/table/section/context where available;
- links to Project Basis/model records that use a source preserve exact identity;
- Jarvis research/extraction is proposal/evidence intake; a web finding does not silently become authoritative project memory.

---

# 8. Development

Development owns committed/planned project work and non-authoritative ideas.

## 8.1 Roadmap — Timeline

Canonical visual reference:
`docs/design-references/development-beta/development-roadmap-timeline-beta-approved-2026-08-27.html`

Roadmap has only:

`Timeline | Calendar`

There is no standalone Board.

Normal Timeline composition:

- large dominant timeline/Gantt work area;
- right column with `Jarvis` above `Focus & filters`;
- lower collapsible `Execution status`.

Rules:

- Timeline represents project windows, sequencing, dependencies, workstreams and milestones, not continuously occupied clock time;
- filters are PRESENTATION only and may focus status/priority/domain/model/dependency/critical path;
- the colored workstream bars alone use the approved narrow/tall/light condensed type treatment;
- workstream blocks use the approved technical, restrained 3D/bevel/depth language from the canonical HTML;
- `Execution status` reuses the same Roadmap item IDs and emphasizes `Ready | In progress | Blocked`;
- Planned/Completed are secondary/filterable, not permanent primary columns;
- expanding/collapsing Execution status does not create another route/store;
- status changes affect the same Roadmap item visible in Timeline/Calendar links;
- deterministic dependency gates own whether `Ready` is true where knowable;
- transition to `Done` cannot silently bypass deterministic `Done when`/acceptance conditions;
- manual work items are COMMIT actions under operator authority;
- Jarvis-created Roadmap changes are PROPOSE until accepted.

### Roadmap work-item editor

`+ Add work item` and `Edit` use the same canonical entity/editor semantics.

Supported product fields include:

- title;
- type: `Task | Work package | Milestone | Investigation | Validation | Decision | Procurement | Manufacturing | Meeting/Review`;
- start/end, or one date for a milestone;
- status: `Planned | Ready | In progress | Blocked | Done | Cancelled`;
- priority: `Critical | High | Normal | Opportunity`;
- description;
- domain;
- links to Project Basis, model/version, criterion, Literature, Process/BLUECAD objects and Brainstorm;
- `Depends on` / `Blocks`;
- cannot-start-before / must-finish-before constraints;
- acceptance / `Done when`;
- tags;
- owner;
- effort estimate;
- progress/subtasks;
- resources/cost;
- notes/attachments;
- provenance.

Clicking an existing item exposes at minimum:

`Open details | Edit | Delete`

Deletion/edits act on the same canonical item identity and follow the owning backend's integrity/provenance rules.

## 8.2 Roadmap — Calendar

Canonical visual reference:
`docs/design-references/development-beta/development-calendar-beta-approved-2026-08-27.html`

Calendar is **actual time allocation**, not a duplicate Roadmap visualization.

Views are:

`Day | Week | Month | Agenda`

Week is the normal default because hour/minute planning is central.

Supported event semantics include:

- work session;
- call/meeting;
- experiment/lab session;
- review;
- reminder;
- deadline;
- unavailable/personal block.

Rules:

- exact date/start/end/time-zone identity;
- one Roadmap item may link to zero, one or many Calendar blocks;
- Roadmap project duration does not imply Calendar occupancy;
- creating from a Roadmap item prelinks that exact item;
- creating from a Calendar cell pre-fills the selected date/time;
- effort metrics are shown only when derived from real linked work/time data;
- Jarvis may READ availability/deadlines/dependencies and PROPOSE exact slots; it does not silently insert them.

`+ Add event` supports product fields including:

- title;
- event type;
- date;
- start/end time;
- priority;
- description;
- linked Roadmap item;
- domain;
- location/meeting link;
- participants;
- reminder;
- done-when;
- tags;
- optional notes/attachments.

Selecting an event exposes at minimum:

`Open details | Open roadmap item` when linked, `Edit | Delete`.

Month is overview-oriented. Day/Week are execution-planning views. Agenda is chronological.

## 8.3 Brainstorm

Canonical visual reference:
`docs/design-references/development-beta/development-brainstorm-beta-approved-2026-08-27.html`

The authoritative product flow is:

`RAW -> discussion/reconciliation with Jarvis -> RECONCILED -> explicit promotion`

The old Inbox/Exploring/Candidate/Kanban concept is superseded.

### Raw

Raw is intentionally low-friction, non-authoritative capture:

- free-form text;
- file attachment;
- future local speech-to-text;
- rough question/idea/observation/problem/opportunity.

Rules:

- original Raw text/files are preserved and are not silently rewritten by Jarvis;
- Phosphor paperclip and microphone are the approved generic affordances for attach/speech;
- truthful lineage states include `NEW | DISCUSSED | RECONCILED | SUPERSEDED`;
- `DISCUSSED` is not equivalent to `SUPERSEDED`;
- reconciliation/supersession never destroys provenance.

Speech capture, if promoted later, is not a parallel model path: every transcription inference uses canonical JarvisOS AI execution/audit/egress authority.

### Reconciled

At rest each IDEA remains compact enough to scale to many records: exact idea ID, title, concise takeaway and only necessary domain/state/update metadata.

Clicking expands inline; multiple IDEA records may remain expanded.

Expanded content preserves **the result of reasoning**, not merely a paraphrase of Raw:

- variable-length `Discussion synthesis`;
- why useful/not useful;
- accepted/rejected constraints;
- trade-offs;
- chosen direction;
- open questions;
- structured bullets/tables/decision summaries;
- inline SVG/schematics where materially useful;
- compact/collapsible provenance to Raw records, files, relevant discussions and exact project/Design/Coding context;
- revision/lineage when later discussion updates an existing idea.

Available explicit actions include, where applicable:

`Edit | Supersede | Add to Roadmap | Promote -> Design | Promote -> Coding`

Promotion creates the owning target proposal/work item. It does not directly mutate Design models or repository code.

### Jarvis active context basket

Jarvis remains fixed on the right.

Rules:

- opening an IDEA for reading does not change Jarvis context;
- `Add to Jarvis context` / `Discuss with Jarvis` explicitly adds exact references;
- active context is visibly represented and removable;
- multiple IDEA records may be loaded simultaneously;
- context may combine IDEA/Raw/model/Project Basis/Literature/Design/Coding refs;
- natural-language requests such as “recover context of IDEA-030 and IDEA-034” resolve to the same explicit exact-reference mechanism;
- Jarvis may propose reconciliation updates, supersession, new derived IDEA, Roadmap work or target promotion, but domain mutations require approval.

---

# 9. Settings

Canonical visual reference:
`docs/design-references/settings-beta/settings-beta-approved-2026-08-26.html`

Settings owns only:

`Appearance | AI | System`

No persistent Jarvis sidecar. No giant dashboard cards.

## 9.1 Appearance

Required concepts:

- `System | Light | Dark`;
- accent presets represented by the canonical HTML;
- custom HEX/accent picker;
- accent affects shell focus/navigation/selection emphasis;
- engineering/scientific/status colors remain semantic and independent.

Typeface/density/motion customization remains optional future capability unless separately promoted; it is not permission to change the canonical production type hierarchy.

## 9.2 AI

Stable groups are:

`Providers | Orchestration | Budget & limits`

Provider management is generic/provider-scoped:

- provider/integration ID/name;
- truthful connection/config status;
- endpoint when relevant;
- masked credential state;
- model/capability catalogue where discoverable;
- usage/cost capability where supported;
- bounded actions such as connect/manage/update credential/test/refresh/disconnect.

Rules:

- no API key per model;
- never redisplay full stored secrets;
- credential mutation uses canonical secure backend storage;
- OpenAI API and Codex integration may be distinct identities;
- Anthropic API and Claude Code integration may be distinct identities;
- DeepSeek, Z.AI, OpenRouter, local AI, Scaleway and future providers must fit the same generic pattern rather than forcing a redesign;
- OpenRouter/opportunistic free/cheap models are allowed as provider/policy options, not mandatory routing intermediaries;
- sensitive project data remains subject to egress/privacy policy regardless of price;
- Orchestration exposes policy/permission/status, not frontend model-routing authority;
- Hermes label/status does not create Hermes backend authority before accepted specs;
- deterministic validation remains separate from model/orchestrator output.

Budget/limits values are shown only when real accounting/config provides them.

## 9.3 System

System is the normal home for non-primary diagnostics.

Compact groups include:

`Application | Runtime | Data | Services | Advanced`

Typical READ-only/managed data may include app version/environment, backend status/endpoint, local AI runtime, data-root, database/schema/path, provider config state, Process/BLUECAD service availability, migration/runtime diagnostics, logs and reload-system-state actions.

Advanced details remain subordinate/collapsed.

---

# 10. Coding

Coding owns JarvisOS software-development knowledge/state. It is not a generic IDE clone.

## 10.1 Repository

Canonical visual reference:
`docs/design-references/coding-beta/coding-repository-beta-approved-2026-08-27.html`

Repository means GitHub remote/future software state.

The first screen preserves:

- truthful repository/branch/exact SHA identity;
- active-development lifecycle;
- current work;
- dominant Repository Inspector;
- persistent right Jarvis panel.

The canonical lifecycle is:

`Proposal -> Plan -> Implementation -> Tests -> Independent Review -> Reconciliation -> Merge`

Rules:

- one exact repository/head identity at each stage;
- implementation writes occur only in isolated branch/worktree under accepted authority;
- head mutation invalidates stale gate/review evidence;
- currently executed code is never the direct mutation target;
- current work derives from canonical queue/spec/PR state rather than UI inference.

### Repository Inspector

Repository Inspector is the normal search/preview entry point for:

- Markdown/specs;
- architecture Markdown/SVG/image artifacts;
- source code;
- tests;
- config/workflows;
- other explicitly safe repository-readable artifacts.

Rules:

- literal/path/ID search is truthful and preserves exact ref/path/blob identity;
- semantic retrieval may be added only as bounded evidence-backed retrieval, never as source authority;
- presentation filters such as `All | Docs | Specs | Architecture | Code | Tests | Config` do not create separate stores;
- Markdown supports `Rendered | Raw`;
- source/config receives bounded text/syntax preview;
- SVG/image renders only when selected and safe;
- unsupported/too-large/binary state is explicit;
- architecture is an artifact family inside Inspector, **not** a permanently visible default graph.

A selected artifact exposes where applicable:

`Add to Jarvis context | Suggest modification | Open on GitHub`

Action classes:

- Add to context = CONTEXT;
- Open on GitHub = NAVIGATE to exact real ref/path;
- Suggest modification = PROPOSE.

`Suggest modification` flow is exactly:

`selected exact target/ref -> user instruction -> proposed diff/plan/reason/affected files -> discussion -> development proposal/plan -> isolated implementation lifecycle`

It is never a direct save-to-repository button.

## 10.2 Runtime

Canonical visual reference:
`docs/design-references/coding-beta/coding-runtime-beta-approved-2026-08-27.html`

Runtime means the JarvisOS files/build **actually executed locally**.

Local-vs-GitHub identity is the dominant first-screen concept.

Required independent facts:

- local actually executed version/SHA/path/branch/dirty-clean/health where observable;
- selected/latest approved GitHub version/SHA;
- explicit alignment state: aligned/local-behind/divergent/unknown.

Presentation requirements:

- local current uses the approved green treatment plus explicit text such as `Local current / actually executed`;
- newer remote uses approved orange treatment plus explicit text such as `Latest GitHub / not yet executed locally`;
- color is never the only meaning;
- a remote SHA is never presented as running merely because it is newer.

### Semantic delta

When comparable remote state is ahead:

- compare exact commit ancestry/diff;
- summarize `What GitHub added after local version`;
- bind each summary to real commits/files/spec evidence;
- expose underlying commits/files for inspection;
- classify docs/reference-only vs runtime-affecting only when supportable;
- divergent/unrelated/unknown history fails honestly;
- an LLM-only feature summary cannot be presented as fact.

Service health remains visible but subordinate.

### Safe update

Update details remain compact until explicitly requested.

The required conceptual sequence is:

`preserve state -> fetch exact approved target -> dirty-state guard -> migration/build -> deterministic smoke -> restart -> post-restart health -> rollback on failure`

Rules:

- exact current/target identity remains visible;
- unsafe dirty local state blocks blind overwrite;
- failed candidate startup/health preserves an evidence-backed rollback result;
- update/restart is explicit operator/policy authority, not a background assumption;
- self-update authority is separate from terminal authority.

## 10.3 Future integrated terminal

Approved direction:
`docs/design-references/coding-beta/CODING_RUNTIME_TERMINAL_FUTURE_2026-08-27.md`

The lower Runtime area may become:

`Terminal | Logs`

without displacing local-vs-GitHub identity/delta/update hierarchy.

Required user behavior:

- real PTY/session, not fake terminal;
- PowerShell default on Windows;
- session cwd and scroll/history;
- stdin and Ctrl+C/interrupt;
- validated `Open terminal here` from Repository Inspector/worktree/path;
- explicit `Send output to Jarvis`;
- Jarvis command proposal with `Insert in terminal` / Copy;
- Logs retained adjacent.

Hard authority/security behavior:

- no frontend-direct shell/filesystem/process authority;
- backend-mediated local/authenticated PTY/session service;
- child environment is deliberately minimum/scrubbed and does not inherit protected provider/repository/credential secrets by default;
- protected cwd/path/credential locations are denied/isolated by backend policy;
- raw PTY bytes do not bypass a backend secret-safe display/isolation/redaction boundary before frontend response;
- if target-OS isolation/redaction cannot be proven, arbitrary PTY remains `DEFER_TRIGGERED`/unavailable;
- Send-output-to-Jarvis applies another bounded context/egress secret policy;
- Jarvis-proposed commands are not executed automatically by default;
- high-risk/destructive command classes require backend/policy classification and explicit confirmation;
- terminal cannot bypass Git/spec/review/update authority for JarvisOS self-modification;
- offline CI uses fake/controlled PTY adapters.

---

# 11. Cross-surface state links

The following links are mandatory and must not be implemented as duplicate identities/stores:

1. Project Basis changes -> exact model working revisions -> deterministic impact/revalidation -> reconciliation.
2. Literature sources -> exact Project Basis/model usages with provenance.
3. Models -> exact Process/BLUECAD configuration, results, validation, artifacts and runs.
4. Roadmap item -> zero/one/many Calendar blocks, with one Roadmap identity.
5. Brainstorm IDEA -> explicit Roadmap/Design/Coding proposal promotion, never silent authority mutation.
6. Brainstorm speech -> canonical AI execution/audit/egress spine.
7. Repository -> exact remote GitHub truth; Runtime -> independently observed local executed truth.
8. Runtime semantic delta -> Repository commit/file evidence.
9. Repository Inspector selected artifact -> explicit Jarvis context, not implicit browsing context.
10. Terminal -> separate PTY security authority; terminal access does not imply self-update or repository mutation authority.
11. Settings AI -> canonical provider/secure-storage/routing/egress/budget boundaries; Settings labels do not create provider/orchestrator authority.

# 12. Explicitly superseded interpretations

Future builders must not resurrect the following without a new maintainer decision:

- normal `Home`;
- peer primary `Runs`, `Engineering Data`, `Review`, `Results`, `Evidence`, `Files`, `History`, or `Lineage`;
- Design tabs `Model | Process | BLUECAD | Results | Lineage`;
- Memory peer sections `Evidence`, `Files`, `History`, `Roadmap`, `Inbox`;
- standalone Development `Board`;
- Brainstorm `Inbox | Exploring | Candidate`/Kanban lifecycle;
- a permanently pinned Architecture graph as Coding Repository default;
- Runtime first screen dominated by migration/update phases;
- generic indefinite STALE when exact stored outputs can deterministically satisfy the new criterion;
- opening a Brainstorm/Repository record implicitly adding Jarvis context;
- `Suggest modification` as a direct repository save action;
- latest GitHub SHA treated as the local running SHA;
- terminal as frontend shell escape or default Jarvis auto-execution;
- static unbounded Models/Literature cards that cause the page to grow indefinitely;
- forcing Source/Document/Claim taxonomy into every Literature compact row.

# 13. Implementation evidence requirement

A retained canonical implementation slice must prove, on one exact head:

1. the corresponding canonical HTML/reference identity;
2. browser evidence at the manifest viewport for normal/default state;
3. browser evidence for each interaction state owned by that slice;
4. exact real backend/repository/runtime evidence for every non-fixture state shown;
5. disabled/unavailable state for approved capabilities whose backend owner is intentionally deferred;
6. no silently dropped capability;
7. no new peer page/store/authority;
8. accessibility/keyboard/focus behavior equivalent to the approved interaction;
9. all deferred behavior has an explicit canonical owner or concrete trigger.

100c may merge pseudo-specs and minimize implementation boundaries, but it may not change these approved product semantics or silently omit a row because implementation is inconvenient.
