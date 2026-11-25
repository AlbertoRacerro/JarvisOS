# 371 — HERMES-CALC-RUN-1: Let the chat compute with short Python in the offline calculation sandbox

Status: ready
Depends on: 368, 370

## Goal

In the Jarvis chat, Hermes can compute a number it needs (an ODE integration, a unit conversion, a balance, a fit) by
sending a short Python function to the existing offline calculation sandbox and reading back a compact result. The
answer's numbers then come from a tool result, so the number-provenance check can verify them. Questions that need
a calculation get this tool from the turn router on every surface.

## Why

Directive `L20261124193224-8c59` and `L20261124094804-a929` item 1 (all Jarvis tools connected to the chat; "if a
model does better outside Jarvis than inside, the harness is the problem"). The private agent bench loop that scored
10.75/16 on the hard set gave the model a Python tool. Through the real chat (spec 369 gate,
`j-20261125T014640Z-82ef20`) the same model scored 4.9/16; in the one-day tank simulation task it called only
`jarvis_turn_plan` for all 10 steps and answered from mental arithmetic with a wrong peak temperature. Off a design
surface the chat has no tool that computes: `bench_run` runs fixed evaluators and is granted only on the CAD surface.
Directive `L20261124012919-7d88` already describes Jarvis as giving the local model "tools and solvers".

## Code facts checked at `8defbd90`

- `backend/app/modules/studio_runtime/runtime.py::run` runs a `RunRequest(source, inputs, limits)` with the
  `calculation` profile in a fresh bubblewrap sandbox: `--unshare-all` (no network), `--clearenv`, read-only
  `/usr` and backend venv, a 4 MiB tmpfs scratch, 15 s wall, 12 s CPU, 1 GiB memory, 32 processes, no artifacts.
  It is fail-closed: `status()` reports `enabled` from `Settings.studio_user_code_enabled`
  (`JARVISOS_STUDIO_USER_CODE_ENABLED=1`) and `available` from the bwrap probe; otherwise the record classification
  is `disabled` or `unavailable` and nothing runs.
- `backend/app/modules/studio_runtime/worker.py::main` executes the source and calls `main(inputs)`; the return value
  must be JSON. `RunRecord` carries `classification`, `result`, `stdout`, `stderr` (bounded logs) and `limit`.
- The backend venv has numpy 2.5 and scipy 1.18.
- `backend/app/modules/agents/hermes/supervisor.py` declares each Hermes capability as a
  `JarvisCapabilityDescriptor`; `jarvis.bench_run` is `EXECUTE` with its own grant outside the common registry and
  is dispatched by identity in the tool dispatch branch (`dispatch_bench_tool`). The tool-name to capability map and
  `HERMES_TOOL_ALLOWLIST` list every admitted tool; `worker_shim.py` keeps the matching MCP allowlist.
- `backend/app/modules/agents/hermes/broker_mcp.py` defines each tool's MCP schema and one-line description.
- `backend/app/modules/ai/thread_service.py::_install_surface_grants` issues `bench_run` grants only for the
  `bluecad` surface; `_turn_tools` picks at most seven tools by surface and word groups and has no calculation group.
- `backend/app/modules/engineering/bench.py::AGENT_RUNNABLE_EVALUATORS` excludes `bluecad.solid_code`: spec 285
  kept model-written code out of the agent tool "in this slice". This spec opens the calculation profile only,
  never the geometry profile.

## Scope

1. **Tool.** New MCP tool `jarvis_calc_run` with capability `jarvis.calc_run` (action class `EXECUTE`, own grant,
   admitted by identity like `bench_run`). Arguments: `grant_id`, `source` (Python defining `main(inputs)`, at most
   16,384 chars), `inputs` (JSON object), `purpose` (at most 200 chars). Description, one line: compute numbers with a
   short Python `main(inputs)` (numpy and scipy available) in an offline sandbox; prefer `bench_run` when an
   evaluator fits.
2. **Execution.** Dispatch calls `studio_runtime.runtime.run(RunRequest(source, inputs), profile="calculation")` with
   default limits. Never the geometry profile, never an artifact directory, never a host path in the request. When
   the runtime is disabled or unavailable the tool returns error `calc_unavailable` and runs nothing.
3. **Compact result to the model.** `classification`, `result` bounded by `compact_tool_payload`, the last 1,000
   chars of `stdout`, on `error` the last 600 chars of `stderr`, `limit` when one was hit, `duration_seconds`, and
   `source_sha256`. No host path.
4. **Grant on every chat turn.** Each Hermes interaction gets one `jarvis.calc_run` grant whatever the surface,
   with the same lifetime as the surface grants.
5. **Router.** `_turn_tools` gains a calculation group placed first among the word groups: words `calculate`,
   `calculation`, `compute`, `simulate`, `simulation`, `solve`, `integrate`, `convert`, `estimate`, `fit` select
   `calc_run`. When no word group matches, the default set is `calc_run`, `paper_search`, `browser_page_read`
   (replacing `paper_pdf_read`). The list stays at most seven names.
6. **Prompt line.** `COMPACT_METHODOLOGY` names `jarvis_calc_run` as the tool for arithmetic and simulation instead
   of computing mentally. The base prompt plus tool schemas stays under the spec 368 first-step size bound.

Out of scope (binding non-goals):

- The geometry profile, artifacts, files, network, package installation or any persistent state from the sandbox.
- Changing the sandbox limits, `studio_runtime` code or the enable flag; enabling it is an operator setting.
- Adding the tool to cloud routes' prompts beyond the shared allowlist; promotion of any result into project state.
- `bluecad.solid_code` in `AGENT_RUNNABLE_EVALUATORS`.

## Constraints

- High risk (execution of model-written code): the only execution path is `studio_runtime.runtime.run` with the
  `calculation` profile; a test proves no other runner, subprocess or profile is reachable from the tool.
- Arguments are validated before execution (types, sizes, JSON-finite inputs); invalid arguments run nothing.
- Events and tool results carry no host paths or secrets; the source is stored only as its SHA-256 in events.

## Acceptance criteria

1. With the runtime enabled (test double of `run`), a valid call returns the compact result of item 3 and the
   double received the `calculation` profile, default limits and no artifact directory.
2. With the runtime disabled or unavailable the call returns `calc_unavailable`; oversize source, non-object inputs
   or non-finite inputs return `invalid_arguments`; in all three cases nothing runs.
3. `_turn_tools` returns `calc_run` for a question containing "simulate" on surface `none`, keeps at most seven
   tools on every surface, and returns the new default set when no word matches.
4. A chat interaction on surface `none` holds a `jarvis.calc_run` grant; a call without a valid grant is refused.
5. On a Linux host with bwrap and the flag set, an integration test runs a real `main(inputs)` that integrates a
   small ODE with scipy and returns its result (skipped, with reason, when bwrap is absent).
6. Lint, mypy ratchet and the focused Hermes, thread-service and studio-runtime tests pass offline.

## Required tests

- Dispatch tests with a runtime double (criteria 1, 2) and a test that the tool cannot select another profile.
- Router and grant tests (criteria 3, 4).
- Sandbox integration test (criterion 5).

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, CI green on the exact head, a full diff review by a
second qualified session, and the real-environment proof: on a proof backend with the flag set, a chat turn that
needs a simulation calls `jarvis_calc_run` and the sandbox runs it. Then the coordinator reruns the chat gate (hard
and example sets) and compares with the spec 370 run: completed turns should not fall, maximum actual input tokens
should stay at or under about 6.5k, and calculation tasks should answer from `jarvis_calc_run` results. The answers
go to the maintainer's chat session for grading. Enabling the flag on the live app is reported to the maintainer
as a setting, not done silently.
