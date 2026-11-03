# Operator usability and frontend completion contract — 2026-09-15

Status: **maintainer-approved cross-surface product-quality contract; not implementation authority or queue state**  
Owner: repository maintainer  
Companion authority: `APPROVED_OPERATOR_UI_MANIFEST_2026-08-27.md`, `FRONTEND_CONFORMANCE_CONTRACT_2026-08-27.md`, `FINAL_OPERATOR_INTERACTION_CONTRACT_2026-08-27.md`, and the most-specific approved surface reference.

`docs/specs/STATUS.md` remains the sole live implementation/spec authority. This contract defines what **usable completion** means whenever an accepted slice exposes or materially changes operator-visible capability.

## 1. Product principle

Human usability is a first-class JarvisOS capability, not visual polish.

JarvisOS exists to make complex engineering, knowledge, planning, AI and software work easier for one human operator. A backend that is correct but difficult to discover, interpret or operate through the application has not delivered the product outcome.

For any user-facing capability, completion means:

`authoritative backend + truthful frontend integration + intuitive human interaction + required evidence`

A feature is not product-complete merely because:

- its API exists;
- backend tests pass;
- the page renders;
- the browser can locate some text;
- the correct value appears somewhere on screen;
- a screenshot was captured;
- a developer who knows the internal architecture can eventually find the control.

Internal-only infrastructure may remain headless when the accepted contract explicitly classifies it as internal/non-operator-facing. The default for project/product capability is operator integration, not permanent backend-only delivery.

## 2. User model

JarvisOS is a specialized engineering workstation, but the operator must not need to remember implementation architecture in order to use it.

The UI should expose the operator's mental model:

- project knowledge;
- models and evidence;
- design/process/CAD work;
- roadmap/calendar/brainstorm;
- repository/runtime;
- Jarvis conversation/context/actions;
- clear system/provider settings.

It should not force the operator to think in:

- table names;
- endpoint names;
- spec IDs;
- backend module boundaries;
- UUIDs/digests/SHAs unless they are genuinely needed;
- storage taxonomies that are only implementation details.

Exact technical identity remains available in `Technical details`, provenance, inspectors or explicit audit surfaces.

## 3. Canonical mock HTML remains the visual target

For every surface listed in `APPROVED_OPERATOR_UI_MANIFEST_2026-08-27.md`, the canonical HTML remains the primary composition reference at its reference viewport.

Production may replace fixtures with real backend data and improve semantic HTML/accessibility, but it must preserve:

- panel order and relative scale;
- dominant versus supporting regions;
- navigation placement;
- interaction affordances;
- contextual versus persistent information;
- typography roles and visual hierarchy;
- control grouping;
- expansion/dialog/selection behavior;
- intended information density;
- visible Jarvis placement where specified;
- surface-specific structure instead of flattening every page into generic cards.

If the production UI is materially less understandable than the mock despite being technically functional, the implementation is non-conformant.

A builder must compare the live surface against the **HTML itself**, not only an old screenshot or textual memory of the design.

## 4. No fake affordances

Anything that visually communicates interactivity must behave as an interaction or be unmistakably disabled/unavailable.

Forbidden examples include:

- a chevron/arrow that looks expandable but is only a decorative `<span>`;
- a text composer that looks like an input but cannot accept focus or typing without a clear unavailable treatment;
- a button that appears enabled but has no meaningful action;
- a clickable-looking row with no click/keyboard behavior;
- a tab label that does not switch views;
- an icon whose meaning is not discoverable by label/tooltip/context;
- controls that mutate only local React presentation while implying server persistence.

Use native semantic elements when possible (`button`, `a`, `input`, `select`, `details/summary`, `dialog`, etc.). ARIA supplements native semantics; it does not replace missing keyboard behavior.

### Disclosure controls

A disclosure chevron must be part of a real button/summary control and:

- toggle expanded/collapsed content;
- expose `aria-expanded` when a custom button pattern is used;
- support `Enter` and `Space` activation;
- rotate/change direction consistently with state;
- keep focus predictable;
- allow multiple expansions simultaneously where the approved surface requires it.

## 5. Every operator-visible backend capability needs a usable frontend path

When an accepted backend/read/action contract exists for a capability that the operator is expected to use, the corresponding product slice must provide a frontend path unless the accepted spec explicitly stages the frontend separately.

The frontend must expose enough of the backend capability to complete the intended human task, not merely prove that the API can be called.

Examples:

- a persisted Roadmap backend requires a usable Roadmap editor/view, not only JSON cards;
- Calendar allocations require a real calendar interaction model when the canonical design calls for one;
- a Project Knowledge record owner requires inspectable human-readable records and working controls;
- Coding repository truth requires a usable Repository Inspector, not a narrow raw-text pane;
- Jarvis context/actions require a functioning common Jarvis interaction surface, not repeated disabled placeholders.

A staged slice may expose a clearly disabled approved affordance only when the owning capability is genuinely not authorized/implemented yet. Once the backend owner exists, leaving the user-facing control permanently fake/disabled is a product defect unless a later accepted contract intentionally defers activation.

## 6. Task-first information architecture

Every surface must make the primary task obvious without documentation.

At normal desktop size:

- the primary work area dominates visually;
- the top-level purpose is clear from heading and layout;
- the most common operator action is visible without searching menus;
- supporting detail remains subordinate;
- advanced/audit metadata uses progressive disclosure;
- user-facing labels describe tasks/concepts, not implementation objects;
- there are no dead-end screens where the operator cannot determine what to do next;
- navigation consistently communicates current location and peer destinations.

The approved primary navigation remains:

`Design | Memory | Development | Coding | Settings`

with the peer destinations already frozen by the final operator contract.

## 7. Progressive disclosure for technical density

JarvisOS is technically dense; hiding all detail would be wrong. The rule is **progressive disclosure**, not simplification by deletion.

Show first:

- human title;
- state/status;
- key value/result;
- reason/meaning;
- next meaningful action.

Put secondary/audit information behind an explicit disclosure when appropriate:

- UUIDs;
- exact SHA/digest;
- raw JSON;
- schema version;
- timestamps;
- internal source IDs;
- full logs;
- raw API payloads.

Collapsed records must remain identifiable and scannable. Expanded records must reveal enough detail to answer the normal operator question without routing to another page unnecessarily.

## 8. Jarvis sidecar is a real shared interaction surface

Where the canonical surface includes Jarvis, use one coherent Jarvis-sidecar interaction model rather than screen-specific fake composers.

The sidecar should, according to the currently authorized runtime capabilities:

- accept text input when a chat path exists;
- show current thread/session identity in a human-usable way;
- show explicit active context and allow removal;
- accept explicit selected records/files/ideas into context;
- preserve page/workspace context without silently adding browsed data;
- expose loading/streaming/error states honestly;
- distinguish explanation/proposal from committed action;
- preserve continuity when navigating between peer surfaces where the product contract requires it.

Future Hermes integration should replace/extend the runtime behind this interaction surface rather than create a second unrelated chat UX.

## 9. Search must support human query behavior

Exact/literal search is useful evidence tooling but is insufficient as the only operator search mode.

For human-facing project/repository/knowledge search, preserve exact identity lookup while supporting progressively richer retrieval:

1. exact ID/path/title match;
2. case/whitespace/punctuation-normalized text matching;
3. token-based matching that tolerates omitted stop words and word order where this does not fabricate facts;
4. later semantic retrieval where an accepted owner exists.

The operator should not need to guess the exact stored sentence. A query such as `perdite carico` should have a deterministic opportunity to match a record containing `perdite di carico` before the system concludes that there are no matches.

Search result ranking and semantic retrieval never become source authority. Every result still links to the exact canonical record/file/source identity.

## 10. Interaction feedback and system status

Every operator action must produce prompt, visible feedback.

At minimum:

- buttons/controls visibly enter pressed/busy/disabled state when appropriate;
- long operations show progress or a clear in-progress state rather than freezing;
- save/commit/proposal success is explicit;
- errors identify what failed and what the operator can do next;
- partial/degraded data is labeled;
- stale results do not silently replace newer selection state;
- destructive operations require the confirmation/reversibility defined by their owning spec;
- async navigation/data refresh never makes the operator wonder whether the click registered.

Status messages should be exposed accessibly (`aria-live`/appropriate status semantics) where relevant.

## 11. Error prevention and recovery

Prefer preventing mistakes over reporting them afterward.

Requirements:

- destructive actions are separated from common actions and clearly labeled;
- invalid combinations are prevented or explained before submission where possible;
- forms preserve operator input after recoverable errors;
- canonical edits are bound to the exact target revision/head where required;
- stale-base/conflict conditions are explained in human terms and provide a recovery path;
- cancel/undo/retry is available when the owning action semantics permit it;
- dialogs/forms never hide the only route back to the prior state;
- failed optimistic UI state must roll back truthfully.

## 12. Forms and editors

Forms must be designed around the human task rather than backend field inventory.

Every editable field should have:

- a visible label;
- an appropriate native input type/control;
- sensible defaults when authoritative/safe;
- units adjacent to engineering values;
- validation close to the field;
- clear required/optional distinction;
- preserved values after validation errors;
- concise help only when the field cannot be understood from label/context.

Advanced fields should be grouped/collapsible instead of overwhelming the primary form.

Do not ask for information already known in the same flow when it can be selected or prefilled safely.

## 13. Keyboard, focus and accessibility baseline

Target WCAG 2.2 AA for normal operator UI wherever applicable, with semantic HTML first.

Minimum requirements include:

- all interactive actions operable by keyboard;
- visible focus indication;
- logical focus order;
- focus moved/restored correctly for dialogs and major dynamic transitions;
- no keyboard traps;
- labels/names/roles/values exposed correctly;
- color is not the sole carrier of meaning;
- text and controls retain usable contrast;
- target sizes satisfy WCAG 2.2 minimum requirements; prefer comfortably larger targets for primary desktop controls;
- dragging has an alternative when dragging is not essential;
- repeated navigation/help remains consistently placed;
- custom widgets follow WAI-ARIA Authoring Practices keyboard/state semantics;
- screen-reader/accessibility-tree output remains meaningful enough for automated and manual inspection.

Accessibility improvements are always permitted when they preserve approved product semantics/composition.

## 14. Layout, clipping, resizing and density

A surface fails usability if content is technically present but unreadable because of layout.

Requirements:

- no unintended overlap between panes/text/controls;
- no clipped primary labels or actions at the canonical reference viewport;
- supporting panes must not starve the dominant work area;
- prose wraps; code/log/raw technical text uses a bounded horizontal-scroll region when necessary;
- scroll containers are visually and behaviorally obvious;
- nested scrolling is minimized and never hides the main task;
- fixed/sticky elements must not obscure focused controls;
- zoom/text resizing must not destroy primary functionality;
- layouts adapt outside the reference viewport while retaining the same information hierarchy;
- the UI must remain usable under normal Windows display scaling, not only one CI screenshot environment.

Canonical HTML viewport fidelity remains mandatory; responsive behavior outside it must not become a redesign.

## 15. Component and frontend coding rules

Prefer shared, tested interaction primitives where behavior is repeated, while preserving surface-specific composition.

Good shared primitives include:

- Button / IconButton;
- Tabs / peer navigation;
- Disclosure;
- Dialog / confirmation;
- FormField;
- SearchInput;
- Empty/Loading/Error/Partial state;
- ContextChip / context basket;
- Inspector split panes;
- technical-details disclosure;
- bounded preview;
- status/progress messaging.

Rules:

- do not use generic abstractions that flatten distinct Process/BLUECAD/Memory/Development/Coding layouts into one dashboard;
- one semantic interaction should behave consistently across the app;
- use native browser controls/semantics where they satisfy the required UX;
- avoid clickable `div`/`span` when a button/link is the correct element;
- server-owned data remains server-owned; local React state is presentation/draft state unless the accepted owner says otherwise;
- request generations/cancellation/stale-response guards must protect selection-driven async views;
- canonical mutation should normally refresh/reconcile from server truth after success;
- component reuse must reduce inconsistency, not erase the approved mock identity.

## 16. Human-readable copy

UI text should use concise operator language.

Prefer:

- `Add work item` over internal mutation terminology;
- `Local runtime behind remote` over exposing a raw enum alone;
- `No records match` plus a useful next step over a cryptic error code;
- human titles before internal IDs.

Avoid large explanatory paragraphs when the interaction can be made self-evident. If the interface requires a paragraph explaining how to use an obvious primary control, redesign the control first.

Technical codes/errors may remain available under details or alongside a human explanation.

## 17. Performance and perceived responsiveness

The interface should remain responsive even when backend work is slow.

Requirements:

- operator input and navigation are not blocked by unrelated requests;
- loading one pane does not blank unrelated stable context unnecessarily;
- expensive lists/previews use bounded rendering/virtualization/pagination when required;
- search/input interactions debounce/cancel stale work where appropriate;
- no repeated network polling/render loop should make typing or scrolling visibly laggy;
- a slow backend call produces visible progress/degraded state rather than frozen UI.

Performance optimization should still be evidence-driven, but obvious interaction jank is a product defect.

## 18. Required test/evidence stack for material operator-facing work

A material frontend or user-facing full-stack slice is not sufficiently proven by backend tests plus static screenshot evidence alone.

Use the least expensive subset that proves the accepted behavior, but the following categories are the default expectation for meaningful surface work.

### A. Backend/contract evidence

- owning API/action tests;
- persistence and exact-target semantics;
- error/stale/permission behavior relevant to the UI.

### B. Frontend deterministic/component evidence

- state rendering;
- primary control behavior;
- loading/error/empty/partial states;
- critical keyboard semantics where component-specific.

### C. Real-browser task evidence

Test actual human tasks, not merely element presence.

Examples:

- click a Project Basis row -> detail visibly expands -> collapse works;
- type a project search query -> relevant results appear -> select result -> exact owner opens;
- type into Jarvis -> submit -> visible response/error state;
- create/edit/delete a Roadmap item through the operator UI;
- switch `Timeline <-> Calendar` and create/edit a calendar allocation;
- capture a raw Brainstorm note and inspect its persisted state;
- select a repository file -> obtain a readable preview -> add it to Jarvis context -> open exact GitHub URL;
- switch Repository/Runtime without losing orientation.

Assertions should verify outcome and interaction state, not only that a selector exists.

### D. Visual/composition regression

For manifest surfaces:

- render at the canonical reference viewport;
- compare against the approved HTML/composition and expected interaction states;
- use deterministic screenshot comparison where the environment can be controlled;
- inspect meaningful diffs rather than accepting a screenshot merely because one exists.

Additional viewport/zoom smoke should detect clipping/overlap in realistic desktop conditions.

### E. Accessibility evidence

For material interaction changes:

- automated axe/WCAG-oriented scan for common violations;
- keyboard-only smoke through the primary flow;
- accessible name/role/state assertions for critical widgets;
- accessibility-tree/ARIA snapshot where it gives stable value;
- manual inspection for issues automation cannot detect.

### F. Layout integrity evidence

Critical pages should verify that primary regions:

- are visible;
- do not unintentionally overlap;
- do not clip required controls/text;
- retain usable dimensions at supported/reference viewport(s);
- keep overflow inside intended scroll containers.

### G. Human operator acceptance

For a new surface, major redesign, or a repair explicitly triggered by real-world usability failure, the maintainer/operator performs a short task-based acceptance pass before the surface is treated as beta-complete.

The goal is not subjective aesthetic perfection. The pass asks:

1. Can I immediately tell what this page is for?
2. Can I find the primary action without documentation?
3. Do controls behave the way they look?
4. Can I complete the main task without knowing backend architecture?
5. Is important information readable without clipping/overlap?
6. Do errors/statuses tell me what happened and what to do next?
7. Does the page still look/behave like the approved mock direction?

A material failure is a product finding, not optional polish.

## 19. Usability severity

Classify operator-UX defects by effect, not aesthetics.

### P0

- UI can cause destructive/unsafe unintended action;
- operator is shown materially false authoritative state;
- security/privacy control is unusable or misleading in a way that can expose data.

### P1

- primary task cannot be completed through the intended UI;
- a major approved capability is unreachable despite backend support;
- a control materially lies about being interactive;
- severe clipping/overlap makes primary information unusable;
- navigation/state makes the operator lose work/context;
- interface diverges from approved product composition enough to impair normal use.

### P2

- task is completable but unnecessarily confusing/slow;
- search requires unnatural exact phrasing;
- important state is hidden behind excessive technical detail;
- labels/order/feedback create avoidable mistakes;
- keyboard/accessibility issue materially affects a secondary path.

### P3

- cosmetic or minor consistency issue that does not materially affect task success, comprehension or accessibility.

Do not downgrade a P1/P2 usability defect to “polish” solely because APIs/tests are correct.

## 20. Completion rule for user-facing slices

A user-facing slice is complete only when all applicable statements are true:

1. accepted backend authority/behavior is implemented;
2. the intended operator frontend is connected to that authority;
3. canonical HTML/interaction references are respected;
4. all controls are honest and functional for their advertised state;
5. primary tasks are discoverable and completable by the operator;
6. loading/error/empty/degraded states are understandable;
7. no known P0/P1 human-usability defect remains;
8. relevant accessibility/keyboard requirements pass;
9. visual/layout evidence shows no material reference drift or clipping/overlap;
10. real-browser task evidence proves the interaction path;
11. persistence/reload behavior is proven where the task mutates durable state;
12. exact authority/provenance remains available without dominating normal use.

Backend completion without operator completion is backend progress, not finished JarvisOS product capability.

## 21. Immediate lessons from the 2026-09-15 local acceptance pass

The maintainer's real Windows master run exposed a causal family that this contract is intended to prevent:

- Project search was technically truthful but too literal for natural operator phrasing;
- Project Basis rendered chevrons that did not disclose anything;
- Project Basis Jarvis rendered an input-like composer that was hard-disabled;
- Development backend capability existed but the production composition drifted materially from the approved Roadmap/Calendar/Brainstorm references;
- Coding Repository displayed real repository evidence but the file preview/layout was cramped/overlapping and unlike the approved Repository Inspector;
- overall capability was substantially harder to understand than the approved mock direction despite extensive deterministic CI.

These findings require their own authorized implementation/repair slice; this contract does not by itself authorize runtime changes.

## 22. External guidance incorporated

The contract above is JarvisOS-specific. The following external references informed the general usability/accessibility/testing principles and should be rechecked when relevant:

- W3C Web Content Accessibility Guidelines (WCAG) 2.2: https://www.w3.org/TR/WCAG22/
- W3C WAI ARIA Authoring Practices Guide: https://www.w3.org/WAI/ARIA/apg/
- Nielsen Norman Group — 10 Usability Heuristics: https://www.nngroup.com/articles/ten-usability-heuristics/
- GOV.UK Service Manual — make the service simple to use: https://www.gov.uk/service-manual/service-standard/point-4-make-the-service-simple-to-use
- GOV.UK — writing for user interfaces: https://www.gov.uk/service-manual/design/writing-for-user-interfaces
- Playwright — accessibility testing: https://playwright.dev/docs/accessibility-testing
- Playwright — visual comparisons: https://playwright.dev/docs/test-snapshots
- GitHub Primer — accessibility/focus/layout/navigation guidance: https://primer.style/accessibility/design-guidance/ and https://primer.style/product/ui-patterns/navigation/
- Visual Studio Code UX guidelines for complex workbench/container patterns: https://code.visualstudio.com/api/ux-guidelines/overview

External guidance never overrides JarvisOS authority/product contracts. It provides mature interaction/testing patterns used to implement those contracts well.