# PROOF_AND_ACCEPTANCE — JarvisOS 100

This document tells the 100 definition/readiness what must be visually and deterministically proven. It does not by itself mark spec 100 ready.

## Required canonical proof surfaces

Visual identity must be judged on **real dense product surfaces**, not a styleguide-only page.

### Proof A — BLUECAD / Model

Use a state with:

- real shell and primary navigation;
- BLUECAD workbench/viewport visible;
- an object/part selected;
- Properties populated;
- Jarvis sidecar visible with realistic existing content/action structure;
- Analysis Dock or equivalent bottom workbench content visible where current product semantics allow it.

This is the hardest test for CAD-like density, selection accent, technical viewport contrast, panel geometry and typography.

### Proof B — Process workspace scaffold

Use the merged 058d `/design/process` route with:

- Process stage visible;
- inert scaffold/empty-state semantics preserved;
- shell, sidecar and Analysis Dock relationships visible.

Purpose: prove 100 styles the future process-workstation **form** without inventing process nodes/streams/backend semantics.

### Proof C — Engineering Data or Runs dense data surface

Use a state with realistic rows, metadata, status and actions.

Purpose: prove:

- high-information density remains readable;
- status semantics survive the brand accent;
- tables do not become decorative cards;
- typography and row hierarchy work without microtype.

Prefer the denser of Engineering Data and Runs on the exact implementation head; additional proof of the other is welcome but not a substitute for the harder state.

### Proof D — Settings / appearance and accent

Show:

- light/dark/system appearance controls as applicable to the existing Settings contract;
- Microalgae, Leaf Chlorophyll, Lagoon and Custom accent choices;
- Custom color chooser + HEX value;
- Reset/default behavior;
- a live preview or immediately visible application of accent that makes the choice understandable without touching engineering semantics.

## Light is the primary acceptance surface

All A–D proofs must be captured in canonical **light** appearance. Dark must also be verified, but approval cannot be based primarily on dark screenshots because black/dark backgrounds can hide problems in spacing, elevation and material hierarchy that are central to this identity.

## Dark parity proof

At minimum verify the same implementation in dark for:

- shell/navigation;
- one dense engineering screen;
- Settings accent controls;
- focus/selected/status states.

Dark should feel like the same instrument in low light, not a different neon product.

## Accent proof

Verify all three presets plus at least two Custom seeds with materially different hue, including one warm hue such as orange.

Passing means:

- only accent-derived channels change;
- error/warning/success/proposed/stale/unavailable and other semantic colors remain stable;
- scientific/physical visualization colors remain unchanged;
- text remains readable;
- invalid persisted custom value safely returns to default;
- Reset restores Microalgae.

The orange custom proof is deliberately adversarial: if “orange JarvisOS” turns warnings, validation or scientific data orange, the architecture is wrong.

## Typography proof

Verify Instrument Sans at actual engineering density, not a hero heading only.

Required checks:

- 12px metadata remains readable;
- 13px labels are distinguishable from 14px body/data text;
- tabular numerals align repeated engineering values;
- long labels do not force destructive truncation compared with the pre-100 layout;
- IBM Plex Mono is restricted to code/log/path/hash/identifier contexts where it adds value;
- if Instrument Serif is tested, it appears only in a rare display/identity role and is removed if it looks editorial or impairs scanning.

No successful proof depends on proprietary third-party fonts.

## Geometry and material proof

Pass criteria:

- structural engineering panels read as aligned working surfaces, not a dashboard of detached cards;
- small shadow/elevation visibly separates panels from the mineral canvas without obvious dark halos;
- structural surfaces remain substantially opaque;
- pill shapes are not used as generic container language;
- softer radius/translucency is limited to floating/transient/Jarvis surfaces;
- any glass candidate remains legible over the hardest allowed content and has an opaque fallback;
- if glass adds visual noise or weakens text contrast, omit it rather than forcing the effect.

## Motion proof

Deterministic tests should verify timing tokens/branches; browser proof should verify feel.

Requirements:

- ordinary interactions complete within the bounded fast/standard timing system and never gate the action on animation completion;
- `prefers-reduced-motion` collapses non-essential motion;
- indefinite animation is present only while an actual pending state exists;
- loading indicators do not fabricate percentage/progress;
- Jarvis may have a distinct pending animation, but it stops when the pending state ends.

## Icon proof

- only Phosphor generic app icons are introduced by 100;
- no Lucide/Tabler generic icon dependency is present;
- normal icon weight/size is coherent within each control class;
- icon-only controls have accessible names;
- no generic icon is used to pretend process unit-operation semantics exist;
- custom engineering/PFD symbols remain out of scope unless separately authorized later.

## Accessibility/readability gates

At minimum:

- normal text meets WCAG AA contrast against its actual surface; target 4.5:1 for ordinary text;
- focus indication remains clearly visible in both appearances and does not rely on user accent alone when contrast would fail;
- status is never color-only;
- disabled/unavailable is distinguishable from active while still legible;
- keyboard navigation behavior is not regressed;
- reduced-motion behavior remains functional;
- transparency has an explicit opaque fallback rather than relying solely on `prefers-reduced-transparency`, whose browser support is incomplete.

## Semantic regression gate

The 100 diff must not redefine:

- route/stage meaning;
- canonical vs working/run/result authority;
- Jarvis proposal/confirm/reject authority;
- Engineering Data lifecycle semantics;
- BLUECAD selection/scene semantics;
- 058d Process scaffold semantics;
- evaluator/solver behavior;
- backend persistence or API contracts.

If visual implementation seems to require such a change, stop and re-derive scope rather than hiding the change inside 100.

## Visual anti-regression checklist

The final exact-head proof must be rejected if any of these are dominant:

- black-first app identity;
- neon/cyberpunk glow;
- petroleum/navy-teal cast;
- purple “AI” branding;
- large rounded SaaS card grid;
- excessive translucent structural panels;
- green body text or large green fills reducing readability;
- legacy engineering-software microtype;
- oversized whitespace that hides simultaneously useful information;
- literal natural/classical decoration.

## Definition/readiness output expected before implementation

The normal 100 lifecycle should turn this authority pack into:

1. a definition/kernel that states the invariant boundaries;
2. a full spec with exact touched surfaces/tokens/dependencies;
3. readiness evidence that the font/icon licenses and frontend dependency choices are acceptable;
4. deterministic test plan for theme/accent persistence, safe fallback, reduced motion and semantic non-regression;
5. browser/evidence plan covering A–D above.

Only then should runtime implementation begin.
