# BUBU-2 — speech bubble, dictation, screenshots/vision and "show me where"

## Authority

Maintainer request of 2026-10-30, after BUBU-LAUNCHER-1 (#1002): continue Bubu with the green translucent speech
bubble on Bubu's layer, buttons to dictate and to take screenshots, and the backend support discussed in the Bubu
brainstorm. The decisions it implements are the maintainer's of 2026-10-29: Bubu replaces the Sidecar as the way into
Jarvis; it navigates and points on request but never presses controls that change data; the page is described
DOM/accessibility-first and vision is a per-request switch that stays on until Sleep; Sleep ends the request (next
message starts a new thread); dictation now, read-aloud later; no proactive advice; no paid APIs. This is a bounded
presentation + context slice; it grants no 224/225 action authority, no new provider and no tool/MCP surface.

## What ships

**Bubble** (`components/bubu/BubuBubble.tsx`, `conversationStore.ts`, `bubuPose.ts`, `bubblePlacement.ts`,
`bubuAnchor.ts`): clicking Bubu opens a green translucent bubble (backdrop blur, tail to Bubu, z-index 42 between the
launcher at 40 and the floating solver console at 60) that follows Bubu when dragged and flips to stay on screen. It
shows the last turns of the same Jarvis conversation as the panel (`useJarvisSidecar` publishes into an external
store and registers its own submit path, so thread, route class and context handling are unchanged), a composer,
and Send / Expand (full Jarvis panel) / Sleep (new thread next time; Bubu sleeps) / Close. Bubu's pose follows real
state: thinking while a turn runs, clarifying when the last reply asks a question, listening while dictating,
sleeping after Sleep.

**Page description** (`pageContext.ts`): every message carries `ui_context` — the visible stable targets
(`data-jarvis-target`) and other named interactive elements (ephemeral `el-<n>` ids marked on the DOM), at most 150,
Bubu's own UI excluded.

**Show me where** (`pointerDirectives.ts`, `BubuPointer.tsx`): the backend tells the model it may answer with
`[[point:<id>]]` / `[[point:<id>|label]]` using only listed ids; the directives are removed from the visible text and
the newest reply draws a ring, label and arrow from Bubu to each element (scrolls it into view if needed, tracks
scroll/resize) until the next reply or Sleep. The user can point too: the hand tool intercepts the next click
(the page action does not run), quotes the element in the draft and sends it first in the page description.

**Dictation** (`useDictation.ts`): browser Web Speech API (Edge/Chrome), language from the browser, final text
appended to the draft, interim text shown in the bubble's status line. No backend, no new API key.

**Screenshots / vision** (`captureTab.ts`): the camera attaches one screenshot of the tab to the next message; the
eye turns vision on — one share prompt, then a fresh frame with each message until Sleep or until it is turned off.
JPEG ≤ 1600 px wide, re-encoded to stay under the backend limit.

**Backend** (`modules/ai/thread_ui_context.py`, `thread_models.py`, `thread_service.py`, `context_builder.py`,
`contracts.py`, `execution.py`, `providers/local_llamacpp_adapter.py`, `core/ai_thread_schema.py`,
`core/database.py`, `local_ai/runtime/llama_cpp.py`): `AIThreadSubmit` accepts optional `ui_context` and `images`
(≤ 2 PNG/JPEG data URLs, ≤ 6 MB decoded, magic bytes checked; 422 otherwise). The page block is its own context
block (`jarvis:visible-page`), never part of the stored user text; page text cannot forge a directive. Screenshots
are stored as `bubu_screenshot` artifacts under the workspace, referenced from the additive table
`ai_thread_turn_extras` (migration `0028_bubu_turn_extras`, created by `initialize_database()`, which the launcher runs on start). Requests without the
new fields keep identical prompts and digests.

## Known limits

- **Images reach a model only on the local llama.cpp route with a vision projector** (`JARVISOS_LLAMACPP_VISION=true`
  and llama-server started with `--mmproj`); nothing enables that today. Every other route (Hermes agent, Ollama,
  network) keeps the screenshots but marks the turn `images_unsupported`, and the bubble says "This model can't see
  images; it read the page instead." Making the subscription/Relay path accept images is the next backend step.
- Whether the model actually uses `[[point:…]]` well depends on the model; the instruction and the rendering are in
  place, the quality is unmeasured. The preview used the synthetic responder.
- Not exercised in a real browser: microphone and screen-share prompts (they need the user's permission).
- Hermes truncates combined prompt + context at 12 000 characters, so a very long prompt can push the page block out.
- Ephemeral `el-<n>` ids are only meaningful until the page changes; a reply pointing at an element that is gone
  shows nothing for it.

## Screenshots through Relay (subscription)

The default Jarvis chat answers with Hermes on a local model, which cannot see images. The subscription path is the
Relay escalation (Claude Code in the bwrap sandbox). agent-relay has no image flag, so approved screenshots of the
source turn are copied into the run's read-only context directory (`/workspace/context/screenshots/<n>.<ext>`,
rebuilt every run) and the framed prompt asks Claude to open them with its Read tool.

- Draft (`relay-escalation-draft`) lists `image_count` / `image_digests` of the source turn's retained screenshots.
- Approval (`relay-escalate`) sends `image_digests` explicitly (≤ 2, each must belong to the source turn; empty =
  no images, so older clients and runs without images keep byte-identical prompts and digests). The image digests
  join the replay digest, so a different image set is not a replay.
- Screenshots cannot be text-screened: they leave only through this explicit approval of the turn's own images
  ("Send N screenshots to Claude", ticked by default, with the note "Screenshots are not screened like text").
- In the bubble, a turn whose model could not see the images offers **Ask Claude** (only when Relay is enabled),
  which opens that approval in the full panel.

Live check (isolated preview, private data copy and private relay root, neutral synthetic test card — no project
content): Claude Opus via the subscription answered "ZEBRA-4217 … the circle is red", i.e. the answer depends on the
image. Latency, created → finished: 9.7 s with one screenshot (3.2 s start incl. first workspace preparation, 6.6 s
run) vs 3.7 s for a one-word answer without image (0.8 s start, 3.0 s run). The answer arrives as a Relay result in
the panel, not as a bubble reply.

## Evidence

`npm run build` (all static tests incl. new `bubu-bubble`, `bubu-media`, `bubu-pointer`, `bubu-integration`, then
`tsc`, `vite build`); backend `tests/test_bubu_page_context.py` (12) + `tests/test_ai_threads.py` and a 136-file AI/
thread/Hermes/llama/egress subset (2 failures pre-existing on master in this environment); `ruff check app tests`.
Isolated preview on a copy of the live data: bubble placement and look, page description stored (150 targets incl.
the Run button), user pointing intercepts "Run" without running the solver, pointer rings/labels/arrows drawn from a
reply with two directives and the directives removed from the text.
