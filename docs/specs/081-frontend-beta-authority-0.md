# Spec 081 — FRONTEND-BETA-AUTHORITY-0

**Definition status:** definition-only umbrella; registry remains `planned`.

**Depends on:** none mechanically. This authority is derived from the merged baseline named below.

**Target path:** `docs/specs/081-frontend-beta-authority-0.md`

**Implementation PR:** none. Spec 081 must never receive an implementation PR.

---

## 1. Purpose

JarvisOS already contains a substantial deterministic backend, a working but fragmented React
frontend, a real BLUECAD candidate workbench, simulation-run infrastructure, MemoryStore proposal
authority, dependency and freshness graphs, AI routing and egress controls, and a bounded process
kernel.

The current frontend does not expose those capabilities as one coherent beta product.

This document authorizes the next product front and freezes its boundaries. The objective is not to
create a temporary visualization layer. It is to establish the long-term frontend application shell
and the first complete operator workflows while preserving every valid backend authority and
avoiding speculative systems that the runtime does not yet support.

This document:

1. authorizes incremental reconstruction of the frontend shell and operator surfaces;
2. reopens UI foundation work from current `master`;
3. defines the binding implementation queue;
4. places secure credential persistence before frontend implementation;
5. preserves backend, safety, provenance, egress, budget, proposal, validation and promotion
   boundaries;
6. records the canonical product-topology direction;
7. classifies existing 072/074 topology work as a preserved alternative rather than the default
   product v1 concept;
8. defers the Aspen-like editable process environment;
9. preserves one implementation front at a time;
10. defines phase checkpoints, evidence and a controlled queue re-derivation path.

This is a definition-only umbrella. Implementations are owned only by the independently removable
slices listed in this document.

## 2. Baseline authority and verification

### 2.1 Repository baseline

This authority is derived from:

```text
repository: AlbertoRacerro/JarvisOS
branch: master
commit: 2183b2282d239ed570c59d0982e227e54c62dad7
```

That baseline contains no authorized backend or frontend implementation front.

### 2.2 Green evidence

The immediately merged reconciliation PR used exact head:

```text
2eb164c3875ea64e591e87ca832b2f83cde97cca
```

That head passed:

```text
CI run:                      30822120165
BLUECAD Real Tool Proof run: 30822120368
result:                      success / success
```

The PR head and resulting merge commit modify only `docs/specs/STATUS.md`. Both resolve that file
to blob:

```text
b1a8881f139277a80452fb57dd48475c49e853e7
```

The merge commit itself exposes no separately associated status checks through the available GitHub
interface. The accepted baseline evidence is therefore exact-content-equivalent green evidence from
the PR head, not a claim that the merge commit was independently rerun.

Any material movement of `master` before the 081 definition PR is opened requires re-reading:

- `AGENTS.md`;
- `docs/ARCHITECTURE.md`;
- `docs/DECISIONS.md`;
- `docs/specs/STATUS.md`;
- frontend application-shell and client files;
- BLUECAD export and viewer code;
- secrets storage;
- AI execution and MemoryStore provenance.

## 3. Binding product decisions

### 3.1 Incremental frontend reconstruction is authorized

JarvisOS may replace the current page composition, navigation and presentation surfaces.

The implementation must preserve or migrate:

- React;
- TypeScript;
- Vite;
- Three.js;
- backend routes and schemas;
- useful frontend types;
- valid API-client behavior;
- real GLB rendering;
- BLUECAD candidate lifecycle;
- validation and evidence behavior;
- simulation-run persistence;
- MemoryStore proposal and promotion boundaries;
- dependency and freshness semantics;
- AI routing, budget, ledger and egress controls;
- existing deterministic tests.

This authority does not permit:

- a second frontend stack;
- a second backend;
- a second engineering-truth store;
- a big-bang rewrite;
- direct frontend access to providers, files or SQLite;
- silent replacement of accepted records;
- removal of working BLUECAD behavior without equivalent replacement.

### 3.2 Spec 070 is unfrozen but must be re-derived

Spec 070 remains the UI-foundation identifier.

The 2026-07-29 freeze is lifted only for a new definition derived from the current baseline and
started after spec 082.

Closed PR #132 and its retained branch are historical input, not implementation authority.

The new 070 definition must cover:

- semantic design tokens;
- `system`, `light` and `dark` appearance;
- typography;
- spacing;
- status vocabulary;
- buttons, inputs, panels, tables and disclosures;
- loading, empty and error states;
- keyboard navigation;
- focus visibility;
- contrast;
- reduced motion;
- non-colour-only state communication.

JarvisOS remains desktop-first. Phase 1 does not require a full touch-first optimization programme.
Controls must remain usable and not pathologically small, but a separate mobile or touch design
exercise is not a beta prerequisite.

### 3.3 Specs 066–068 remain frozen

Specs 066, 067 and 068 remain frozen.

The frontend beta:

- does not depend on Hermes;
- does not expose Hermes;
- does not start an MCP server;
- does not introduce a second conversation authority;
- does not represent personas as autonomous or continuously online agents.

The initial Jarvis surface reuses the existing AI execution spine.

### 3.4 Spec 080 remains frozen

Spec 080 remains `planned` and frozen for the duration of the frontend-beta queue.

It is the separately removable authority for future automated review, finding, correction and
re-review behavior. No part of 080 may be smuggled into 079, the frontend-beta slices, CI changes, or
review tooling while this queue is active.

Reopening 080 requires a later explicit queue or authority spec.

### 3.5 Spec 062 remains blocked and gains a non-blocking design gate

Spec 062 remains:

```text
blocked
```

No frontend implementation of GRADE-0 is authorized by this document.

After the Phase-4 Review checkpoint, one operator-design session must be requested covering:

- placement;
- four-choice interaction;
- keyboard and screen-reader behavior;
- revision;
- withdrawal;
- stale-subject conflicts;
- concurrent or superseded grade state;
- relationship to Review;
- relationship to route evaluation;
- whether grading is attached to flows, attempts, proposals or another subject.

The session is not a repository implementation front. It may remain pending, or occur at the
conversation/design level, while Phase 5 proceeds. Any resulting repository edit must still respect
the single-front rule and wait for a normal front boundary.

While the session is pending:

- Phase 5 may proceed;
- no Phase-5 slice may add, imitate, consume or redesign the grade surface;
- no route-evaluation claim may treat missing grades as negative or neutral evidence;
- all 062-dependent work remains blocked.

### 3.6 Canonical product topology

The canonical product topology is project-specific. It is defined in the project pack, not in this repository, and is
product authority rather than a claim that the topology is already implemented.

### 3.7 Existing 072 and 074 are preserved alternatives

Specs 072 and 074 remain merged and supported.

They represent a symmetric parallel topology plus a deterministic topology-aware CAD link.

They must:

- remain inspectable;
- retain all lineage and evidence;
- remain available as topology experiments or alternative architectures;
- never be deleted or rewritten to mimic the canonical product topology;
- not appear as the default product v1 topology;
- receive no new implementation work during this frontend front.

Future spec 093 owns the canonical product topology and remains outside the binding frontend queue.

### 3.8 Aspen-like environment is deferred

The beta shell must contain a static `FlowsheetStage` seam.

It must not contain:

- a fake process canvas;
- draggable but non-executable blocks;
- a blank canvas presented as a product;
- a runtime plugin system;
- a second graph store.

The current runtime provides:

- 050 dependency and provenance graph;
- 051 freshness propagation;
- 075 immutable acyclic process execution;
- fixed bundled process profiles.

It does not provide an editable Aspen-like flowsheet.

The initial `FlowsheetStage` must therefore render an honest unavailable state and direct the
operator to Runs or Lineage.

An editable process environment requires separate future authority for at least:

- user-created persistent flowsheets;
- a general unit-operation catalogue;
- block creation and deletion;
- editable connections;
- persistent layout;
- graph validation;
- user-graph execution;
- the canonical product topology;
- undo and redo;
- loop closure;
- recycle convergence;
- solver diagnostics.

### 3.9 AI thread and egress policy

The future local thread domain uses four levels.

#### Level 1 — local persistence

A thread may be stored locally under the JarvisOS data root without preventive pseudonymization.

#### Level 2 — selected message or excerpt

A selected message or excerpt may leave the machine only through existing 059a/059b authority:

- classification;
- minimization;
- sanitization;
- exact packet;
- provenance;
- budget;
- confirmation where required;
- audit.

#### Level 3 — raw complete thread

Forbidden in the first implementation.

#### Level 4 — fully pseudonymized complete thread

Deferred to a separate future specification.

The local thread implementation must not wait for Level 4.

## 4. Immediate prerequisite: secure credential persistence

Secure credential persistence is the first implementation slice after this 081 authority.

No frontend implementation precedes it.

The reason is operational and security-critical:

- the current UI-entered Scaleway key is held only in process memory;
- it disappears at backend restart;
- the operator pays this friction repeatedly;
- frontend Settings must not imply durable storage before it exists;
- frontend and AI verification will repeatedly restart the backend.

The implementation is sequential, not parallel, because the repository permits one implementation
front at a time.

### 4.1 Scope boundary

Spec 082 must implement a narrow backend extension to the current secrets boundary.

It must not create:

- a general vault platform;
- cloud secret synchronization;
- multiuser secret sharing;
- a second configuration database;
- frontend storage of secret values.

### 4.2 Preferred Windows-first direction

The 082 definition must begin from this preferred candidate:

```text
Windows current-user DPAPI
behind a narrow replaceable credential-protection/keyring interface
with a deterministic fake implementation for Linux CI
```

This freezes neither a Python package nor a storage-file format.

The full 082 definition must verify that the selected implementation satisfies the acceptance
boundary. It may choose a different OS-owned protection mechanism only if repository or environment
evidence demonstrates that the preferred candidate cannot meet the required behavior.

The design must not store encrypted secret bytes beside an application-managed decryption key that
is equally accessible to the same storage compromise.

### 4.3 Required security decisions

The full 082 definition must address:

- Windows-first local operation;
- Linux CI and deterministic testability;
- storage-key separation;
- process restart;
- deletion;
- corruption;
- precedence between environment and persisted secret;
- backup inclusion or exclusion;
- file and account permissions;
- migration from runtime-only state;
- bounded status reporting;
- no plaintext logs;
- no secret echo;
- no plaintext SQLite value unless independently protected by justified OS-owned key authority.

### 4.4 UI dependency

Re-derived spec 029 depends on 082.

Settings must distinguish:

- not configured;
- environment-provided;
- runtime-memory only;
- securely persisted;
- unavailable or corrupted.

The word `saved` must not be used for runtime-memory-only state.

### 4.5 Operator-visible P0 proof

The technical gate remains restart, deletion, corruption, log-scan and storage-scan evidence.

In addition, P0 must include one operator-visible runbook using the current UI or an equivalent
existing operator surface:

1. start the backend;
2. enter the provider key;
3. stop and restart the backend;
4. observe `securely persisted`, not `not configured`;
5. execute one bounded live-provider smoke call without entering the key again.

The live call:

- is not a CI test;
- must use the existing budget, route, sensitivity and egress controls;
- requires explicit operator confirmation at the checkpoint if it incurs real spend;
- must record only safe ledger evidence;
- must not expose the secret in request diagnostics or responses.

A screenshot is not required. The proof exists to provide direct operator confidence as well as
technical verification.

## 5. Primary stage and selection authority

### 5.1 Static stage registry

The shell supports exactly:

```text
ModelStage
ResultsStage
ReviewStage
FlowsheetStage
```

No runtime plugin mechanism is authorized.

### 5.2 A0 and A1 selection must be type-distinct

The initial application-shell selection contract must make geometric and semantic selection
structurally different.

Initial A0 contract:

```ts
type StageSelection =
  | {
      kind: "record";
      ref: RecordRef;
    }
  | {
      kind: "geometry-hit";
      viewerSessionId: string;
      ephemeralObjectId: string;
      point?: [number, number, number];
    };
```

The `geometry-hit` branch:

- has no `RecordRef`;
- has no `sceneComponentId`;
- is valid only within the current viewer session;
- must not be persisted;
- must not be used for evidence binding;
- must not survive artifact reload;
- must not be interpreted as a part identity.

After spec 092 supplies verified scene binding, re-derived spec 058c may add:

```ts
type SceneComponentSelection = {
  kind: "scene-component";
  ref: RecordRef;
  sceneComponentId: string;
  bindingManifestDigest: `sha256:${string}`;
  glbArtifactId: string;
};
```

Consumers must exhaustively discriminate the union.

A mesh index, Three.js object UUID, node array position or exporter-generated name can never be
promoted into `sceneComponentId`.

## 6. Evaluation-signal preservation

### 6.1 Existing evidence

The current runtime already records the following evidence.

#### AI provider and fallback attempts

`ai_jobs` and token-flow state include, where applicable:

- task kind;
- requested route;
- selected route;
- provider;
- model;
- flow ID;
- flow attempt index;
- fallback index;
- continuation index;
- parent attempt;
- status;
- outcome reason;
- finish reason;
- adapter invocation;
- external dispatch state;
- token usage;
- accounting basis;
- provider spend;
- latency;
- error type.

#### BLUECAD structural outcomes

`bluecad_attempts` records:

- candidate;
- attempt number;
- route;
- linked AI job;
- proposal outcome;
- build outcome;
- validation verdict;
- artifacts;
- structured error detail.

BLUECAD malformed-output, build and validation retries are therefore derivable from the attempt
ledger.

#### Proposal outcomes

AI-originated assumptions, parameters and decisions retain `source_ai_job_id`.

That join remains available after:

- acceptance;
- rejection;
- Parameter supersession.

### 6.2 Retry meanings must remain distinct

There is no generic schema-validation retry count owned by `run_ai_task`.

The AI execution spine retries:

- configured fallback bindings after retryable provider errors;
- bounded token-flow continuation where authorized.

Schema, build and validation retries are workflow-specific, such as BLUECAD.

The implementation must not collapse these different meanings into one ambiguous integer.

### 6.3 Requirements for AI Threads

Spec 090 must:

1. add thread provenance to AI execution through a nullable `thread_id` or an equivalent typed
   relation;
2. version or strictly document the operator-facing task-kind taxonomy;
3. preserve the one-row-per-attempt model;
4. preserve BLUECAD-attempt joins;
5. prove proposal → AI-job joins for accepted, rejected and superseded records;
6. provide a read model suitable for later grouping by:
   - task-taxonomy version;
   - task kind;
   - requested route;
   - concrete provider and model;
   - attempt outcome;
   - proposal disposition;
   - cost;
   - latency;
7. avoid creating route scores or automatic route promotion.

If a future structured-output workflow performs model-assisted schema repair, every repair must be
represented as a distinct attempt with parent lineage and a reason code. It must not be hidden
inside an in-memory retry counter.

Spec 025 remains the later route-evaluation authority and still requires sufficient representative
graded use.

## 7. Transition continuity and legacy surfaces

The application must remain usable throughout migration.

Spec 083 must preserve not-yet-migrated pages under explicit legacy routes:

```text
/legacy/domain-foundation
/legacy/ai-draft
/legacy/system-status
```

Rules:

- legacy routes are not shown in primary navigation;
- they carry a visible `Legacy diagnostic surface` label;
- they retain the same backend authority boundaries;
- they must not bypass the new shell, secret or proposal rules;
- they remain directly reachable during transition;
- each route is removed only by the spec that proves functional replacement;
- no migrated function may exist indefinitely in two primary navigation locations.

The current BLUECAD workbench is mounted through a compatibility adapter during shell
implementation and then replaced by spec 085.

## 8. Binding implementation queue

### 8.1 Size definitions

The estimates below are relative engineering sizes, not calendar promises.

| Size | Meaning |
| --- | --- |
| S | Documentation or a narrow localized change with a small focused test surface |
| M | One bounded cross-module implementation with clear contracts and focused integration tests |
| L | A broad vertical slice with several components, significant failure modes and a substantial review surface |

Every L slice must be split during its complete-definition step if it cannot remain independently
reviewable in one pass. Splitting at that point is correct queue maintenance, not a planning failure.

The queue remains sequential.

Every implementation slice must pass:

```text
backlog row
→ kernel or definition
→ complete specification
→ readiness decision where required
→ implementation
→ exact-head deterministic gates
→ review
→ merge
→ registry reconciliation
```

### 8.2 Ordered queue

| Order | Spec | Size | Scope |
| ---: | --- | :---: | --- |
| G0 | 081 FRONTEND-BETA-AUTHORITY-0 | S | Definition-only product authority and queue |
| 1 | 082 SECURE-CREDENTIAL-STORAGE-0 | M | Durable backend provider-secret storage |
| 2 | 070 UI-FOUNDATION-1, re-derived | M | Tokens, themes, primitives and accessibility baseline |
| 3 | 083 APP-SHELL-1 | L | Router, rail, top bar, sidecar, navigator, dock, PrimaryStage and legacy continuity |
| 4 | 084 BLUECAD-READ-MODEL-1 | M | Candidate aggregate read surface for artifacts, evidence, runs and freshness |
| 5 | 085 BLUECAD-WORKBENCH-2 | L | Full BLUECAD migration into the new shell |
| 6 | 086 MODEL-INSPECTION-A0 | L | Geometry-only inspection tools and viewer lifecycle hardening |
| 7 | 087 LINEAGE-OVERVIEW-1 | M | Early read-only 050/051 graph overview with compact inspector |
| 8 | 088 RUNS-WORKBENCH-1 | M | Run list/detail, bindings, results, logs and artifacts |
| 9 | 035 ENGINEERING-DATA-1, re-derived | L | Searchable engineering-record navigation |
| 10 | 089 ANALYTICS-DOCK-1 | L | Unit-aware, comparable, real-data analytics |
| 11 | 054 PROPOSAL-REVIEW-1, re-derived | L | Human promote/reject authority and replacement consequences |
| Design | 062 operator-design session | S | Non-implementation design; may remain pending while Phase 5 proceeds |
| 12 | 090 AI-THREADS-0 | L | Local episodic thread domain and evaluation provenance |
| 13 | 091 JARVIS-SIDECAR-1 | L | Contextual Jarvis and role profiles |
| 14 | 029 SETTINGS-1, re-derived | M | Settings, provider status, budget, storage, tools and diagnostics |
| 15 | 092 SCENE-BINDING-0 | L | Verified backend GLB/component binding contract |
| 16 | 058c SCENE-SEMANTICS-A1, re-derived | L | Semantic selection, isolate and evidence highlighting |
| 17 | 006b PARAMETRIC-VARIANTS-1, re-derived | L | Deterministic child variants; no comparison in this slice |
| 18 | 058b VARIANT-COMPARISON-1, re-derived | M | Comparison and parent-child design history |

### 8.3 Frozen and outside-queue work

The following remain outside the binding queue:

| Spec or area | Disposition |
| --- | --- |
| 066–068 | Frozen; Hermes integration is not reopened |
| 078 | Planned; documentation does not authorize implementation |
| 080 | Frozen; autonomous review and repair is not reopened |
| 093 | Planned future canonical product topology |
| Aspen-like editable flowsheet | Unnumbered future authority |
| Full-thread pseudonymized egress | Unnumbered future authority |

No work on these items starts during the frontend-beta queue.

### 8.4 Controlled queue re-derivation

The queue is binding but not immutable.

If an active slice proves non-implementable within its accepted boundary, or if a prerequisite
proves insufficient during implementation, the active front stops and the remaining queue may be
re-derived through a later definition-only authority spec.

That re-derivation:

- does not invalidate already merged slices;
- does not reopen 066–068 or 080;
- does not modify the product decisions in section 3 unless it says so explicitly;
- records the stop reason, attempted routes and reached state;
- passes the same documentation, registry, exact-head and review gates as this authority;
- keeps independently removable work separate.

Silent abandonment, substitution, queue skipping or scope absorption is not authorized.

## 9. Phase composition and checkpoints

Screenshots are required at completed frontend phase checkpoints, not for every intermediate PR.

### Pre-phase G0

Contains:

- this authority document;
- registry reconciliation;
- queue activation.

Evidence:

- no screenshot;
- exact-head CI;
- BLUECAD Real Tool Proof;
- registry gate;
- review.

### Prerequisite P0 — secure credentials

Contains:

- 082.

Evidence:

- backend tests;
- restart proof;
- delete proof;
- corruption proof;
- log and persisted-storage scan;
- operator-visible proof from section 4.5;
- no formal screenshot requirement.

### Phase 1 — Foundation and Shell

Contains:

- 070;
- 083.

Checkpoint evidence:

1. full desktop shell;
2. compact-width shell;
3. one loading, error or empty state;
4. one real current backend subject;
5. direct deep-link reload;
6. visible legacy diagnostic route;
7. keyboard and focus demonstration.

### Phase 2 — BLUECAD and A0

Contains:

- 084;
- 085;
- 086.

Checkpoint evidence:

1. real GLB artifact;
2. candidate status and freshness shown separately;
3. validation and evidence;
4. attempt history;
5. at least three A0 tools;
6. one candidate without FEM showing a didactic empty state;
7. compact-width behavior;
8. no conceptual image presented as backend output.

### Phase 3 — Lineage, Runs, Engineering Data and Analytics

Contains:

- 087;
- 088;
- re-derived 035;
- 089.

Spec 087 comes first and depends only on merged 050/051 plus shell 083. Its compact inspector may
show bounded metadata already returned by 050. Engineering Data later adds full record navigation,
and Review later adds proposal actions.

Checkpoint evidence:

1. parameter → run → result navigation;
2. dependency and provenance distinction;
3. one stale path;
4. one failed or incomplete run;
5. one engineering record;
6. one real analytics widget;
7. unit incompatibility rejected visibly;
8. compact-width behavior.

### Phase 4 — Review and Human Authority

Contains:

- re-derived 054;
- integration with 087 Lineage.

Checkpoint evidence:

1. proposed/current comparison;
2. promote or reject operation;
3. Parameter replacement path where applicable;
4. affected stale records before confirmation;
5. resulting lineage path;
6. no 062 grading controls;
7. accessible confirmation and conflict state.

After this checkpoint, request the 062 operator-design session. Phase 5 need not wait for the
operator's availability, subject to section 3.5.

### Phase 5 — Jarvis and Settings

Contains:

- 090;
- 091;
- re-derived 029.

Checkpoint evidence:

1. local persisted thread;
2. candidate selected as context;
3. source manifest and context digest;
4. role-profile change;
5. proposal created but not promoted;
6. provider and budget status;
7. securely persisted credential state;
8. raw complete-thread external egress denied;
9. no Hermes runtime, grade surface or fake online presence.

### Phase 6 — Scene Semantics and Variants

Contains:

- 092;
- re-derived 058c;
- re-derived 006b;
- re-derived 058b.

Checkpoint evidence:

1. named scene component selected through verified binding;
2. binding-manifest and GLB-identity agreement;
3. evidence-to-highlight;
4. stale or unresolved component behavior;
5. deterministic child variant;
6. parent unchanged;
7. unit-aware comparison;
8. design-history navigation.

## 10. Registry reconciliation

The 081 definition PR must update `docs/specs/STATUS.md` in the same commit series.

### 10.1 Canonical ID form

All spec IDs use the registry gate's canonical three-digit form, optionally followed by the existing
single-letter suffix:

```text
081
082
083
...
093
006b
058c
```

Unpadded aliases such as `82`, `89` or `93` are forbidden in registry identity and normative queue
references.

### 10.2 New rows

Add planned rows for:

- 081;
- 082;
- 083;
- 084;
- 085;
- 086;
- 087;
- 088;
- 089;
- 090;
- 091;
- 092;
- 093.

Only 082 is first in the selected post-G0 sequence. No new row becomes `ready` merely because 081
merges.

### 10.3 Existing rows

The registry reconciliation must:

- keep 006b blocked through Phase 5 and permit only fresh Phase-6 re-derivation;
- update 029 dependencies to secure persistence, shell and thread provenance;
- cancel standalone 030 and 037 and absorb their valid responsibilities into 090/091;
- remove live dependencies on cancelled 030, 037 and 058;
- re-derive 035 around shell, 050/051, Lineage and Runs;
- re-derive 054 around shell, Engineering Data and Lineage without 062 UI;
- cancel monolithic 058 while preserving its objective in 070, 083, 091 and 029;
- re-derive 058b and 058c as separate Phase-6 slices;
- keep 062 blocked with the non-blocking operator-design rule;
- keep 066–068 frozen;
- unfreeze 070 only for fresh re-derivation after 082;
- preserve 072/074 as merged alternative-topology work, not the canonical default;
- keep 078 planned;
- keep 080 frozen;
- reserve 081–093 in canonical padded form;
- leave no active row dependent on a cancelled row;
- leave the dependency graph acyclic.

### 10.4 Queue authority

`STATUS.md` must identify this queue as binding while preserving the ordinary spec lifecycle.

No implementation may skip directly from `planned` to active work.

## 11. Architecture constraints

### 11.1 State ownership

The implementation must begin without Redux or another global state library.

Initial ownership:

- URL: workspace, stage, mode and primary subject;
- shell reducer or context: rail, navigator, sidecar and dock visibility;
- server responses: API query state;
- component state: ephemeral form and viewer-tool state;
- localStorage: appearance and non-authoritative visual preferences only.

Adding a global store requires demonstrated duplicate state, race behavior or impossible cross-route
coordination.

### 11.2 API architecture

The frontend must have one request boundary and domain modules.

It must support:

- typed responses;
- structured backend errors;
- timeout or abort;
- no local page-specific `fetch`;
- no second data layer;
- no secret storage;
- no direct provider or filesystem access.

### 11.3 Primary attention

At most one object is dominant:

- model;
- result;
- record;
- review;
- future flowsheet.

Secondary surfaces yield space.

Default composition:

- thin rail visible;
- primary stage dominant;
- project navigator closed or compact;
- analysis dock closed;
- one context sidecar;
- inspector open only with a meaningful selection.

Jarvis and Inspector share the context sidecar by default.

### 11.4 Authority vocabulary

The UI must distinguish:

```text
accepted
proposed
rejected
superseded
fresh
stale
invalid
running
failed
diagnostic-only
```

Historical execution status and current freshness remain separate.

A valid BLUECAD candidate must not be rendered as an approved design.

## 12. Acceptance criteria for 081

Spec 081 is complete only when:

1. the baseline SHA and verification evidence are recorded;
2. the product decisions in section 3 are merged;
3. 081 is registered as a definition-only umbrella;
4. 082 is first in the post-G0 sequence;
5. 070 is explicitly unfrozen for fresh re-derivation after 082;
6. 066–068 remain frozen;
7. 080 is explicitly frozen for the queue;
8. 062 remains blocked with a design session that does not stall Phase 5;
9. 072/074 remain merged and are labelled alternative and non-default;
10. the future canonical topology is registered separately and remains outside the active queue;
11. monolithic 058 and standalone 030/037 are reconciled without live cancelled dependencies;
12. every queue item has a size classification;
13. every completed frontend phase has one uniform screenshot checkpoint;
14. P0 has both technical and operator-visible evidence;
15. legacy-page continuity is binding;
16. A0/A1 selection identity is structurally separated;
17. AI evaluation signals follow actual ledgers rather than an invented generic retry count;
18. a controlled queue re-derivation path exists;
19. no implementation row is promoted to `ready`;
20. no runtime, frontend, backend, schema, dependency or workflow behavior changes;
21. registry parsing and dependency-cycle validation pass;
22. exact-head CI succeeds;
23. BLUECAD Real Tool Proof succeeds;
24. independent review finds no unresolved blocking issue.

## 13. Non-goals

Spec 081 does not:

- implement secure credential storage;
- implement frontend code;
- create the shell;
- modify runtime routes;
- create threads;
- create Settings;
- create a graph viewer;
- create scene semantics;
- unblock 006b;
- unblock 062;
- activate Hermes;
- implement 080;
- implement the canonical product topology;
- implement 078;
- implement Aspen-like editing;
- create screenshots;
- add dependencies;
- add migrations;
- create an implementation branch after merge;
- permit multiple simultaneous implementation fronts.

## 14. Stop conditions before merge

Stop the 081 definition work and report before merge if:

1. `master` moves materially and invalidates the audit;
2. a newly merged spec claims ownership over the same queue or files;
3. the registry cannot remain acyclic;
4. reconciliation leaves an active row dependent on a cancelled row;
5. 082 cannot be separated from the Settings UI;
6. unfreezing 070 would implicitly unfreeze Hermes;
7. 072/074 would need runtime mutation to classify them as alternatives;
8. the queue accidentally authorizes Aspen-like, canonical-topology, 078 or 080 implementation;
9. deterministic gates fail;
10. a security or secret risk appears in the documentation change.

Post-merge non-implementability is handled only through section 8.4, not through silent queue drift.

## 15. Result of merging 081

Merging 081 means:

- frontend-beta reconstruction is the selected product direction;
- the implementation queue is binding;
- secure credential persistence is the first implementation front;
- one implementation front at a time remains mandatory;
- existing engineering and AI authority boundaries remain unchanged;
- 066–068 and 080 remain frozen;
- 062 does not stall Phase 5 but remains blocked;
- no frontend implementation has yet started;
- no spec may skip its own definition, readiness, implementation and review gates.
