# BUBU-LAUNCHER-1 — movable entry to the existing Jarvis conversation

## Accepted slice and authority

On 2026-10-29 the maintainer explicitly approved the first implementation:
“ok vai con primo intervento”, following the proposal to implement only the
movable mascot and opening of the existing Jarvis. This is a bounded presentation
amendment to the 161/213 shell, not implementation readiness for the broader Bubu,
224 guidance or 225 computer-use architecture. The supervisor stays paused.

Acceptance:

- A single Bubu launcher is present across routes without taking a layout column.
- Pointer dragging does not open the conversation; an ordinary click does.
- Position survives reload/navigation and remains reachable after viewport resize.
- Keyboard users can activate, move and reset Bubu, with visible focus.
- Design shell Sidecar starts closed, with no width-consuming reopen tab.
- Existing chat, composer draft, context controls and Properties remain usable.
- No duplicate conversation is created on pages already rendering Jarvis.
- Real browser evidence starts at `/` in a fresh context and uses visible controls.

The maintainer's approved Claude analysis remains architecture context: presentation
is separate from observation, navigation and mutations. This patch implements only
the user-operated launcher. It does not create a generic DOM observer/action tool,
image transport, annotation engine or model-routing mechanism.

## Implementation

`BubuLauncher.tsx` mounts one native button through a React portal to the document
body. Its transparent image occupies a 96 CSS-pixel box; only that box intercepts
pointer input. It stays outside route clipping containers. Native modal top layers
and the existing context-menu layer remain above it. Fullscreen viewer integration
is not added or claimed.

Pointer capture handles mouse/touch/pen dragging; a six-pixel movement threshold
separates dragging from clicking. Cancel/lost capture restores the starting position;
Escape cancels a drag. Arrow keys move by 16 pixels, Shift+arrows by 48; Home restores
the lower-right corner. Enter/Space retain native button activation. The launcher
receives focus after closing the shell Sidecar.

Only normalized viewport position is stored in `jarvisos:bubu-position:v1`.
Malformed/unavailable browser storage falls back to a usable default. Resizing
recomputes available movement range; position has no conversation/session meaning.

The shell no longer opens Jarvis automatically on Process/BlueCad/Studio entry,
and the old right-hand reopen strip is replaced by Bubu. Explicit existing shell
requests still open the panel. On Coding and ready in-page Jarvis routes, clicking
Bubu scrolls to and focuses the existing `jarvis-prompt` composer rather than
mounting a second chat. Settings can explicitly open the same shell conversation.
Without a workspace, the existing composer remains disabled and explains that a
workspace must be selected. No new submission path is introduced.

Conversation state already belongs to `useJarvisSidecar` in `App`, above the
optional panel. This slice leaves that ownership unchanged. Embedded chat columns
on Memory/Development/Coding are retained: reclaiming their width requires a
separate layout change. A compact chat bubble and expanded conversation modes are
also deferred; this first slice opens the existing Sidecar directly.

The standard `bubu-listening.webp` is used as a static presence illustration. It
does not indicate recording, model activity or a running Bubu session. The other
four prepared poses remain unbound until actual state ownership is implemented.
Closing the Sidecar does not mean cancelling a model request or ending a session.

## Boundary

Added frontend presentation code and a local visual preference only. No backend,
API/route, database, memory/retrieval, model/provider call, tool/MCP/hook/worker,
viewer behavior, project modeling, external code vendoring, microphone/capture,
image egress, automatic navigation, issue publication or supervisor activation.

The old source assertions requiring Studio auto-open, a Sidecar strip and no
Settings conversation are updated to the newly accepted presentation outcome.
Other regression assertions remain intact; no frozen backend conformance test
is modified.

## Validation and delivery

Validation results, exact commands and delivery state are recorded below after
the browser and build checks. Earlier architecture audit/register edits and
prepared image assets predate this slice and must remain distinguishable from it.

### Integration onto master (2026-10-30)

The slice was prepared by Astra on a branch based on `f3ec4913` (29 commits behind) and
re-applied by hand onto master `fc12713d`, which already carries the Design project
switcher in the rail (#996) and the case-study/Results work (#1000, #1001). Only the
launcher, its assets, this note and the spec 213/STATUS amendment are included; the
brainstorming audit and the idea-register edits stay on Astra's branch. The proof sheet
moved to `docs/implementation/assets/bubu-poses-preview.png` so it is not served.

Checks on the integrated branch: `npm run build` (all static tests, `tsc`, `vite build`)
and `scripts/check_spec_status.py` pass. Browser, isolated preview on a copy of the
live data: Sidecar closed on Design entry with no reopen strip; click on Bubu opens
Jarvis (Properties inside) and Bubu steps beside the panel; a second click closes it;
a mouse drag moves Bubu without opening Jarvis and the position survives reload; the
floating solver console (z-index 60) stays above Bubu (40). `bubu-launcher.browser.mjs`
needs a local Playwright and an isolated instance; it is not wired into CI.
