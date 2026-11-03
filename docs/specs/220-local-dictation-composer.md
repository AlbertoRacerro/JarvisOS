# 220 — Local dictation in the Jarvis composer

State: **planned** (live state in [`STATUS.md`](STATUS.md)). Work item `JARVIS-LOCAL-DICTATION-1`. This is a definition under maintainer issue #930. **Implementation is not authorized yet.** The maintainer scheduled it after the remaining spec 216 work, unless the maintainer reorders. Acceptance and readiness must be made explicit in a later change before any implementation PR. The issue body is the source for the engine facts below; its host-side handoff lives at `/home/user/asr-local`, outside Git and not visible from `jarvis-sandbox`. Code references were re-checked on this branch (based on `origin/master`).

## Outcome

A microphone button next to the existing composer in `frontend/src/components/ai/useJarvisSidecar.tsx` (the `jarvis-prompt` textarea inside the `jarvis-sidecar__composer` form):

1. Click: browser permission, then recording with a visible timer.
2. Stop: the complete audio goes to the local JarvisOS backend for transcription only.
3. The text lands in the same composer as an editable draft. The user edits it or presses the normal Send; the existing Jarvis/Relay target (`composerTarget`) and guards then apply unchanged.

Stop never sends anything to an assistant.

## Scope

In scope:
- Browser capture and composer integration (below).
- A minimal typed audio extension of `run_ai_task` with a local ASR binding.
- A subprocess adapter around the existing host ASR batch CLI.
- An Italian profile and quality qualification on the maintainer's own audio (before any quality claim).

Out of scope (binding non-goals):
- Wake word or always-on listening; auto-send on Stop.
- Web Speech API; any remote or paid ASR fallback.
- Archiving audio or text, or promotion to memory. This is distinct from REF-067 (meeting capture and archiving); it is not REF-067.
- Correcting ASR errors with an LLM; changing the lecture-transcription app's defaults.
- A second gateway, ledger or scheduler; a warm ASR session (see Latency).
- Streaming or partial transcripts; the audio is uploaded complete.

## Existing local engine (host, outside Git)

- WSL, isolated venv, CUDA only with no CPU fallback. Whisper Large-v3 Turbo via Faster-Whisper 1.2.1 / CTranslate2 4.8.3, plus Silero v6.2.3 VAD on CUDA.
- A batch CLI, not an HTTP service. Contract: exit code plus a JSON/TXT result file. It keeps per-chunk checkpoints and holds a global file lock.
- Measured 2026-10-24 (TEDLIUM, about 60 min, English): Whisper Turbo + VAD WER 2.998 %, CER 1.832 %, load about 7 s; Parakeet v2 WER 3.25 %, load about 77 s; Parakeet Unified WER 3.24 %, load about 74 s. Whisper Turbo needed about 2 GiB extra VRAM.
- The VAD is required: raw Whisper hallucinated "Thank you" on silence and the VAD drops those chunks.
- A separate Windows lecture-transcription app already shares the engine ("one engine, two clients").
- These measurements are issue-reported and were not re-verified from the sandbox.

## Contract

### 1. Typed `run_ai_task` audio extension

`run_ai_task` (`backend/app/modules/ai/execution.py`, `def run_ai_task`) is text-only today (`user_prompt`, `context_blocks`). Product AI calls go through it and create an `ai_jobs` row (`AGENTS.md` invariant 2; insert in `execution.py`). The slice adds the minimum:

- **Audio input:** a typed, server-side reference to a stored audio file in the per-request private directory. It is never inline bytes, a client-supplied path or a URL.
- **Local ASR binding:** a `ProviderBinding` (resolved by `resolve_binding`) for a local ASR route class, with no network requirement.
- **Outcome and evidence:** `AiTaskOutcome` carries the transcript text plus bounded evidence (engine id and version, language/profile, audio duration, latency, VAD result, exit classification). The `ai_jobs` row records digests, durations and status, not audio or full transcript text.
- **No second gateway or ledger.** The existing `ai_jobs` ledger and gateway path are reused.
- **Local only.** A dictation request never reaches an `external:` or other network route. `route_class="auto"` (dispatched by `backend/app/modules/ai/gateway.py` to `routing/bridge.py` `run_auto_task`) must never fall back to a remote or paid provider for audio; if the local ASR binding is unavailable the outcome is a typed failure.

### 2. Subprocess CLI adapter

- Runs the existing CLI as a subprocess: argument list, `shell=False`, explicit timeout, bounded environment.
- Works in a private per-request directory under the JarvisOS data root, via `backend/app/core/paths.py` (`JarvisPaths`, `resolve_paths`). The directory holds the uploaded audio and the result file.
- Reads only the structured result file and the exit code; never parses log text.
- Personal paths (engine location, venv, model cache) are local configuration, not committed.
- Typed errors, mapped to user messages: silence (no speech after VAD), missing CUDA, lock held or GPU busy, timeout, bad audio.
- Cleans up the request directory after the outcome, including on error and timeout.

### 3. Backend upload

A bounded, authorized upload endpoint accepting WebM/Opus (preferred) and other negotiated formats it can decode; enforces duration/size limits and MIME allow-list; binds the request to workspace and thread. Exact route, limits and formats are fixed at readiness.

### 4. Browser capture

- `getUserMedia` plus `MediaRecorder` with a negotiated MIME type (WebM/Opus preferred); no Web Speech API.
- Wait for the final `dataavailable` before upload.
- Release all tracks on cancel, error or unmount.
- Visible timer; clear message on denied permission or no device.

### 5. Composer rules

- Append, never overwrite, using a functional `setPrompt` update.
- Bind the result to workspace, thread and target; drop a late result after a context switch and offer recovery.
- Disable Send while a dictation is pending.
- Never silently truncate past the 12,000-character limit (the textarea `maxLength` and the backend `_MAX_PROMPT` in `thread_service.py`).
- Clear messages for silence, denied permission, missing CUDA, busy GPU.
- No change to Relay guards or the Jarvis/Relay target semantics.

### 6. Privacy

Dictation audio is temporary and removed after the outcome. No audio or full transcript text in ordinary logs.

### 7. Language, GPU contention, deployment, latency

1. **Italian.** The engine is forced to English and qualified only on TED. The maintainer dictates mostly Italian with English technical terms (Reynolds, NPSH, baffle). An Italian profile and a WER/CER test on the maintainer's own audio are required before any quality claim. Do not change the lecture app's default.
2. **GPU contention.** The RTX 5070 Ti is shared by Gemma, ASR and future GPU CFD; the ASR lock covers only ASR. The durable job owner from #918 (spec 218, `scripts/relay_jobs.py`) should expose GPU occupancy so dictation answers "GPU busy with a simulation, retry" rather than failing arbitrarily. Reuse the job owner; build no scheduler.
3. **Deployment.** The backend most likely runs in WSL (127.0.0.1:8000, Linux launcher) and would call the engine directly with no `wsl.exe` bridge. Verify at implementation. localhost is a secure context for microphone permission.
4. **Latency.** The first version keeps the ephemeral process (about 7 s load plus inference). Measure Stop-to-text p50/p95 before considering a warm, opt-in session with idle unload.

## Open questions for the maintainer

1. How does the sandboxed backend reach the host engine at `/home/user/asr-local`, which is not visible from `jarvis-sandbox`? (Direct call from a WSL-hosted backend, a mounted path, or another route; decide at readiness.)
2. Does GPU occupancy come from the spec 218 job registry only, or must the adapter also probe the GPU/lock?
3. Naming and shape of the local ASR route class and of the audio reference type.
4. Upload limits (maximum duration and bytes) and the accepted audio formats.
5. Is the ASR CLI's result-file schema stable enough to pin, or does the adapter need a version check?
6. Which Italian clips form the qualification set, and what WER/CER bar counts as acceptable?

## Files likely touched

Verify against actual code at readiness.

- `backend/app/modules/ai/execution.py`, `execution_types.py`, `gateway.py`, `routing/bridge.py`
- `backend/app/modules/ai/providers/` (new local ASR adapter), `provider_registry.py`
- `backend/app/core/paths.py`, `backend/app/core/config.py`
- a new upload route module under `backend/app/modules/ai/`
- `frontend/src/components/ai/useJarvisSidecar.tsx` and a new capture hook/component
- `docs/specs/STATUS.md`

## Acceptance criteria

1. Stop transcribes locally and inserts editable text into the composer; nothing is sent until the normal Send.
2. Every dictation goes through `run_ai_task`, creates one `ai_jobs` row, and never uses a network route, including under `route_class="auto"`.
3. The adapter uses an argument list, `shell=False`, a timeout, a private per-request directory under the data root, and reads only the structured result; the directory and audio are removed after every outcome.
4. No audio or full transcript in ordinary logs or the `ai_jobs` row.
5. Composer rules in section 5 hold, including append-only, late-result drop, Send disabled while pending, and no silent truncation past 12,000 characters.
6. Tracks are released on cancel, error and unmount.
7. Typed user messages exist for silence, denied permission, missing CUDA and busy GPU.
8. No change to the lecture app default; no LLM post-correction.

## Required evidence

- **Fake-backed backend tests in CI**, using a fake ASR binary (no GPU, no model): upload contract, limits, formats, paths, authorization, `ai_jobs` row, CUDA and lock errors, timeout, cleanup, no-remote-fallback under `auto`.
- **React tests:** draft edited while waiting, late result, workspace/thread/target switch, cancel, character limit, no auto-send, Relay guards unchanged, track release.
- **Real browser on the deployed app:** permission, record, Stop, text in the same box, edit and send verified separately, denied permission or no device.
- **Real local CUDA run on the host (environment evidence):** short Italian clips with noise and technical terms; CUDA use and VRAM release; Stop-to-text latency; concurrency with the lecture app. Never interrupt CFD, Relay or the supervisor to test. This cannot run in CI or the sandbox.

## Definition of done

Readiness made explicit, test gate green (see `AGENTS.md`), acceptance criteria met, the host CUDA run recorded as environment evidence, `STATUS.md` updated with the implementation PR.
