# DESIGN_SYSTEM_DECISIONS — JarvisOS 100

These are the maintainer-approved visual-system decisions that spec 100 should implement or make directly testable. Exact values may move slightly only when browser proof demonstrates a concrete readability/accessibility defect.

## 1. Typography

### Primary UI family — Instrument Sans

Use **Instrument Sans** as the primary UI/body/control typeface unless an implementation-level browser proof demonstrates a material legibility defect. It is open-source under SIL OFL 1.1 and explicitly combines precision with subtle playfulness, matching the requested rational-content / humanistic-shell split.

Upstream: `https://github.com/Instrument/instrument-sans`

Intended use:

- navigation, Properties, Jarvis prose, tables, forms, buttons, labels, headings;
- engineering numbers should normally remain in the UI family with `font-variant-numeric: tabular-nums` so the interface does not turn into a wall of monospace;
- prefer 400/500 for ordinary content and 600 for strong hierarchy; avoid broad use of 700.

### Technical mono — IBM Plex Mono

Use **IBM Plex Mono** for code, logs, hashes, paths, command/console surfaces, identifiers that genuinely benefit from character-by-character distinction, and other machine-oriented text. IBM Plex is open-source under OFL and designed for UI/technical use.

Upstream: `https://github.com/IBM/plex`

Do not use monospace merely to make ordinary engineering values look “technical”.

### Optional display serif — Instrument Serif

Instrument Serif is allowed only as a **rare brand/display accent** if the 100 browser proof shows it strengthens the JarvisOS identity without reducing scanability. It must never become the ordinary control/table/Properties font. If the proof does not clearly improve the product, omit it and keep the wordmark/display text in Instrument Sans.

Upstream: `https://github.com/Instrument/instrument-serif`

This is intentionally proof-gated rather than required.

### Target type scale

The existing scale should be tightened toward engineering density without returning to legacy microtype. Initial target values:

| Role | Target |
| --- | ---: |
| caption / metadata | 12px |
| control label | 13px |
| ordinary body / table content | 14px |
| section title | 16px |
| page/workbench title | 21–24px |

Body line-height should normally remain around 1.4–1.5. Dense tables may be tighter if row separation remains obvious. No required operating information should depend on text below 12px.

## 2. Density and layout rhythm

Target: **high-information, medium-density engineering UI**.

- Keep many relevant values visible simultaneously.
- Use grouping, alignment, typographic hierarchy and restrained whitespace before shrinking type.
- Compact control target: roughly 30px high.
- Default engineering control target: roughly 34–36px high.
- Do not inflate ordinary controls to mobile/SaaS dimensions unless touch/accessibility semantics require it.
- Preserve the current workstation architecture and relative role of navigator, canvas, sidecar and Analysis Dock; visual identity does not re-author product semantics.

## 3. Geometry

Initial token direction:

| Surface | Radius direction |
| --- | --- |
| dense structural panel/table/canvas frame | 6–8px |
| normal button/input | 6–8px |
| floating popover/command palette/Jarvis transient surface | 10–12px |
| chips/tags/toggles with pill semantics | pill allowed |

Avoid universal 16–24px card radii. A visible hierarchy of radii is part of the `bio-machined` language.

## 4. Light-first mineral surface model

### Canonical light baseline

The values below are implementation seeds, not third-party colors.

| Token intent | Seed |
| --- | --- |
| mineral canvas | `#F2F3ED` |
| main surface | `#FBFCF8` |
| raised surface | `#FFFFFF` |
| subtle chlorophyll-neutral surface | `#E9EEE7` |
| primary text | `#20251F` |
| secondary text | `#59645B` |
| muted text | `#758078` |
| quiet border | `#CFD8CE` |
| strong border | `#AAB7AA` |
| technical viewport dark mineral | `#121A16` |

These neutrals should feel slightly mineral/organic rather than blue corporate gray. Do not tint ordinary text green.

### Dark appearance

Dark remains a full supported alternative, but should use charcoal/mineral darks rather than pure black as the app-wide identity. Scientific/3D viewports may remain darker where contrast requires it. Dark is not the source from which light styling is reverse-engineered.

## 5. Accent presets and Custom

Expose four accent choices in Settings:

1. **Microalgae — default:** `#528B68`
2. **Leaf Chlorophyll:** `#5F8F52`
3. **Lagoon:** `#4F938A`
4. **Custom**

These are accent **seed colors**, not single global replacement colors.

Each seed must produce a bounded tonal family for at least:

- accent subtle background;
- accent surface;
- accent border;
- accent strong border;
- primary accent;
- hover;
- pressed/active;
- accessible foreground/on-accent where needed;
- focus/selection treatment.

Prefer perceptual derivation in **OKLCH**. Modern browsers broadly support `oklch()` and `color-mix()`; a CSS-variable approach using `color-mix(in oklch, ...)` is preferable to a new color-library dependency if it satisfies the required browser/test matrix. Provide deterministic fallbacks if the implementation supports older environments that need them.

### Custom control

Minimum-necessary 100 implementation:

- selecting Custom reveals a visible color chooser and editable HEX value;
- prefer the native `<input type="color">` plus a validated HEX field/preview before adding a third-party picker dependency;
- selected preference is a **local visual preference only** and may follow the existing appearance-preference localStorage pattern;
- invalid/stale stored values fail safely to Microalgae default;
- Reset restores the JarvisOS default.

No backend/canonical engineering state is created for accent preference.

### Accent channels

Accent may drive:

- current navigation/selection;
- focus ring and active borders;
- selected geometry or selected-row non-physical emphasis where existing semantics allow it;
- restrained panel-edge/surface tint;
- Jarvis presence and active/waiting detail;
- progress/active interaction detail;
- a primary graph series or selected series only when it does not overwrite engineering meaning.

Accent must **not** replace:

- danger/error red;
- warning amber;
- independently defined success/valid status tokens;
- proposed/stale/unavailable/etc. record-state semantics;
- CFD/FEM/scientific field colormaps;
- categorical series colors where distinct identities are required;
- any color that is already an authoritative domain signal.

Status must never rely on hue alone: pair with text/icon/shape where material.

## 6. Borders, elevation and shadows

The light UI should make aligned panels legible as separate working surfaces.

Recommended depth model:

- **canvas/background:** no shadow;
- **structural panels:** fine border + very small soft elevation;
- **raised/floating controls:** slightly stronger but still restrained shadow;
- **modals/command palette/popovers:** highest ordinary UI elevation.

Shadows should read as real Z-separation, not decorative floating-card spectacle. Avoid dark halos.

A useful initial direction is two elevation tokens, e.g. low structural and floating, rather than a large shadow scale.

## 7. Glass / translucency

Glass is a testable accent, not the base material.

Allowed candidates:

- command palette;
- transient tool palette;
- popovers;
- selected floating controls;
- a bounded Jarvis transient/proposal surface if readability is preserved.

Normally disallowed:

- dense tables;
- navigator/tree backgrounds;
- Properties body;
- Analysis Dock data grids;
- large structural canvas frames.

Every translucent treatment must remain readable over the hardest permitted content and have an opaque fallback. `prefers-reduced-transparency` may be honored where supported, but support is incomplete across browsers, so do not depend on that media feature as the only fallback.

## 8. Motion

Rule: **motion is like bass in a song — it should be missed when absent, not watched when present.**

### Ordinary interaction

- hover/press/focus/selection: roughly 80–140ms;
- panel/tab/popover transitions: roughly 120–180ms;
- no ordinary action should be delayed so an animation can finish;
- motion should primarily communicate continuity, hierarchy or state change.

Keep/extend `prefers-reduced-motion` so non-essential motion becomes effectively immediate.

### Real waiting states

When the user is already waiting for real work — AI response, solver run, artifact load, long computation — a longer looping/loading animation is allowed because it fills unavoidable latency rather than creating it.

Requirements:

- animate only while work is actually pending;
- never imply fake percent completion;
- ordinary loaders can remain simple;
- Jarvis may use a slightly more organic/pulsing presence, but never a distracting screen-wide effect.

## 9. Jarvis component language

Jarvis uses the same typography, icon family, spacing and core surfaces as the rest of the workstation. It may differ through:

- slightly softer floating geometry;
- subtle accent-surface modulation;
- restrained elevation/translucency where proof passes;
- a distinct real-waiting animation;
- clear proposal/confirm/reject hierarchy.

Do not create a separate “AI visual brand”.

## 10. Accessibility and comprehension

100 must preserve or improve:

- keyboard focus visibility;
- readable contrast in light and dark;
- non-color status signaling;
- reduced-motion behavior;
- hover/focus/selected distinctions;
- text readability at engineering density;
- clear disabled/unavailable state;
- chart/plot meaning independent of user accent where required.

Visual polish fails if it weakens engineering comprehension.
