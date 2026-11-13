# Roadmap proposal — Bubu legibility and the COMSOL-like engineering workbench (2026-10-30)

Status: **maintainer-directed roadmap; implementation authority remains in [STATUS.md](../specs/STATUS.md).**
PR #1016 preserved the proposal; the 2026-10-30 Codex desktop follow-up (directive `L20261030183013-6180`)
requests autonomous preparation and continuation through final operator testing, with Terra/Luna workers.
The coordinator may prepare and accept bounded contracts under the existing execution protocol without another
ordinary maintainer approval. `planned` is still not implementation authority: establish truthful readiness first.
Prepared by Claude (Opus 5.5) from a live test of Bubu and a read-only audit of master `c2945ee5`.

Maintainer priorities (2026-10-30): (1) Bubu must explain how to use the app when something can't be found, and the
whole system must become easier to read; (2) move toward a COMSOL Multiphysics-like workbench: direct 3D geometry in the
viewport (COMSOL / Fusion / AutoCAD style) instead of only Studio scripts, 3D CFD views (including extending a
symmetric partial simulation to the whole object), and more backend capability surfaced in the frontend.

## 1. Bubu legibility — live test findings and fixes

Live app, case study C1, Design › CFD, local Gemma 4 12B (BUBU-3 app map active):

| Question | Result | Problem |
|---|---|---|
| "How do I see the turbulent CFD results of this tube?" | Correct path (tree → component → Results → "Compare runs"), 12 s | Pointed at `design.tree.component.c1-horizontal` instead of `c1-cfd` |
| "Show me where I click first." | Ring + arrow drawn on the tree row, × works | Same wrong component; bubble text read "Click first on." (directive stripped, no name) |
| "How do I go back to the main project?" | Correct (`Back to <project name>`), 8 s | Text read "Click the button to go back" — name missing |

Proposed quick wins (one small spec, frontend + prompt; no new authority):
1. **Readable replies.** Replace each `[[point:id|label]]` with the target's visible name (or the label) in bold
   instead of deleting it, so the text stays a sentence ("Click **C1-CFD** first").
2. **Tell the model what each component contains.** Tree rows get a short state in the page description
   (e.g. `C1-CFD · CFD: 8 runs, results ready`; `C1-horizontal · Process unit, no CFD`) from data the page already has.
3. **Few-shot pointing examples** in the app map (2–3 per area) and the BUBU-3 pointing eval run
   (`docs/implementation/evidence/bubu3-pointing/`, needs `JARVISOS_LLAMACPP_API_KEY`) before/after.
4. **Route escalation for "where/how" questions** to Relay/Claude only if the eval stays poor (subscription quota).
5. **System legibility pass** (separate small specs): one help line per Design stage ("what this stage is for"),
   consistent empty states (already the `empty` + dot rule), and a "Where am I?" Bubu answer built from the route +
   selection + app map, without calling a model.

## 2. Audit — what exists today (master `c2945ee5`)

**Backend → frontend coverage.** 308 routes; Design/components/versions, process drafts, studio, coding, threads are
almost fully surfaced. BlueCAD CAD-link 047/072 topology preflight/execute is not an implemented backend capability:
`backend/app/core/cad_link_schema.py` defines the `bluecad_cad_links` table and
`backend/app/modules/flowsheet/service.py` reads it for references, but there is no CAD-link preflight/execute route
or service in `backend/app`, and no frontend call. Genuine backend-only UI gaps include sensitivity labels and
derivative review (beyond approve), Relay context releases, local llama.cpp runtime control, Hermes status, flow
grading/cohorts, project-knowledge scalars/validation/snapshots, flowsheet invalidations, and bio-model set history.
(Count is string-match based; a few may be dynamic calls.)

**CAD.** build123d 0.11.1 in process (OpenCASCADE via OCP), typed builders/specs (`bluecad/builders.py`, `spec.py`),
Studio geometry scripts in a bwrap sandbox, STEP/STL/GLB/3MF export. Viewer `BluecadGlbViewer.tsx` (three.js 0.171,
OrbitControls, raycast selection, tint). **Selection only — no transform gizmo, no sketching, no parametric feature
history** (the "tree" is candidates/attempts/artifacts + design components).

**CFD.** `bluecad/cfd_pipe/`: only `tube_run`; the domain is a **3D quarter bore** with `symmetry_y`/`symmetry_z`
planes (blockMesh written by `casegen.py`), laminar + RANS, OpenFOAM v2412 via the tool registry. Only the z=0 axial
section is exported (ASCII legacy VTK) and drawn as a 2D SVG heatmap (`CfdFieldSlicePanel.tsx`). **No 3D field export**;
symmetry is implied by the template, not stored on the run.

## 3. COMSOL-direction workbench — proposed specs (in order)

**W1 · CFD 3D results view (smallest step with high value).**
- Export, per finished run, the wall surface + both symmetry planes + a few axial sections as VTK PolyData (or VTP),
  plus explicit symmetry metadata on the run record (`symmetry: {mirror_planes: ["y=0","z=0"], axis: "x"}`).
- three.js viewer: colour-mapped surfaces/sections, legend, probe; **mirror the quarter ×4 to show the whole pipe**
  ("virtually extended" badge, never presented as computed data); later revolve for axisymmetric wedges.
- Optional streamlines/iso-surfaces after a full-volume export is decided (size budget).
- Reuses the Results node and the run comparison (stack 3D views like the 2D blocks).

**W2 · Parametric feature history (data model first).**
- A geometry sequence like COMSOL's (and Fusion's timeline): ordered features (`primitive`, `sketch`, `extrude`,
  `revolve`, `boolean`, `fillet`, `pattern`, `transform`, `parameter`) stored per design component version,
  regenerated by build123d into STEP/GLB; deterministic, versioned, diffable; Studio scripts stay as an advanced path
  and can be "a feature".
- Parameters shared with Process units (diameter, length…) so CFD/Process stay linked (obsolescence already exists).

**W3 · Direct 3D modelling in the viewport.**
- Primitives (box, cylinder, tube, sphere) placed with the mouse; three.js TransformControls gizmo (move/rotate/scale)
  writing back `transform` features; sketch on a picked plane/face (lines, arcs, constraints minimal) → extrude/revolve;
  boolean union/difference; dimension edit in the inspector; undo = feature history.
- An AutoCAD-like command line (typed commands with coordinates) and Bubu as the natural-language front end
  ("make a 50 mm tube 2 m long") that proposes features the user accepts.
- Reverse-engineering scope: public documentation, UX conventions and file formats of COMSOL / Fusion / AutoCAD —
  never proprietary code or assets.

**W4 · Backend coverage in the UI.** Distinguish genuinely backend-only UI gaps from capabilities not yet implemented
in the backend. CAD-link topology preflight/execute is the latter, not an existing backend surface to expose and not
the starting point for a UI slice; begin with a confirmed backend-only UI gap such as sensitivity review.

**W5 · Later physics.** Generalise `cfd_pipe` beyond `tube_run` (bends, the PBR module), FEM (CalculiX) on the same
feature-history geometry, optics coupling — after W1–W3.

## 4. Third-party licence inventory

- Separate processes and user installation do not by themselves establish licence compliance. Audit the exact
  versions, licences, linking/communication and packaging of each dependency, including build123d/OCP/OpenCASCADE and
  optional engines; preserve upstream notices and identify unresolved terms.
- Earlier GPL/LGPL/permissive classifications were preliminary leads, not a verified licence inventory. Hermes' licence
  and the empty `license:` fields in `configs/bluecad_tools.yaml` need verification. Spec 239 tracks this non-urgent
  investigation.

## 5. Candidate providers (unqualified proposals, 2026-10-30)

- **Copilot as a candidate subscription capability.** Spec 238: qualify the Copilot CLI as a Relay/supervisor agent
  next to `claude`, `codex`, `argon` (`scripts/relay_continuity.py` `AGENTS`, quota/cooldown handling like
  `relay_argon.py`). Gates: (1) written check of the Copilot terms for unattended/agentic CLI use; (2) quota model
  (monthly premium requests per model) mapped to the supervisor's cooldowns; (3) sandbox and egress identical to the
  other agents; (4) measured latency and success on a few authorized bounded turns before it is allowed to take
  missions. Qualification does not assign a permanent vendor role or override the current coordinator allocation;
  failed qualification leaves the existing providers usable.
- **GitHub Models as a rate-limited fallback provider** for Jarvis/Bubu (candidate prototyping tier; terms and credential scope unverified).
  Spec: an OpenAI-compatible adapter in the provider registry with explicit rate-limit handling, the same egress and
  sensitivity rules as any cloud route, never the default route, and a check that the tier's terms allow this use.
  Any product integration must use `run_ai_task` and create `ai_jobs`; `route_class="auto"` must never execute an
  external provider. Do not repurpose a repository credential for model calls or enable a fallback on quota exhaustion
  without an accepted credential, routing, budget and egress contract. This is a distinct qualification within 238.

## 6. Maintainer scheduling direction and autonomous delivery

1. Bubu legibility quick wins (§1.1–1.3) + run the pointing eval.
2. W1 CFD 3D results view.
3. W2 feature-history data model (spec + readiness), then W3 direct modelling in slices.
4. W4 coverage specs in parallel where independent.
5. Provider qualification (§5) and the licence inventory (§4) are secondary; an unavailable quota, optional account
   detail or legal review blocks only that lane, never Bubu or the workbench. Broader legibility (232) and verified
   coverage gaps (236) may proceed in disjoint slices alongside the workbench. Later physics remains after W1–W3.

The coordinator owns investigation, bounded spec/readiness, implementation, causal repair, appropriate review,
exact-head checks, merge/reconciliation and safe live activation under the existing protocol. Do not ask the maintainer
to approve routine implementation choices or each lifecycle transition. Ask for the final unaided test once the relevant
version is running and agent proof is complete; report that human result as pending until supplied. While waiting,
continue independent authorized work. Preserve genuine existing holds. Escalate only the constitution's spending,
missing human-controlled access, security/data-loss or no-practicable-safe-route conditions. A failing test first opens
diagnosis and repair, not an automatic request for the maintainer to debug it.

## 7. Coverage and handoff map

This table maps the request to canonical owners; it is not another status registry. Read live state and hard
dependencies in [STATUS.md](../specs/STATUS.md). The coordinator chooses mechanisms; W1–W5 above are design leads,
not permission to build a larger system than the accepted slice needs.

| Request | Canonical owner | Next acceptance work before implementation |
| --- | --- | --- |
| Bubu readable pointing, current component facts, examples and measured quality | 231 | Bounded accepted contract; obtain before/after local-model and clean-profile browser evidence, then live final test. No routing change. |
| General legibility, stage help, empty states and deterministic “Where am I?” | 232 | Audit actual remaining friction after merged 229/230; accept the smallest existing-surface slice, preserve navigation and visible scientific limits. |
| W1: CFD 3D and honest full-object symmetry display | 233 | Freeze exported field/domain provenance, units, coordinate frame and symmetry applicability; specify scalar versus vector reflection and probe provenance. Never mirror a non-symmetric solution or label reconstruction as newly computed data. Bound artifact size and browser performance using a real retained run. |
| W2: parametric feature history | 234 | Freeze version/parameter ownership, deterministic regeneration, failure atomicity and existing geometry compatibility; no second geometry authority. Decompose the first useful sequence before expanding the operation catalogue. |
| W3: direct viewport modelling | 235 | First useful geometry created/edited through visible controls without Studio; undo/version lineage and stale downstream results proven. Add sketching/constraints and command-line or Bubu proposals only in bounded accepted follow-ups. |
| W4: backend capabilities missing from UI | 236 | Verify each candidate against endpoint and visible navigation evidence; distinguish an unimplemented capability such as CAD-link topology preflight/execute from a genuinely backend-only UI gap. Select useful UI gaps, do not expose all endpoints indiscriminately, and do not authorize CAD-link implementation by planning alone. |
| W5: later physics/coupling | 237 | After W1–W3, choose a concrete user calculation with numerical validation and physical validity limits; no claim that separately proven solvers form a coupled chain. |
| Copilot and GitHub Models | 238 | Separate terms/account/quota/sandbox qualification from product adapter authority; no provider enabled by planning alone, no dependency of the primary lanes. |
| Third-party licence inventory | 239 | Non-urgent exact-version licence/notice and distribution-boundary inventory; no redistribution commitment. |

Spec 224 is merged only for its accepted owner-surface slice. Its deferred guide runtime and spec 225 computer use
are not prerequisites for 231 and are not authorized by this roadmap. Draft PR #966 must be reconciled against
ADR-061 before any reuse; a green historical CI does not establish current scope or readiness.
