# Frontend conformance contract — approved operator workstation — 2026-08-27

Status: maintainer-approved future implementation contract; visual authority only, not queue/runtime implementation authority.

## Purpose

Remove implementation discretion from the final visual pass. Future builders are expected to implement the operator workstation represented by `APPROVED_OPERATOR_UI_MANIFEST_2026-08-27.md`, not merely produce a visually related redesign.

The canonical HTMLs own visual/composition fidelity. `FINAL_OPERATOR_INTERACTION_CONTRACT_2026-08-27.md` is the mandatory companion for user-visible interaction/state-transition fidelity. A future implementation is not conformant if it matches the screenshot/layout but changes the approved action class, expansion/context behavior, state transition or authority boundary.

## Mandatory implementation rule

For a surface listed in the manifest, the canonical HTML is the implementation target at its reference viewport. The frontend may translate the prototype HTML/CSS/JS into React/Vite components and may reuse shared production components, but the resulting rendered surface MUST retain the same approved composition.

The following are binding unless a later maintainer-approved reference explicitly changes them:

- primary and secondary information architecture;
- panel order, relative scale and visual hierarchy;
- which information is persistent versus contextual/collapsible;
- normal typography roles, density and hierarchy;
- colors, border/radius language and structural depth;
- control grouping and normal action placement;
- which interactions expand inline, open a modal/popover, select an item, or move context into Jarvis;
- the semantic action class of visible controls as frozen in `FINAL_OPERATOR_INTERACTION_CONTRACT_2026-08-27.md` (`PRESENTATION`, `READ`, `CONTEXT`, `PROPOSE`, `COMMIT`, `EXECUTE`, `NAVIGATE`);
- which destinations do not exist as peer pages;
- the semantic distinction between remote repository state, local runtime state, project memory, project planning and actual calendar time.

An implementer MUST NOT substitute a different dashboard pattern, generic component-library layout, additional peer navigation, card grid, wizard, Kanban board, architecture-first screen or settings information architecture because it is easier to implement.

## Allowed translation from prototype to production

The following differences are allowed without a new visual approval when they do not change the rendered/semantic composition:

- replacing inline prototype CSS with production CSS/modules/tokens;
- extracting React components;
- replacing fixture strings/numbers with truthful backend values;
- using bundled official Phosphor assets instead of the prototype CDN;
- accessibility markup, ARIA, keyboard/focus support and semantic HTML improvements;
- loading/error/empty states required by the owning spec, provided they preserve the approved geometry where practical and never fabricate success/state;
- responsive layout changes outside the reference viewport that preserve the same hierarchy and capabilities;
- virtualization/pagination/internal scrolling required for scale where the approved product contract explicitly requires bounded surfaces;
- implementation-only wrappers required for routing, data fetching or state management that do not appear as new product concepts.

## Forbidden reinterpretations

Without an explicit maintainer-approved reference update, builders MUST NOT:

- remove an approved visible function because its backend is not implemented yet;
- change an approved action from `PROPOSE` to direct `COMMIT`, from explicit `CONTEXT` to implicit browsing context, from `EXECUTE` to simulated frontend state, or otherwise change its semantic class because that is easier to implement;
- invent frontend-only records, validation, health, confidence, version alignment, repository status, source provenance or model results;
- add `Home` or restore legacy primary destinations such as standalone Results, Runs, Evidence, Review, Lineage, Files or Board;
- expose a permanent architecture graph in Coding Repository instead of Repository Inspector;
- turn Calendar into a second visual rendering of Roadmap date spans;
- make opening a Brainstorm record silently add it to Jarvis context;
- make `Suggest modification` directly save repository files;
- present latest GitHub as the locally executed Runtime version;
- expose frontend-direct GitHub, provider, filesystem, shell or process authority;
- auto-execute Jarvis terminal commands by default;
- use LLM confidence or prose as a substitute for deterministic validation where deterministic validation is required;
- overwrite reconciled model history to simulate working revisions;
- make a rendered architecture SVG authoritative merely because it is visible.

## Shared-shell normalization

Every normal workspace uses the final primary rail:

`Design | Memory | Development | Coding | Settings`

This later cross-surface decision supersedes stale embedded shell fragments in older canonical HTML files. It does not license any other change to their screen-local composition.

The normal peer destinations are fixed:

- Design: `Process | BLUECAD`
- Memory: `Project Basis | Models | Literature`
- Development: `Roadmap | Brainstorm`; Roadmap: `Timeline | Calendar`
- Coding: `Repository | Runtime`
- Settings: `Appearance | AI | System`

## Data truth and unavailable behavior

A prototype may display fixture content to demonstrate the intended layout. Production MUST use real data or a truthful unavailable/loading/empty state.

If the UI is implemented before the backend owner exists, the builder has only these valid choices:

1. omit activation while retaining the approved affordance in a clearly disabled/future/unavailable state if the active spec permits frontend-first delivery; or
2. wait for the backend/read-model owner.

The builder may not replace the capability with a simpler unrelated behavior or silently change its action class. The capability matrix, interaction contract and eventual canonical specs decide the owner.

## Interaction fidelity

Visible interactions in the canonical references are product requirements, not decorative prototype behavior. Examples include:

- Literature inline expansion with multiple records open;
- Models disclosure sections and bounded detail;
- Project Basis bounded proposal approval, working-revision transition and deterministic revalidation versus genuine recomputation-required state;
- Roadmap execution-status collapse/expansion and same-item state transitions;
- Calendar event selection and add/edit surface;
- Brainstorm inline expansion and explicit context basket;
- Repository Inspector search/result/preview selection;
- Repository `Suggest modification` proposal flow;
- Runtime local-vs-GitHub version inspection and semantic delta drill-down;
- future terminal Insert/Copy versus explicit execution and secret-safe backend-mediated PTY behavior.

Production implementations may change internal event/state code, but must preserve these user-facing behaviors unless the active canonical spec explicitly narrows them for a staged delivery. A staged slice must leave a clear later owner for every deferred approved behavior.

`FINAL_OPERATOR_INTERACTION_CONTRACT_2026-08-27.md` is the definitive interaction/state-transition companion when the HTML demonstrates composition but does not itself make backend action authority unambiguous.

## Browser evidence gate

For each materially implemented manifest surface, the implementation PR must provide browser proof at the canonical reference viewport. Evidence must be tied to the exact PR head and include the normal/default state plus every interaction state owned by that slice and needed to prove the slice's acceptance criteria.

Reviewers should compare against the canonical HTML itself, the final interaction contract, and the most-specific surface reference—not memory or a screenshot embedded in a chat. A mismatch in panel hierarchy, navigation, geometry, typography role, component treatment, approved interaction or action authority is a conformance finding even if the new UI is aesthetically acceptable.

## Backend completion obligation

Visual completion is not product completion. Every approved visible action/state is tracked in `docs/spec-drafts/FINAL_OPERATOR_CAPABILITY_MATRIX_2026-08-27.md` (not carried into this repository); every approved interaction/state-transition family is preserved in `FINAL_OPERATOR_INTERACTION_CONTRACT_2026-08-27.md`. Missing capability is implementation work, not permission to redesign it away.

100c is responsible for re-deriving the minimum canonical spec ownership after 100a/100b. It may merge or reorder pseudo-specs to minimize semantic surface, but it MUST preserve every maintainer-approved capability and interaction unless it records an explicit maintainer-authorized rejection/supersession.

## Definition of visual/interaction conformance

A surface is conformant only when all of the following are true on one exact implementation head:

1. manifest/reference identity is recorded;
2. reference viewport browser evidence exists;
3. final shared-shell overlay is respected;
4. screen-local composition matches the approved HTML;
5. action classes and state transitions match the final interaction contract;
6. no fixture/fabricated state is passed off as runtime truth;
7. all functional deferrals have an explicit backend/frontend owner rather than being silently dropped;
8. accessibility and keyboard behavior do not regress the approved interaction model.
