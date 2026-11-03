---
name: jarvis-studio-visual-design
description: Apply an audited, style-only visual design process to JarvisOS Engineering Studio React views, covering tokens, typography, tables, charts, themes, motion, and before/after browser acceptance. Use for aesthetic refinement or art-direction work; exclude behavior, data, units, API, and scientific-contract changes.
---

# Jarvis Studio visual design (DRAFT, not yet adopted)

Status: draft for maintainer review under issue #834. It grants no authority to implement a redesign. A `ready` spec and maintainer approval of the visual direction are required first. Invoke explicitly by reading this file in the active Relay session; nothing is installed or auto-loaded.

Scope is style only: CSS, tokens, typography, spacing, iconography, motion, and presentational markup. Flows, data, calculations, physical units, permissions, API calls, simulation semantics, Edit/Test/Publish, Sidecar, workspace, and routing stay unchanged. A change that alters what a number means, which unit it shows, or whether an action is allowed needs separate authorization.

## 1. Preserve scientific semantics (hard rules)

1. Every physical quantity keeps its value, unit, and precision exactly as the owner API supplies it. Restyle the unit (smaller, muted, non-breaking space before it); never drop, abbreviate differently, or reformat it.
2. Uncertainty, provenance, freshness, and evidence class (for example measured, simulated, assumed, draft, published, pinned) must stay visible and distinguishable by more than colour: a text label or icon shape plus a colour. Do not demote them to hover-only.
3. Draft, Test, Publish, and pinned states keep their existing wording and ordering. Restyle may change how they look, not how many steps are visible.
4. Never smooth, round, interpolate, or clip data for appearance. Charts draw straight segments between samples; axis truncation is visible and labelled.
5. Missing is a muted dash, a true zero prints as zero, stale data shows its age, errors appear inside the failed block with what failed and when data was last good.
6. Do not invent content (decorative numbers, sparkline "trends", sample badges) to make a screen look full.

## 2. Direction brief (parameterize before designing)

Fill every field before touching CSS; record the filled brief in the PR. The default below is the maintainer's art direction of 2026-10-19; change only on maintainer instruction.

- Essence: a refined, futuristic fusion of human intelligence, nature, and advanced machinery; a technologically advanced sanctuary of nature.
- Heritage cue: subtle mythic/Hellenic architectural inspiration used as proportion and rhythm (a stable base, a clear shaft of content, a restrained entablature-like header band; evenly spaced vertical divisions as columns). A meander (Greek key) appears sparingly, only as a hairline border detail or a single empty-state motif, never as a repeating wallpaper.
- Palette: restrained natural greens (deep moss/forest and a lighter sage), mineral and glass surfaces (cool stone neutrals, faint translucency used purposefully), subtle bronze or gold accent for emphasis and focus only. One data hue per chart.
- Geometry: elegant, precise, mostly orthogonal; small consistent radii; hairline strokes; generous alignment discipline.
- Typography: strong and legible; a distinguished display or heading face with real character used for titles only, and a highly legible text face with true tabular figures for all data.
- Readability goal: exceptional scientific readability of numbers, tables, and plots outranks every stylistic motif.
- Avoid: fantasy ornament, columns or laurels as clip-art, excessive foliage or leaf textures, generic neon sci-fi (glowing cyan/purple gradients, grid-glow backgrounds), skeuomorphic marble or gold textures behind data.

Parameters to set per run: `accent` (bronze hex), `primary` (green hex), `surface-tone` (stone/glass), `ornament-budget` (default: one meander instance per screen at most), `density` (compact for engineering tables, comfortable elsewhere), `display-face`, `text-face`.

Worked-out checks for the default direction: Hellenic reads through proportion and spacing, not drawn temples; nature reads through colour and organic restraint, not imagery over data; machinery reads through precision, alignment, and mono/tabular detail.

## 3. Tokens before values

- Discover the existing token and theme owner first (`frontend/src/theme.ts` and the style sheets under `frontend/src/styles/`); extend it rather than adding a parallel system. Do not add a new dependency.
- Express every colour, size, space step, radius, border, shadow, duration, and z-index as a CSS custom property. A raw hex, pixel, or millisecond value in a component rule is a defect unless it is a one-off geometry value with a comment.
- Semantic layers: raw palette, semantic roles (surface, surface-raised, border, text-primary/secondary/tertiary, accent, focus, status-ok/warn/error/info, evidence-class colours), then component tokens. Components consume only roles.
- Two themes (dark and light) are defined from the same role names, not by inverting. Both must be reviewed; set `color-scheme` per theme so native controls follow.
- Scales: a spacing scale on a small base unit; a fixed rem type scale with a tight ratio (about 1.125 to 1.2), not fluid sizes, because tools are viewed at consistent DPI. Typical working sizes: at least 12px labels/axis, 13-14 body and cells, 16 card titles, 18-28 page titles. No data figure above roughly 40 px; app screens are not landing pages.
- Elevation is declared once: either a border or a shadow per surface level, not both. Use two or three neutral steps for panels (page, panel, raised).

## 4. Readability rules for numbers, tables, and plots

Numbers
- Use tabular figures everywhere numbers align or update; verify the chosen face actually changes digit widths with `font-variant-numeric: tabular-nums`, else pick another face.
- Right-align numeric columns and keep one right edge per row. Preserve the precision and formatting supplied by the data owner; change separators, minus signs, or unit placement only when the accepted contract allows it.
- Use `Intl` formatting only where it does not change the owner-supplied precision. Scientific notation, significant figures, and uncertainty ("1.234 +/- 0.005") stay as supplied.
- Only identity and the key figure take primary ink; secondary text is muted but never below contrast limits.

Tables
- Rules and alignment, not zebra stripes or header bands. Header: sticky, small, sentence case, with unit; hairline row dividers at low contrast; a hover and a selected state that are distinct and keyboard reachable.
- Give the identity column the most width, truncate with an ellipsis plus a full-value tooltip or title, never break inside a number.
- Dense tables raise row pitch and padding with type size together; do not shrink type while keeping padding. Virtualize very long lists only if the existing code already does.
- A changed or overridden cell carries a mark (dot or tinted border) and a named baseline.

Plots and charts
- Choose the form by the question: line for change over time, bars from a zero baseline for magnitudes, ranked horizontal bars for comparison, small multiples before overlaying more than three series. No 3D, no smoothing, no decorative fills.
- Few, faint gridlines (about 3-5), no heavy axis boxes, ticks at round steps, at most 4-8 flat x labels. Axis titles carry quantity and unit. Axis and label text is never larger than the data.
- One hue per chart for the main series; additional series differ by hue plus a second channel (dash, marker shape, direct label). Status colours are reserved for status.
- Direct-label lines and annotate meaningful events with a thin marker and short label; forecasts, simulations, and measurements differ in line style and the legend says which is which. Show uncertainty bands or error bars as a quiet tint or whiskers that never hide the central estimate.
- Render by data volume: vector for small sets, canvas with downsampling for large ones, with the downsampling stated in the UI if it changes what is displayed.
- Provide a table or text equivalent for any chart that conveys information.

CAD and 3D viewports
- The viewport is the content: keep chrome thin and low-contrast, overlay controls in a consistent corner, and never place gradients or glass blur behind coordinates, dimensions, or measurement labels. Axis colours follow the existing convention; legends and selected-entity highlights meet 3:1 against the scene background in both themes. Do not restyle model geometry colours that encode meaning.

## 5. Contrast, focus, theme, responsiveness

- WCAG 2.2 AA: body and placeholder text at least 4.5:1, large text 3:1, meaningful UI boundaries, icons, chart marks, and focus indicators at least 3:1, measured on the real surface in each theme. Placeholders and tertiary text are not exempt.
- Never encode meaning by colour alone; pair it with text, shape, pattern, or position. Check red/green confusion for status and chart pairs.
- Visible `:focus-visible` outlines on every interactive element (bronze or a high-contrast ring), never `outline: none` without replacement; sticky headers must not cover a focused element.
- Targets: keep operable controls comfortably sized for pointer use; compact density never goes below the size existing accepted UI already uses.
- Responsive means structural collapse (sidebar, columns, table scroll containers), not fluid type. Verify at the widths the accepted spec names plus a narrow width.
- Browser-default surfaces (selection, caret, scrollbars, native selects, number inputs) must follow the theme in both modes.

## 6. Motion

- Motion states a change: open, expand, confirm, load. 150-250 ms, ease-out, animate transform and opacity, list properties explicitly (never `transition: all`).
- No page-load choreography, no scroll-triggered fades, no looping decoration, no hover transition on every card. At most one authored emphasis moment in a whole flow, and none on data views.
- `prefers-reduced-motion` removes or reduces every non-essential animation; skeleton loading keeps layout stable and has no shimmer sweep.

## 7. Anti-generic-AI-aesthetic checklist

Reject or justify in the PR each of these if it appears without a reason tied to the brief:
- cream plus terracotta serif look; near-black plus a single neon accent; neon-glow gradients and grid-glow backgrounds
- identical rounded cards with the same soft shadow around every block; nested cards; gradient washes as decoration; gradient text
- icon in a tinted circle on every KPI; hero-metric tile (big number, small label, supporting stats, accent); sparkline or progress ring standing in for content
- tracked all-caps eyebrow over every heading; "01 / 02 / 03" markers on non-sequences; middle-dot meta strings; spaced em-dash labels; arrows appended to every link
- monospace used as a costume for "technical" instead of for code, IDs, and measurement; emoji or glyphs as icons (use one consistent icon set and stroke)
- coloured thick side-stripe borders on cards or alerts; hard offset shadows; glass blur everywhere
- foliage, marble, laurel, or meander used as wallpaper; mythic ornament that competes with data
- invented numbers, fake avatars, placeholder marketing copy
Positive tests: the screen answers its one question first; one memorable element, everything else quiet; every structural device (border, label, divider) encodes information; copy uses the product's own terms and one name per action through the flow.

## 8. Process

1. Read the accepted spec and `docs/agent-skills/relay-studio-react/SKILL.md`; list the screens and states in scope.
2. Capture the baseline with `relay-browser-proof` (Section 9), both themes.
3. Fill the direction brief (Section 2). When the maintainer asked for options, produce two or three distinct directions as static or prototype screens, label them, and stop for selection; do not implement a direction the maintainer has not chosen.
4. Write a compact design plan: tokens (palette roles, type scale, spacing, radius, elevation), layout concept per screen, and the one memorable element. Review the plan against the brief and Section 7; revise anything that matches a default.
5. Implement through tokens; touch only style. Keep diffs reviewable: tokens first, shared components second, screens last.
6. Run Section 9 acceptance. Fix everything one look reveals before taking the next screenshots.

## 9. Before/after acceptance (reuses `relay-browser-proof`)

Read `docs/agent-skills/relay-browser-proof/SKILL.md` and follow it for session setup, the exact-head record, and screenshot capture. The model's own liking of the result is not acceptance evidence.

1. Baseline ("before"): on the pre-change exact head, record `git rev-parse HEAD`, then capture the same named screens and states: default, dense table, long values, empty, error/stale, loading, focused control, selected row, chart with annotation, CAD viewport; in dark and light; at the specified wide and narrow widths. Name files `<screen>-<state>-<theme>-<width>-<before|after>.png`.
2. Candidate ("after"): rebuild the frontend at the new exact head, restart so the backend and frontend actually serve it, and repeat every capture with the same data and viewport.
3. Behavior parity: walk the accepted operator path end to end in a fresh session (draft save, isolated Test, explicit Publish, pinned downstream use) and compare snapshot text. All values, units, labels, and available actions must be identical before and after; verify backend records where the criterion depends on them. Run the existing frontend unit and end-to-end tests and the build; they must pass unchanged.
4. Contrast and accessibility: measure computed foreground/background pairs for each text tier, chart mark, boundary, and focus ring in both themes against the 4.5:1 and 3:1 limits; walk the main flows by keyboard only; confirm `prefers-reduced-motion` behavior. Automated scores (Lighthouse, axe) are supporting evidence, not a pass on their own.
5. Report: exact heads, the filled direction brief, side-by-side image pairs, a measured-contrast table, test results, what was not checked, and any behavior difference (any difference blocks acceptance).
Missing tools or runtime means report missing evidence; do not substitute mocks or synthetic screenshots.

## 10. Provenance

Techniques below were distilled in the author's own words from sources pinned in the issue #834 section of `docs/agent-skills/AUDIT.md` (not carried into this repository). No upstream text is copied. None of their scripts, hooks, binaries, MCP servers, or installers is used. Apache-2.0 sources need attribution only if text is copied; it is not.

| Source | Pinned commit | License | Techniques taken |
| --- | --- | --- | --- |
| Leonxlnx/taste-skill, `skills/taste-skill/references/app-screens.md` | `b9ca2fd8047f4c72d7da90ebc1c1203e57c72cd2` | MIT | KPI/table/chart conventions (tabular right-aligned figures, fixed decimals, unit suffix, honest zero/missing/stale/loading/error states, one data hue, faint few gridlines, no smoothing, annotation of events, dashed forecasts, contrast floor for every text tier); density as three settings moved together |
| anthropics/skills, `skills/frontend-design/SKILL.md` | `dbd4588f9e1033efb41dad4bef2f7947c8993d44` | Apache-2.0 | compact token plan then review against the brief; one memorable element; catalogue of generic-AI tells; one-name-per-action copy rule |
| vercel-labs/web-interface-guidelines, `command.md` | `434b7f91364665f2f733b310ec54809bf8f37937` | MIT | focus-visible and no bare outline removal; reduced motion; transform/opacity only and no `transition: all`; tabular numerals; truncation and empty states; `color-scheme`; destructive-action safety |
| pbakaus/impeccable, `skill/reference/operate.md` and `craft-floor.md` | `0665bd812ae0908fcab17b96fd4f840defb882d9` | Apache-2.0 | separate app-UI (Operate) ambition from marketing; fixed rem scale with tight ratio; second neutral for panels; all component states; skeleton loading; short state-conveying motion; themed browser-default surfaces |
| addyosmani/web-quality-skills, `skills/accessibility` | `afa8da942115f2961fdbfa80807ea0b232ff6c00` | MIT | evidence-led accessibility loop (audit, localize, keyboard, fix, re-run) and the warning that automated scores are not conformance; 4.5/3/3 contrast thresholds |
| callahkate/data-viz-best-practices | `da2c6af9cdd897d15257e0974055bf50a5a45860` | MIT | not-by-colour-alone; truthful scales and naming trade-offs; direct labels; consistent encoding across a dashboard |
| nextlevelbuilder/ui-ux-pro-max-skill, `src/ui-ux-pro-max/data/charts.csv` | `56ea9cf174ed86786187daa9de0e6247cb0747e4` | MIT | rendering by data volume and per-chart accessible fallback |

The direction brief (Section 2) and the scientific-semantics rules (Section 1) are original to JarvisOS and come from the maintainer's issue text and repository constraints, not from any upstream source. Pin updates require a fresh source audit.
