# BUBU-3 — pointer dismiss, movable bubble, navigation paths, LaTeX

Status 2026-10-30: integration PR #1005 is merged and activated in the live app; the Chromium operator walk passed.
The Hermes LaTeX prompt follow-up is merged in #1009, and KaTeX lazy loading is merged in #1010. The original
integration was prepared by Claude (Opus 5.5 coordinating, Sonnet 5.5 subagents). It builds on BUBU-2 (#1003)
and Relay screenshots (#1004).

## Authority

Maintainer feedback on the live BUBU-2 (2026-10-30), quoted in substance: the local AI recognises the screen well but
does not understand requests that need a different path (example: "how do I see the turbulent CFD results?" → it
pointed at a Library row instead of CFD stage → component → Results); add a small × next to the boxes drawn by the
arrows to close the overlay when no longer needed; make the chat popup easier to move; render formulas in Jarvis
replies like LaTeX (the maintainer endorsed the KaTeX plan written by Claude Opus via Relay); dictation fails in Edge
("Dictation needs a connection"). Bounded presentation + context slice like BUBU-2; no 224/225 action authority, no
new provider, no paid API.

## In this PR (built, tested; browser check of the new UI still to do)

| Item | What | Where |
|---|---|---|
| Pointer × | Each ring gets an 18 px × (aria "Dismiss", Esc), "Clear all" when ≥ 2; overlay stays `pointer-events:none`, only the buttons are clickable and marked `data-bubu-ui` | `components/bubu/BubuPointer.tsx|.css`, `useBubuIntegration.tsx` (`onDismiss`) |
| Movable bubble | Grip bar; drag (pointer capture, 4 px threshold, viewport clamp) detaches it (no tail, persisted in `jarvisos:bubu-bubble-position:v1`); pin / double-click / Home re-attaches; arrows move it | `BubuBubble.tsx|.css`, `bubbleDrag.ts` |
| Navigation paths | Route-scoped "how to get around" guide in the visible-page block (rail, Design + per-stage addendum, Studio, Process, Memory, Development, Coding, Settings; ≤ 2.4 k chars); rules: steps in order, point only at the first step available now, number it (`[[point:id|1. CFD]]`), never Library rows for simulation results; Components-tree rows get stable ids `design.tree.component.<slug>` | `backend/app/modules/ai/thread_app_map.py`, `thread_ui_context.py`, `frontend/src/app/designTargets.ts`, `components/design/DesignComponentTree.tsx` |
| LaTeX | KaTeX 0.16.47 (pinned, bundled fonts, `trust:false`); `$…$`, `$$…$$`, `\(…\)`, `\[…\]`; currency `$5` stays text, `\$` escape, nothing inside code; same renderer in the panel and the bubble; thread prompt asks for LaTeX | `components/ai/mathText.ts`, `JarvisMessageText.tsx`, `JarvisSidecar.css`, `useBubuIntegration.tsx` (`replyText`), `backend/app/modules/ai/context_builder.py` |

Checks on the integrated branch: `npm run build` (all static tests incl. new `bubu-ui`, `jarvis-math`, `bubu-paths`,
then `tsc`, `vite build`); backend `test_bubu_app_map.py`, `test_bubu_page_context.py`, `test_ai_context_builder.py`,
`test_ai_threads.py`, `test_migration_discipline.py` (87 passed); `ruff`; typecheck ratchet PASS (301 ≤ 310);
`check_spec_status` OK. No schema change.

## Not in this PR / open

1. **Local dictation (item 5)** — built and pushed on branch `bubu3/dictation` (commit `68225289`, based on master
   `920a9b8d`; open it as its own PR or merge into this one). Follows `~/asr-local/HANDOFF-ASR-SIDECAR-2026-10-25.md`:
   MediaRecorder (webm/opus) → `POST /ai/transcriptions?workspace_id=&duration_s=` (raw audio body; `python-multipart`
   is not installed) → governed `transcribe` task via `run_ai_task` (one `ai_jobs` row per attempt) → route `local:asr`
   → `~/asr-local/transcribe` subprocess (argv, `shell=False`, timeout, private request dir under the data root,
   deleted after) → text appended to the bubble draft, never auto-sent; `GET /ai/transcriptions/status`; Web Speech
   removed. Config: `local_asr.enabled` in `configs/ai_providers.yaml`, env `JARVIS_ASR_CLI`, `JARVIS_ASR_TIMEOUT_S`,
   `JARVIS_ASR_MAX_AUDIO_S`, `JARVIS_ASR_WSL_DISTRO`. Gates: 32 new tests + 1099 in 91 relevant files, ruff, ratchet,
   `npm run build`. Live smoke (throwaway :8011, preview data, real engine, LLM resident): 12 s TED clip → correct
   text in 8.8 s cold / 5.0 s warm (≈4 s model load, ≈1 s inference); silence → `no_speech` in 4.5 s; mp3 → 415.
   **Blocking for the maintainer: the engine profile is English-only — Italian dictation needs a multilingual Whisper
   profile (language auto/it) and real tests before it is useful.** Also open: real-browser pass in Edge (permission,
   record, stop, text), cancellation does not kill the engine child, silence is logged as `provider_error/empty_response`
   in `ai_jobs`, and the token-flow record keeps the transcript text on success (review vs. privacy expectations).
2. **Pointing quality not measured.** `docs/implementation/evidence/bubu3-pointing/` has 11 scenarios, the scorer
   and the exact before/after prompts, but the live llama-server (127.0.0.1:18081) needs its per-launch key
   (`JARVISOS_LLAMACPP_API_KEY`, not exported in WSL). Run the eval with the key before/after and record scores.
3. **Browser check of the new UI** (× placement incl. labels wider than rings and rings near edges, bubble drag/
   re-attach/resize, formulas in light/dark in the panel and the bubble) — not yet done; use an isolated preview
   (`JARVISOS_DATA_ROOT` copy, uvicorn on :8010, run `initialize_database()` first; never :8000 / live data root).
4. ~~The LaTeX sentence is only in the thread envelope (`THREAD_AUTHORITY_INSTRUCTIONS`); the Hermes worker prompt
   (default chat route) does not have it yet.~~ Done 2026-10-30: the worker passes the same sentence as Hermes'
   `ephemeral_system_prompt` (`worker_shim.PRESENTATION_INSTRUCTIONS`, test in `test_hermes_runtime.py`).
5. ~~KaTeX adds ≈ +264 kB JS / +30 kB CSS (raw) to the main chunk; consider lazy-loading it.~~
   Done 2026-10-30 in #1010: the JS and CSS load on the first formula. The production build emitted separate
   KaTeX JS (258.49 kB) and CSS (29.29 kB) assets; the frontend build and exact-head CI passed.

## Supervisor review checklist

- Security: KaTeX HTML is the only `dangerouslySetInnerHTML` (trust:false, maxSize/maxExpand set); model text still
  never rendered as HTML; page text cannot forge `[[point:…]]` (defused in the backend); × buttons excluded from page
  context and user-pointing.
- Prompts: requests without `ui_context` are byte-identical (tests); guide ids checked against `designTargets.ts`.
- Frontend tests pin intent, not incidental strings; `tests/bubu-integration.mjs` relaxed one exact match.
- Known stale qualification walks from earlier UI batches (#1000/#1001/#1002/#1003) still need updating:
  `scripts/qualification/214/design_shell_browser_proof.mjs`, `216/cfd_pipe_browser_proof.mjs` (`gotoNode("Studies")`),
  `224/grounding_real_app.mjs` (R17/R20, `design.field.run`), `229/design_discovery_walk.mjs`, `230/design_visual_walk.mjs`.

## Supervisor review update (2026-10-30)

Chromium 140 on an isolated data root reached Jarvis from `/` through visible controls. Bubble drag, saved position,
resize clamp and re-attachment passed. A seeded synthetic answer rendered KaTeX in the bubble and in the full panel
in light and dark appearances; `$5` remained plain text. Pointer dismiss and Clear all passed. Edge proof found a
material defect: the global button rule enlarged the 18 px ×, and a long label overlapped it and ran off screen.
The candidate now gives the dismiss controls explicit sizing and clamps the label; Chromium measured an 18 × 18 px
× fully on screen, with zero overlap. The isolated browser evidence is kept in the coordinator's evidence directory.
This proof does not qualify pointing answer quality or Italian dictation; those remain separate work.

## Recommended next steps (priority order)

1. Finish and merge BUBU-3: review this PR, browser-check it, merge, deploy (launcher restart runs
   `initialize_database()`; after `git pull` check no tracked file is zero bytes — a WSL shutdown during a pull once
   truncated 23 files).
2. Land local dictation (open item 1) with a live smoke test on the real engine; report cold-load and per-clip latency.
3. Measure pointing (open item 2). If the local model still misses multi-step paths, route "where/how do I…" questions
   with `ui_context` to Relay/Claude (subscription) or add few-shot examples to the guide — decide on data.
4. Bring Relay answers back into the bubble (today an "Ask Claude" answer appears only in the full panel).
5. Update the stale qualification walks above so 224 grounding evidence stays meaningful.
6. Smaller UI items the maintainer listed: a Materials section (common steels + tube polymers/glasses: PET, ETFE,
   glass, polycarbonate…), Studio explanation font weight, read-aloud (TTS) for Bubu.
7. Then resume the roadmap (chat → decision → calc + CAD → FEM → swarm).
