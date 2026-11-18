# 302 — SAGE-DEFAULT-1: Strata-served model as the standard local model, with one click back

Status: merged
Depends on: none

## Goal

The local model registry can name a Strata-served model as its `default`. At backend start the backend starts that
model through a host-ops `service.control` request (never its own process), routes local product calls (chat, in-app
agent, every `local:*` route that today reaches llama.cpp) to it, and shows its state truthfully: loading, ready,
stopped or failed. One action in the existing local model panel switches to the llama.cpp model (Gemma) and one
switches back; the choice survives a restart. Only one model is hot at a time on the 12 GB card.

## Why

Maintainer directive L20261116155611-930c point 3: Sage replaces Gemma as the standard local model in the product,
not only as a selectable model, after memory and start-up time are checked on the 12 GB card, with a one-click way
back. It amends the 2026-11-07 directive S1, where Gemma stayed the default and Sage was the maintainer's choice; S1's
rule that start and stop go through host-ops typed operations, never through direct process control by the backend,
still binds. Sage scored 33/34 on the Q1 semantic set against 12/34 for Gemma.

## Code facts checked at `4d2fd1f2`

- `backend/app/modules/local_ai/runtime/model_registry.py::read_registry` accepts runtimes `llama_cpp` and `strata`
  but refuses a registry whose single `default` entry is not `llama_cpp`.
- `backend/app/modules/local_ai/runtime/model_registry.py::activate_model` refuses a `strata` entry with 409 "This
  model requires host start"; `backend/app/modules/local_ai/runtime/model_registry.py::config_for_entry` keeps the
  default's alias for product calls; `backend/app/modules/local_ai/runtime/model_registry.py::restore_default`
  re-activates the default.
- `backend/app/modules/local_ai/runtime/llama_cpp.py::LlamaCppRuntimeOwner.switch_model` stops and respawns
  llama-server under the backend; `backend/app/modules/local_ai/runtime/llama_cpp.py::LlamaCppRuntimeOwner.start`
  spawns it; `backend/app/main.py::lifespan` calls that start when the llama.cpp config has `manage` set.
- `backend/app/modules/ai/providers/local_llamacpp_adapter.py::LocalLlamaCppAdapter.complete` posts to the
  llama.cpp config `base_url`, refuses product calls while a non-default model is active, and already separates
  `reasoning_content` from the answer; `backend/app/modules/ai/providers/local_llamacpp_adapter.py::LocalLlamaCppAdapter.health`
  probes `/health`.
- `scripts/host_ops.py::op_service_control` starts, stops or restarts a unit listed in the host-only policy;
  `scripts/host_ops.py::make_request` and `scripts/host_ops.py::submit` write a `jarvisos.hostops.request.v1` file into
  the spool; `scripts/host_ops.py::request_problem` is the executor's request validator.
- `frontend/src/components/coding/LocalModelPanel.tsx::LocalModelPanel` lists registered models and their state.

Host facts (ledger, not code): the Sage unit, its config and its loopback port exist on the host and the unit is in
the host-ops `service.control` allowlist; the Q1 smoke test measured about 151 s to ready and 10.8 GB on the GPU with
the app stopped, and the Q1 run peaked at 23.76 of 25 GB WSL memory.

## Scope

In scope:
- Registry: a `strata` entry may be the `default`. A `strata` entry carries `unit` (a systemd user unit name,
  `^[a-zA-Z0-9@._-]+\.service$`), `port` (loopback) and `served_model` (the name the server answers to); a
  `llama_cpp` entry keeps today's fields. Still exactly one `default`.
- Model choice: the active choice is stored in one small JSON file under the data root (`paths.py`), written only by
  the activate/restore routes; at start the backend uses the stored choice when it names a registered
  `default`/`qualified` entry, otherwise the registry default.
- Host start: a small backend writer produces a host-ops request (`service.control`, `{unit, action}`) in the spool
  named by `JARVISOS_HOSTOPS_SPOOL`. Without that variable, or without the spool directory, `strata` entries stay
  "requires host start" exactly as today and the backend starts the llama.cpp entry instead: the core must run
  without host-ops.
- Readiness is judged by the backend's own loopback probe of the entry (`GET /v1/models` on its port, model name
  present), not by trusting a receipt. States: `stopped`, `loading` (request submitted or port not answering yet,
  with elapsed seconds), `ready`, `failed` (receipt outcome other than `ok`, or no readiness within a bounded time,
  default 600 s).
- One hot model: activating a `strata` entry first stops an owned llama-server; activating a `llama_cpp` entry first
  requests `stop` of the running `strata` unit and waits until its port no longer answers before spawning. Switches are
  serialized under the existing owner lock.
- Product calls: the llama.cpp adapter sends to the active entry's endpoint and served name. The refusal "default
  local model is not loaded" applies only while an `under_qualification` entry is active; a `default` or `qualified`
  entry serves product calls. The `ai_jobs` row keeps its binding `model_id` (token-flow evidence checks it) and records the served model's name as `served_model_id` in its route reason. While the
  active model is `loading`, a product call fails retryable with the message that the local model is still loading.
- Interface: `LocalModelPanel` shows each entry's state in plain Italian (in caricamento, pronto, fermo, errore) and
  offers "Usa questo modello" on the inactive one and "Torna al modello standard"; no unit names, ports or ids in
  visible text.
- Host steps for the maintainer's chat session, written in the PR: set `JARVISOS_HOSTOPS_SPOOL` for the app, add the
  three Sage fields to the data-root registry and move `default` to Sage, keep Gemma as `qualified`.

Out of scope (binding non-goals):
- Changing the host-ops executor, its policy or the unit file; adding any other host operation.
- Chat and model-selection redesign (directive point 4: the maintainer will give it separately).
- Showing reasoning collapsed under the answer (already separated by the adapter; a display slice comes with the chat
  changes).
- Starting a second model while one is hot, or any automatic switch on load or failure.
- Ollama routes and the local classifier.

## Files likely touched

- `backend/app/modules/local_ai/runtime/model_registry.py`
- `backend/app/modules/local_ai/runtime/llama_cpp.py`
- a new `backend/app/modules/local_ai/runtime/host_start.py` (request writer and readiness probe)
- `backend/app/modules/local_ai/runtime/routes.py`
- `backend/app/modules/ai/providers/local_llamacpp_adapter.py`
- `backend/app/core/paths.py`, `backend/app/main.py`
- `frontend/src/components/coding/LocalModelPanel.tsx`, `frontend/src/api/llamaRuntime.ts` and its test
- `backend/tests/test_local_model_registry.py` (or the existing registry test file) and a new host-start test

## Design constraints

- The backend never runs `systemctl`, never spawns or signals the Strata process and never reads the host-only
  policy or key. Its only host action is writing a request file; it does not import `scripts/`.
- The request it writes must pass `scripts/host_ops.py::request_problem` (pinned by a test that imports the script
  from the test only).
- Endpoints stay HTTP loopback (`validate_loopback_host`); no credential is sent to Strata and no new secret is stored.
- Registry, choice file and spool path come from the data root or environment, never from a path in code.
- A registry without a `strata` entry behaves exactly as today (existing tests unchanged except where they pin the
  old `default must be llama_cpp` refusal).
- No schema migration.

## Acceptance criteria

1. A registry with a `strata` default and a `llama_cpp` qualified entry is accepted; two defaults, a `strata` entry
   with only some of `unit`/`port`/`served_model`, a `strata` default without them, a non-loopback port or a bad unit
   name are refused with 503 as today. A non-default `strata` entry listed by file only (the shape accepted before this
   spec) stays valid and "requires host start", so a registry is not broken by the upgrade before the host steps.
2. With the spool configured, backend start with a `strata` default writes exactly one `service.control` `start`
   request for its unit, spawns no llama-server, and reports `loading` until the probe sees the served model, then
   `ready`.
3. Without the spool, the same registry starts the `llama_cpp` entry and reports the `strata` entry as needing host
   start; nothing is written.
4. Activating Gemma while Sage is ready writes a `stop` request, waits until the Sage port stops answering, spawns
   llama-server, and stores the choice; activating Sage while Gemma runs stops llama-server before writing `start`.
   Restarting the backend restores the stored choice.
5. A product call while Sage is ready reaches the Sage endpoint with the served name and no auth header, and the
   `ai_jobs` row records that model as `served_model_id`; while `loading` it fails retryable with the loading message; while an
   `under_qualification` entry is active it is refused as today.
6. A failed receipt or no readiness within the bound gives `failed` with a plain reason, and the panel offers the way
   back.
7. The panel shows the four states and the two actions in Italian with no unit name, port or id; its `.mjs` test
   asserts the labels.
8. Live proof with the app idle, on the maintainer's machine after the host steps: start-to-ready time, GPU memory and
   WSL memory headroom with the app and a browser open during a long answer are recorded; if headroom is under about
   1.5 GB the context is lowered by a host step and the trade-off recorded; Sage to Gemma to Sage switch succeeds by
   the panel.

## Required tests

- Registry validation cases of criterion 1.
- Host-start writer: request shape accepted by `request_problem`; no write without spool; readiness states driven by a
  fake probe (loading, ready, timeout to failed, failed receipt).
- Owner switch order with a fake popen and fake probe (criterion 4), including the stored choice across a new owner.
- Adapter routing to the active entry, job model id, loading refusal, `under_qualification` refusal (criterion 5).
- Frontend `.mjs` test for the panel labels.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met with criterion 8 proven live, high-risk evidence (second
session diff review), spec status updated.
