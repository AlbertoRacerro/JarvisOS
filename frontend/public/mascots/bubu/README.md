# Bubu — static mascot assets, v1

Five isolated poses prepared from the maintainer-provided Bubu reference.

**BUBU-LAUNCHER-1 follow-up:** `BubuLauncher.tsx` now uses the standard `bubu-listening.webp` as a static movable shortcut to the existing Jarvis. No microphone or activity state is implied; the other four poses remain unbound. The preparation notes below preserve the original asset-only task and its checks.

Original preparation scope: This directory contains **assets only**: no mascot controller, UI wiring, STT/TTS, capture, navigation, issue publication or supervisor behavior is added.

## Files and future pose assignment

| Pose key | PNG / WebP basename | Intended use requested by the maintainer | Reference figure |
| --- | --- | --- | --- |
| `listening` | `bubu-listening` | Standard/present/listening illustration | Large character on the left |
| `thinking` | `bubu-thinking` | Thinking, designing or calculating | Small upper-right character with holographic screen |
| `clarifying` | `bubu-clarifying` | Asking a clarification or authorization | Small lower-left character with question mark |
| `sending-issue` | `bubu-sending-issue` | Sending an issue | Small joyful upper character, left of the screen character |
| `sleeping` | `bubu-sleeping` | Off/asleep | Small bottom-right character with Z marks |

The winking reference figure has no assigned role and is deliberately excluded. These are suggested future assignments, not runtime state transitions. The standard pose does not itself indicate that the microphone is recording. The sending pose does not prove publication succeeded.

## Format and alignment

- Every final pose has a transparent **512 × 512** canvas.
- PNG files use **RGBA** and are the lossless interchange versions.
- WebP files use **lossless encoding with alpha**, suitable for future browser consumption.
- The common bottom anchor is **(256, 472)** pixels; both formats use exactly the same canvas and placement. Keep the whole canvas when changing poses to avoid a changing external anchor.
- Aspect ratio is preserved. Standing/seated poses fit within 440 × 440 pixels. The sleeping pose uses a 392-pixel width limit to keep the head visually comparable while preserving its naturally shorter silhouette.
- Question mark, sleep Z marks, the holographic screen and the sending pose's joy rays remain part of their respective assets. Ground shadows and the contact-sheet backdrop are excluded.
- `manifest.json` lists filenames, future public URLs, descriptions, canvas geometry, visible bounds, provenance and SHA-256 digests. It is not imported by product code.
- `docs/implementation/assets/bubu-poses-preview.png` (kept out of the served `public/` folder) shows the actual exported PNGs on light and dark backgrounds for visual checking. It is a proof sheet, not a mascot pose.

Future static URLs follow `/mascots/bubu/bubu-<pose>.webp` or `.png` after these files are integrated/deployed with the frontend. The launcher consumes only the standard WebP; the remaining poses await state integration. Prefer a fixed-size image box, preserve aspect ratio, and use the same box for all poses. These are static illustrations, not an animation sprite sheet.

## Provenance and preparation

Reference: maintainer-provided `Immagine ChatGPT 28 ott 2026, 09_11_50.png`. Its digest is recorded in the manifest. No third-party product artwork, fonts, prompts or proprietary assets were used.

Mode: built-in **image_gen**, background-extraction edit, one call per pose. Exact prompts are preserved in `prompts.json`. The results are faithful generative cutouts derived from the supplied illustration; they are **not pixel-identical crops** of the source sheet. The generated originals remain in the tool's original output location, and final normalized assets are saved here.

Mechanical finishing used Pillow 12.0.0: alpha values up to 8/255 were cleared to remove nearly invisible background speckles, the visible alpha box was cropped with three pixels of source padding, aspect-preserving LANCZOS resizing was applied, and the pose was composited on the shared transparent canvas. PNG and WebP were then exported from the same RGBA pixels. No new pose or illustration was created by the normalization script.

## Scope and validation

The maintainer's pasted Claude notes were read as context only. No recommendations or implementation work described there were executed. Earlier architecture audit changes in the worktree are unrelated and remain pending.

Asset checks cover: decode, dimensions, real/partial alpha, transparent canvas border, matching PNG/WebP pixels, anchor/bounds, file digests, manifest references, five requested poses, light/dark proof sheet and small display sizes. Backend/frontend behavior tests do not validate these isolated files; no application behavior is claimed.

The system Python initially lacked Pillow (`ModuleNotFoundError: No module named 'PIL'`). Image finishing uses an isolated `uv` environment with `Pillow==12.0.0`; no repository dependency was added. `uv --no-project` emits a harmless no-project warning in this checkout. During asset preparation the worktree had no `frontend/node_modules`, and native Linux `node` had not been located; full frontend build availability is reported separately from asset validation.

Validation performed for this asset preparation:

- All five PNG/WebP pairs: PASS for decode, 512 × 512 size, real and partial alpha, fully transparent outer border, common baseline, exact pixel equality between formats and matching SHA-256/byte counts.
- Manifest/prompt coverage: PASS for the five requested states and exclusion of the unassigned winking figure.
- Visual inspection: light/dark proof sheet plus 48/80-pixel canvas previews inspected. No clipping or background panel observed; small details are naturally less legible at 48 pixels.
- `git diff --check`: PASS; new text files additionally checked directly because untracked files are not included in that command.
- `npm run build`: attempted, **FAIL, exit 38**. The Windows npm script runner invoked `CMD.EXE` from a WSL UNC directory; CMD does not support that working directory, fell back to Windows and failed with `ENOENT` reading `C:\Windows\package.json`. The chained frontend checks/TypeScript/Vite build did not complete. This is an environment failure, not a successful product validation.
- Backend tests: not run; no backend files or behavior changed.

No runtime assignment, issue publication, architectural implementation, commit or supervisor restart was performed.
