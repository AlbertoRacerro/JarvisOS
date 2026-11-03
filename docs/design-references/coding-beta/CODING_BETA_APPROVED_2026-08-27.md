# Coding beta — approved visual and interaction reference — 2026-08-27

Status: maintainer-approved visual/product reference; not runtime implementation authority.

This file freezes the approved `Coding` workspace composition reached during the 2026-08-27 maintainer visual pass, including the later Repository Inspector and local-vs-GitHub Runtime refinement. It supplements `docs/product-direction/04-coding-and-self-development-contract.md` (not carried into this repository). `docs/specs/STATUS.md` remains the live implementation registry.

## Shared Coding shell

Primary navigation remains exactly:

`Design | Memory | Development | Coding | Settings`

`Coding` owns exactly:

`Repository | Runtime`

The approved warm limestone / near-white operator shell is reused. Normal application typography follows the Inter / Inter Display direction already frozen by the maintainer, with regular/medium weights rather than heavy bold treatment. IBM Plex Mono is reserved for code, paths, hashes, ports and logs. Generic icons remain Phosphor-only where icons are needed.

## Repository — final refined direction

Final approved local HTML identity:

- `coding_repository_beta_mockup_v2_inspector.html`
- SHA-256: `afe3bf43eebc3da65e38aadcb27dcaac6f55b61959077cad82cbc51979b1d11f`

Approved rendered reference identity:

- `coding_repository_beta_mockup_v2_inspector.png`
- SHA-256: `d6b398833d05c35cfb9288f4284326388ae35127ae3fb1f4fc8db7ff30b8b3be`

The exact approved HTML is preserved beside this file as `coding-repository-beta-approved-2026-08-27.html`.

Approved composition and semantics:

- Repository represents GitHub remote/future software state, not the currently loaded local runtime;
- the top strip exposes repository identity, branch, exact/short SHA, remote-current status and working-tree cleanliness where truthfully available;
- the active-development card renders the canonical lifecycle `Proposal -> Plan -> Implementation -> Tests -> Review -> Reconciliation -> Merge`;
- software changes remain isolated in a branch/worktree. Jarvis never directly mutates the currently executed code path;
- architecture is **not** permanently visible as a dedicated graph surface;
- the lower dominant surface is a general `Repository Inspector`: one search/inspection entry point for specs, Markdown, architecture artifacts, SVGs, code, tests, configuration, workflows, images and other repository-readable artifacts;
- filtering may expose `All | Docs | Specs | Architecture | Code | Tests | Config` or equivalent presentation-only categories;
- Markdown supports rendered preview and raw/source inspection; SVG architecture artifacts render as diagrams only when selected; code/config receive appropriate syntax/structured previews; images may render directly where safe;
- selecting an artifact shows its exact repository path/ref and a bounded preview rather than navigating away from Coding;
- each selected artifact exposes `Add to Jarvis context`, `Suggest modification`, and `Open on GitHub` where a direct remote URL exists;
- `Open on GitHub` links to the exact repository file/ref and does not alter JarvisOS state;
- Jarvis active context may combine multiple files/specs/SVGs/PRs/modules so the operator can ask cross-document consistency questions;
- `Suggest modification` is proposal-only: the operator describes the intended change, Jarvis may prepare a diff/plan, and any accepted mutation still enters the normal isolated development lifecycle before merge;
- architecture remains fully supported as an inspectable/searchable artifact family rather than a permanently pinned page element;
- search may later become semantic in addition to literal/path/ID lookup, but must not invent repository facts or create a second truth store.

## Runtime — final refined direction

Final approved local HTML identity:

- `coding_runtime_beta_mockup_v2_divergence.html`
- SHA-256: `041b2f8974a1ad866ac5fad700c920c3a4816e6a0d6263a185e20c0ca421893e`

Approved rendered reference identity:

- `coding_runtime_beta_mockup_v2_divergence.png`
- SHA-256: `a59b8e0788807052600335fd5753783ca7c0728b4f10fa7714dfcc7168df1306`

The exact approved HTML is preserved beside this file as `coding-runtime-beta-approved-2026-08-27.html`.

Runtime is explicitly distinct from Repository. It represents the JarvisOS instance actually loaded/executed on the workstation.

Approved composition and semantics:

- local-vs-GitHub divergence is a primary first-screen concept rather than a secondary metadata row;
- the **actually executed local version is green** and clearly labeled `Local current` / `actually executed`, with local SHA/path/health/cleanliness where truthfully observable;
- the **latest approved GitHub/remote version is orange** and clearly labeled as newer remote state that is **not yet executed locally**;
- the UI must never imply that a newer GitHub SHA is already running merely because it exists on `master`;
- the comparison area exposes an explicit alignment state such as `aligned`, `local behind remote`, or an equivalent truthful state;
- when GitHub is ahead, Runtime exposes a semantic delta: concise descriptions of commits/features/contracts added after the local SHA, with the ability to inspect the full change set/commit/file evidence;
- semantic summaries must be derived from real repository differences and clearly distinguish runtime-affecting changes from docs/reference-only changes when possible;
- Jarvis may answer `what changed since my local version?`, compare commits/files, summarize added features and explain whether an update is safe;
- local services/health remain visible but subordinate to version alignment;
- migration/update phases no longer consume a large permanent pipeline. The guarded sequence remains authoritative but is compact/collapsed until the operator requests an update;
- the future update path remains `save state -> fetch approved target -> migration -> build -> smoke -> restart -> health -> rollback`, with dirty local changes blocking unsafe overwrite and failed post-restart health preserving rollback behavior;
- runtime logs retain essential evidence and full-log access without dominating the page;
- update/restart/deploy remains an explicit operator action under accepted runtime authority.

## Authority boundary

These references freeze approved visual composition and interaction intent only. They do not by themselves create Repository Inspector indexing, semantic search, Runtime comparison APIs, new state stores/process supervisors, or Jarvis commit/deploy authority. Real previews, health, SHAs, semantic deltas and GitHub links must be sourced from actual repository/runtime evidence.

When an approved HTML artifact and an incorrectly rendered screenshot disagree, the approved HTML/composition contract is authoritative.
