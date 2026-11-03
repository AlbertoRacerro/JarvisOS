# 100c canonical queue re-derivation — 2026-08-28

Exact source master: `f50eb0a1f5b246d5e6f6eabab3a033e9a8c5a5c5`.

Authority: definition/queue re-derivation only. No runtime implementation is authorized by this document. Every new row remains `planned` until its own kernel/full-spec/readiness lifecycle makes it `ready`.

Primary evidence: `docs/audits/100c-final-product-direction-overlap-audit-f50eb0a.md` (not carried into this repository), the final operator capability matrix, final interaction contract, final visual implementation pack, PD-01..PD-08, merged 100f/100g composition and merged 100a/100b audit/cleanup evidence.

## Binding post-100c order

The order below implements the maintainer priority: Jarvis common foundation first; Project Knowledge backend; Development backend; Coding backend; incremental Jarvis domain actions; Design/Process last.

`100c` is definition-only queue authority, not a runtime prerequisite. New implementation rows cite the merged 100c planning artifacts as governing authority but MUST NOT list `100c` as a hard `Depends on` row, because definition-only authority rows conventionally remain non-implementation (`planned`) and would otherwise make the registry gate impossible to satisfy.

1. **111 JARVIS-CONTEXT-ACTION-FOUNDATION-1**
   - Depends on: 040, 042, 059b, 061a, 061b, 090, 091, 097.
   - Scope kernel: stable workspace identity, route descriptor, exact selected refs with id/version/ref, explicit removable added-context refs, inspected preview + digest, provenance/source manifest, fail-closed stale refs, generic capability/action registry, Jarvis service/policy adapter over the current AI runtime.
   - Hard line: no page-specific business logic, no second orchestration store, no Hermes runtime, no canonical domain COMMIT/EXECUTE.
   - Promotes/absorbs: generic parts of 031/034/036 and the common part of FV-B11/FV-B16.
   - Kernel drafting step: derive from exact post-100c master before any implementation.

2. **112 PROJECT-KNOWLEDGE-CORE-1**
   - Depends on: 001, 035, 040, 042, 050, 051, 071b, 098, 111.
   - Scope kernel: canonical Project Basis CRUD/write intent; model change sets; working revisions; deterministic impact/revalidation; atomic reconciliation; proposal vs operator mutation boundary; immutable parent/history; no second engineering store.
   - Promotes: FV-B01/B03/B04/B05 and the useful final-product responsibilities previously proposed by 101.
   - Frontend obligation: activate only truthful existing 100f Project Basis/Models controls necessary for this owner; no visual redesign.

3. **113 MODEL-DOSSIER-1**
   - Depends on: 112 plus existing modeling/runs/evidence/files owners.
   - Scope kernel: exact model/version/revision read dossier with bounded pagination/disclosures and exact run/result/artifact/source identity. Read-only aggregation; no new canonical model store.
   - Promotes: FV-B02 and FV-F04 read obligations.

4. **114 LITERATURE-KNOWLEDGE-1**
   - Depends on: 040, 042, files/source owners, 112.
   - Scope kernel: structured source/document/import/claim/datum/citation/location/used-by provenance bridged to existing `source_ref`; bounded safe preview/open; research/extraction remains proposal-only.
   - Promotes: FV-B06/FV-F05.

5. **115 PROJECT-SEARCH-1**
   - Depends on: 112, 113, 114 and existing FTS/search infrastructure.
   - Scope kernel: literal/structured project search returning exact owner/type/version/provenance; read projection only. Semantic retrieval remains proof-gated and trigger-deferred.
   - Promotes: FV-B07 and search portions of Memory references; absorbs future useful part of 064 only after evidence.

6. **116 ROADMAP-CALENDAR-1**
   - Depends on: 111 and existing events/workspace/decision infrastructure proven during definition.
   - Scope kernel: stable Roadmap work-item identity and deterministic status/done-when/dependency gates; separate real Calendar time-allocation entities with exact date/time/time-zone and zero/one/many links to Roadmap items.
   - Promotes: FV-B08/B09 and FV-F06/F07.
   - Hard line: no Board store; Timeline project window != occupied Calendar time.

7. **117 BRAINSTORM-1**
   - Depends on: 111 and existing files/events/proposal/decision boundaries proven during definition.
   - Scope kernel: immutable RAW identity/content, NEW/DISCUSSED/RECONCILED/SUPERSEDED lineage, reconciled revisions, discussion synthesis provenance, explicit promotion proposals to Roadmap/Design/Coding.
   - Promotes: FV-B10/FV-F08.
   - Speech capture remains trigger-deferred and is not part of baseline 117.

8. **118 CODING-REPOSITORY-TRUTH-1**
   - Depends on: 111 plus exact server-side GitHub/repository integration selected at definition.
   - Scope kernel: remote repository/ref/SHA/branches/PR/check/review truth; bounded Repository Inspector path/literal/ID search and safe artifact preview with exact ref/path/blob; architecture/spec/code/test/config/workflow/SVG inspection; exact safe GitHub URLs.
   - Promotes/merges: FV-B13/B14/B17 and FV-F10/F15 read obligations.
   - Hard line: no frontend GitHub token and no duplicate repository truth/index unless minimum-necessary proof succeeds.

9. **119 CODING-RUNTIME-TRUTH-1**
   - Depends on: 118 and exact local observer boundary selected at definition.
   - Scope kernel: local installation/worktree path, executed SHA/version/branch, dirty state, service/build/runtime health, approved remote target, aligned/local-behind/divergent/unknown derivation, evidence-backed semantic delta.
   - Promotes: FV-B18/B19 and FV-F11 read obligations.
   - Hard line: observation only; no update/restart mutation.

10. **120 DEVELOPMENT-PIPELINE-STATE-1**
    - Depends on: 118 and current spec/PR/gate process owners.
    - Scope kernel: inspectable `Proposal -> Plan -> Implementation -> Tests -> Independent Review -> Reconciliation -> Merge` state with exact repository/head and stale-gate invalidation.
    - Promotes: FV-B15 and active-development portions of FV-F10.
    - Hard line: no hidden auto-merge or second queue authority.

11. **124 PROVIDER-SETTINGS-GENERIC-1**
    - Depends on: 015, 018, 021, 059b, 061a, 082, 094, 111.
    - Scope kernel: provider/integration-scoped credential/config/status/test/catalogue/usage/budget/system projections where supported, reusing secure storage/provider/egress/policy owners.
    - Promotes: FV-B12/FV-F09.
    - Hard line: provider API identity remains distinct from coding-tool integrations; no model-scoped keys.

12. **121 JARVIS-PROJECT-KNOWLEDGE-ACTIONS-1**
    - Depends on: 111–115.
    - Scope kernel: exact explicit context refs and stale-safe CONTEXT/PROPOSE actions over Project Basis, Models and Literature; bounded previews/digests/source manifests; no direct domain COMMIT.

13. **122 JARVIS-DEVELOPMENT-ACTIONS-1**
    - Depends on: 111, 116, 117.
    - Scope kernel: explicit multi-record context basket; scheduling/reconciliation/Roadmap/promotion proposals; browsing/opening remains context-neutral; domain acceptance remains 116/117 authority.
    - Promotes: FV-B11 and Jarvis portions of FV-F06/F07/F08.

14. **123 JARVIS-CODING-ACTIONS-1**
    - Depends on: 111, 118, 119, 120.
    - Scope kernel: inspect/explain; Add to Jarvis context; Suggest modification as proposal/diff/plan only; development-proposal creation; deterministic check requests only through accepted server-side development authority.
    - Promotes: FV-B16/FV-F13 and Jarvis portions of Repository/Runtime.
    - Hard line: no direct file mutation from `Suggest modification`.

15. **125 SAFE-SELF-UPDATE-1**
    - Depends on: 119, 120.
    - Scope kernel: exact target eligibility; dirty refusal/reconciliation; state preservation; fetch/migration/build/smoke/restart/health; known-good rollback and evidence.
    - Promotes: FV-B20/FV-F14.
    - Hard line: remains separate from observation and terminal authority.

16. **126 LOCAL-TERMINAL-PTY-1**
    - Depends on: 118, 119, 123 and a full explicit local security/readiness proof.
    - Scope kernel: typed local PTY/session boundary, PowerShell-default Windows adapter, validated cwd/path, scrubbed environment, secret-safe/redacted display boundary, stdin/Ctrl+C/resize/exit/session lifecycle, high-risk command confirmation, bounded Jarvis-context export.
    - Promotes: FV-B21/FV-F12.
    - Readiness gate: arbitrary PTY streaming remains unavailable unless no-secret frontend delivery and local-only security are deterministically proven.

17. **102 ENGINEERING-EVIDENCE-CONTRACT-1 — REDERIVE/REORDER**
    - Preserve 044/077 evidence authority; rederive the planned generalized evaluator/evidence metadata only after the operator knowledge/development/coding foundations above are stable.
    - Do not make 102 a prerequisite for early Project Knowledge when existing exact evidence is sufficient.

18. **103 PROCESS-UPSTREAM-BAKEOFF-1**
19. **104 PROCESS-STACK-STRANGLER-1**
20. **105 ENGINEERING-DOMAIN-CLEANUP-1**
21. **106 ENGINEERING-EVALUATOR-1**
22. **107 PBR-EVALUATOR-1**
23. **108 DESIGN-STUDY-CONTROLLER-1**
24. **109 PROCESS-CAD-HANDOFF-1**
25. **093 PRODUCT-TOPOLOGY-0 — REORDERED AFTER 109**
26. **110 MULTIFIDELITY-ENGINEERING-1**

Rows 103–110/093 preserve their existing zero-sunk-cost/upstream/evaluator/CAD intent but are intentionally last. Exact dependencies must be revalidated when each kernel is refreshed; Process visual affordances remain truthful unavailable until these owners land. BLUECAD merged capabilities remain preserved and are not reimplemented.

## Rows superseded or removed from the active binding order

- **101 CANONICAL-STATE-WRITE-1:** superseded as standalone by 112. Its valid 040/042/071b/098 unification/lifecycle obligations are mandatory inputs to 112 rather than a parallel write layer.
- **055 Project view:** cancel/supersede as standalone; 100f/100g + 112–115 provide the final surface/read ownership without a second project store.
- **064 LIT-RAG-0:** remove from immediate binding order; semantic retrieval becomes a trigger-gated extension of 114/115 only after literal/structured search evidence proves need.
- **069 MEMORY-CONSOLIDATE-0:** remove from immediate binding order while Hermes 066–068 remain frozen; reopen only under an explicit Hermes release and reuse 111/domain proposal contracts.

Other planned/deferred rows retain the exact dispositions in `docs/audits/100c-final-product-direction-overlap-audit-f50eb0a.md` (not carried into this repository); absence from the binding execution order is not cancellation unless explicitly stated.

## Deferred/triggered capabilities

- FV-B22 Brainstorm speech: after 117, only with bounded media handling and canonical AI job/ledger inference path.
- FV-B23 semantic architecture graph: only if repository artifact inspection proves insufficient; current SVG/Markdown remain 118-readable.
- 053 decision dossier export: after stable 112–115 ownership, when a concrete thesis/advisor/investor/IP/grant export need is ready for a fresh spec.
- 063 local vault/vector working layer: only after 115 proves literal/structured search insufficient and conflict/authority semantics are explicitly specified.
- 011/026/032 personas/board sessions, 012 free scripts, 013 plugins, 014 CFD, 023 corpus, 025 routing promotion, 027 modal/thermal, 033 reusable part kinds, 039 frontier provider, 046 alternative design loop, 065 provider-family diversification remain trigger-gated per their existing rows/audit.
- 066–068 and 080 remain frozen.

## Promotion requirements for every new row

Before any row above becomes `ready`, its definition/full spec/readiness must cite:

- exact current master and current owner code;
- merged 100c planning artifacts as governing definition authority (citation only, never a hard status dependency);
- applicable PD contract, capability-matrix rows and interaction-contract action classes;
- applicable canonical HTML path/hash/blob/viewport for user-facing work;
- exact state/write owner and proof no second truth store is introduced;
- stale/concurrency/failure behavior;
- deterministic tests and, for visible changes, exact-head browser proof states;
- explicit deferred owner for every canonical control not activated by that slice.

No row is implementation-authorized merely because it appears in this re-derivation.
