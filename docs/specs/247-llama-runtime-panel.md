# 247 — Local model (llama.cpp) status and start/stop in Coding → Runtime

Status: ready
Depends on: none

## Goal

From Jarvis, the operator sees whether the local llama.cpp model is configured, running and loaded, and can start, stop or
restart it without a terminal.

## Why

The backend owns the llama.cpp process through `GET /local-ai/runtime/llama-cpp` and
`POST /local-ai/runtime/llama-cpp/{start|stop|restart}` (`backend/app/modules/local_ai/runtime/`). No screen uses them.
Maintainer choice: option 3 of question `L20261105213009-3f9a`, answered in decision `L20261105214933-cb50`.

## Scope

In scope:
- A "Local model" panel in Coding → Runtime. It shows: configured or not with the backend `message`; reachable; model
  loaded; `reason_code`; model file name; context size; GPU layers; VRAM/RAM when reported; whether Jarvis spawned the
  process; crash count and last crash time.
- Start, Stop and Restart buttons, enabled only when the reported state makes them meaningful. Stop and Restart ask for
  one confirmation. While an action runs, all three are disabled and a visible pending state is shown. After the action,
  the panel shows the returned status.
- A manual refresh. Polling only while the panel is visible and an action or load is in progress, with a bounded interval.

Out of scope (binding non-goals):
- Backend, route, settings or schema changes. `verify-digest`. Editing `llama_cpp.json`. Model choice. Any change to
  provider routing, the API key, or how other features use the local model.
- The Ollama runtime.

## Files likely touched

Verify against actual code before starting; report conflicts instead of guessing.

- `frontend/src/pages/CodingWorkbench.tsx` (Runtime area) or a focused component beside it
- a small frontend API module for the two routes
- `frontend/tests/` and `frontend/package.json`
- `e2e/` for a browser walk

## Design constraints

- The browser only calls the JarvisOS backend routes above. It never contacts llama-server, a provider or the filesystem
  (AGENTS.md, Never 3).
- Show only the fields listed above. Never show request headers, the API key, or environment values.
- When not configured, say so with the backend message and disable Start. A failed action shows its error and leaves the
  last known status labelled as such. An error never reads as "stopped".
- No action starts on its own: no auto-start on page load and no retry loop.

## Acceptance criteria

1. From a clean profile at `/`, the operator reaches Coding → Runtime through visible controls and sees the local model
   panel with the reported state.
2. With a not-configured runtime, the backend message is shown and Start is disabled. With a reported running state,
   Stop and Restart are available, ask for confirmation, and show the returned state.
3. Loading, pending action, failure and refresh are visible. Concurrent clicks cannot send two actions at once.
4. Opening the page performs no POST.

## Required tests

- Offline frontend tests cover state-to-button mapping, confirmation, single in-flight action, error vs stopped, the
  not-configured message, and the absence of any auto-start.
- A clean-profile browser walk from `/`, on a fresh temporary data root without a llama.cpp configuration, checks the
  not-configured state and that no POST is sent on load. The running/stop path is covered with a request interception
  that serves fixture status bodies. The walk never starts a real model. Keep its evidence directory.
- The first real Start/Stop on the maintainer's machine is a live activation step after merge, recorded separately.

## Definition of done

Acceptance criteria pass, along with the relevant tests and lint. Required CI, review and the UI browser walk pass on the
exact head. The registry is updated as the work advances. Accepted under maintainer directive `L20261105144536-3af0` item 3
and decision `L20261105214933-cb50`.
