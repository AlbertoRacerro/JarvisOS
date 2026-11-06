# 250 — Local model registry and switch for Q1

Status: ready
Depends on: 247

## Goal

Jarvis lists the local models the host has registered (the default, qualified ones and ones under qualification) and
shows which one is loaded. The operator can switch the single hot llama.cpp model to another registered entry, stop it,
or restore the default, all from Coding → Runtime. Gemma stays the default responder.

## Why

Q1 compares local models on 12 GB VRAM, so only one model can be hot at a time. Spec 247 exposes start, stop and restart
for the one model in `llama_cpp.json`, but there is no way to see or choose others. Maintainer directive
`L20261105222120-78af` item 3 (option 3 of question `L20261105130109-901c`, adapted to serve Q1).

## Scope

In scope:
- A host-written registry file `data-root/settings/local_models.json`. Each entry has `id`, `label`, `runtime`
  (`llama_cpp` or `strata`), `qualification` (`default`, `qualified` or `under_qualification`), `model_path`, pinned
  `model_sha256`, and optional per-model `ctx_size`, `n_gpu_layers`, `extra_args` and `thinking`. Exactly one entry is
  `default`. The binary, host, port, key and library settings stay in `llama_cpp.json` and are shared.
- A read route `GET /local-ai/models` returns the entries (id, label, runtime, qualification, model file name,
  installed, digest state) and the active id.
- `POST /local-ai/models/{id}/activate` stops the current llama.cpp process and starts the selected `llama_cpp` entry with
  its own path and parameters under its own `id` as alias. `POST /local-ai/models/restore-default` does the same for the
  default entry. Stop stays the Spec 247 route.
- Before starting, the backend refuses an unknown id, a missing file, a non-`llama_cpp` runtime, or a digest that is
  known to differ from the pinned `model_sha256`. The refusal is a clear error and leaves the current process running.
- The active selection lives in backend memory only. A backend restart returns to the default entry.
- The Runtime panel shows the list with qualification badges, marks the active entry, and offers Activate on non-active
  `llama_cpp` entries and Restore default when a non-default entry is active. Activate and Restore default ask for one
  confirmation and follow the same single-action rule as Spec 247.
- Without a registry file, the panel and routes behave exactly as Spec 247, with only the `llama_cpp.json` model.

Out of scope (binding non-goals):
- Starting Strata. `strata` entries are listed with "requires host start" and cannot be activated here. Strata start is a
  follow-up once it is installed and its command line is checked on the host.
- Writing or editing the registry from the app or the sandbox, downloading models, or computing digests on request.
- Changing provider routing, the paid-AI defaults, the Ollama runtime, or which model product AI calls use by default.
- A host operation for Q1 runs (stop Gemma, run, restore). That is the separate Q1 execution spec.
- Schema changes.

## Files likely touched

Verify against actual code before starting; report conflicts instead of guessing.

- `backend/app/modules/local_ai/runtime/llama_cpp.py` (start with an override config) and a small registry module beside it
- `backend/app/modules/local_ai/runtime/routes.py` or a sibling routes file, registered in `backend/app/main.py`
- `backend/tests/` for the registry and routes
- the Spec 247 frontend panel and its API module; `frontend/tests/` and `frontend/package.json`
- `frontend/src/api/generated` via `scripts/generate_frontend_contracts.py`
- `e2e/` for a browser walk

## Design constraints

- One hot model: every activation stops the previous process first and never starts a second llama-server.
- Product AI calls must not silently reach a non-default model. While a non-default entry is active, the local provider
  reports the default model as not loaded and its calls fail as unavailable, as they do when the server is stopped.
- The registry is read-only to the app. Paths come only from it and from `llama_cpp.json`, never from the request body.
  Activation takes only an id.
- The sandbox gets no new process control. Agent-side switching for Q1 goes only through the typed host operation in the
  Q1 execution spec.
- Never show the API key, environment values or full filesystem paths. Show model file names only.
- No auto-start, no retry loop, and no POST on page load.

## Acceptance criteria

1. With a registry of three entries (default, qualified, under qualification), the panel lists all three with badges
   and marks the active one.
2. Activating an installed `llama_cpp` entry stops the current process and starts the selected one. The status then
   reports that entry's id and file. Restore default brings back the default entry.
3. Unknown id, missing file, `strata` runtime and known digest mismatch are refused, and the running process is untouched.
4. While a non-default entry is active, a product chat call through the local provider fails as unavailable and does
   not reach the non-default model.
5. Without a registry, behaviour equals Spec 247.
6. Opening the page performs no POST.

## Required tests

- Offline backend tests with a fake `popen` and fake HTTP cover registry parsing (including invalid files and a missing or
  duplicate default), each refusal, stop-before-start ordering, restore default, in-memory selection reset, and the
  product-call refusal while a non-default entry is active.
- Offline frontend tests cover the list, badges, active marker, button availability, confirmation, single in-flight
  action and the no-registry fallback.
- A clean-profile browser walk from `/` on a temporary data root, with request interception serving fixture bodies.
  It never starts a real model. Keep its evidence directory.
- The first real switch on the maintainer's machine is a live activation step after merge, done when the app is idle,
  recorded separately, and ending with the default restored.

## Definition of done

Acceptance criteria pass, along with the relevant tests and lint. Required CI, one severe diff review and the UI browser
walk pass on the exact head. The registry is updated as the work advances. Accepted under maintainer directive
`L20261105222120-78af` item 3 (medium risk).
