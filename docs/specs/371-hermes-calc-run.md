# 371 — HERMES-CALC-RUN-1: Let the chat compute with short Python in the offline calculation sandbox

Status: merged
Depends on: 368, 370

## Goal

In the Jarvis chat, on every turn, Hermes has a `python` calculation tool: it sends a short Python 3 script (numpy,
scipy and pint available), the existing offline calculation sandbox runs it, and the model reads back what the script
printed. The answer's numbers then come from a tool result, so the number-provenance check can verify them.

## Why

Directives `L20261124193224-8c59`, `L20261124094804-a929` item 1 and `L20261125074257-c57d` item 1 (copy the bench
loop's recipe into the local Hermes route). The private agent bench loop that scored 10.75/16 on the hard set gave the
model exactly this tool: "Run Python 3 code (numpy, scipy, pint available) and return what it prints. Use it for every
calculation, unit conversion and data fit. Always print() the results." Through the real chat the same model scored
about 5/16; the maintainer's grading (`L20261125073601-7388`) traces three of the losses to arithmetic done in prose
(a length off by a factor of about 200, an unintegrated "Euler estimate", mg/L confused with mmol/L). Off a design
surface the chat has no tool that computes: `bench_run` runs fixed evaluators and is granted only on the CAD surface.

## Code facts checked at `8defbd90` (rechecked at `513abe87`)

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

1. **Tool.** New MCP tool `jarvis_python` with capability `jarvis.calc_run` (action class `EXECUTE`, own grant,
   admitted by identity like `bench_run`). Arguments: `grant_id` and `code` (a Python 3 script, at most 16,384 chars).
   Description, verbatim: "Run Python 3 code (numpy, scipy, pint available) and return what it prints. Use it for
   every calculation, unit conversion and data fit. Always print() the results."
2. **Execution.** Dispatch builds `source = code + "\n\ndef main(inputs):\n    return None\n"` and calls
   `studio_runtime.runtime.run(RunRequest(source=source, inputs={}, limits=<calculation profile maximum>),
   profile="calculation")`: the script runs at module load inside the sandbox and its prints are the result. Never the
   geometry profile, never an artifact directory, never a host path in the request. When the runtime is disabled or
   unavailable the tool returns `calc_unavailable` and runs nothing.
3. **Result to the model.** Plain text, as in the bench loop: the printed `stdout`; on `error` add `[stderr]` and the
   last 1,500 chars of `stderr`; on a limit add `error: <limit> limit reached (wall 15 s)` or the limit hit; when
   nothing was printed, `(nothing printed: use print())`. The whole text is capped at 4,000 chars. No host path.
   Events carry `classification`, `limit`, `duration_seconds` and `source_sha256` only.
4. **Grant on every chat turn.** Each Hermes interaction gets one `jarvis.calc_run` grant whatever the surface, with
   the same lifetime as the surface grants.
5. **Router.** `_turn_tools` always includes `python` first, on every surface and for every question; the other
   selections are unchanged except that the list stays at most seven names (the last-ranked one gives way).
6. **Prompt line.** `COMPACT_METHODOLOGY` says: never compute numbers in your head; use `jarvis_python` for every
   calculation, unit conversion and fit, and print the results. The base prompt plus tool schemas stays under the
   spec 368 first-step size bound. The rest of the bench prompt is spec 372.
7. **Dependency.** `pint` is imported by the tool's users but is not a declared requirement (it is present in the
   host venv by hand). Add `pint==0.25.3` to `backend/requirements.txt`.

Decision recorded against the directive: the directive asks for a 90 s timeout; the existing calculation sandbox caps
wall time at 15 s (12 s CPU) and this spec keeps that boundary instead of widening the sandbox. The chat gate reports
every `wall_seconds` limit hit; if real tasks hit it, a follow-up spec raises the calculation profile limit.

Out of scope (binding non-goals):

- The geometry profile, artifacts, files, network, package installation or any persistent state from the sandbox.
- Changing the sandbox limits, `studio_runtime` code or the enable flag; enabling it is an operator setting.
- Adding the tool to cloud routes' prompts beyond the shared allowlist; promotion of any result into project state.
- `bluecad.solid_code` in `AGENT_RUNNABLE_EVALUATORS`.
- The system prompt, output cap, continuation, repeated-call, trimming and end-of-turn rules of directive
  `L20261125074257-c57d` items 2–8 (spec 372).

## Constraints

- High risk (execution of model-written code): the only execution path is `studio_runtime.runtime.run` with the
  `calculation` profile; a test proves no other runner, subprocess or profile is reachable from the tool.
- Arguments are validated before execution (types, size); invalid arguments return `invalid_arguments` and run
  nothing.
- Events and tool results carry no host paths or secrets; the source is stored only as its SHA-256 in events.

## Acceptance criteria

1. With the runtime enabled (test double of `run`), a valid call returns the item 3 text and the double received the
   `calculation` profile, the profile's maximum limits, empty inputs and no artifact directory; the source ends with
   the appended `main`.
2. With the runtime disabled or unavailable the call returns `calc_unavailable`; a missing, non-string or oversize
   `code` returns `invalid_arguments`; in all cases nothing runs. Output over 4,000 chars is cut to 4,000.
3. `_turn_tools` returns `python` first for any question on every surface and never more than seven tools.
4. A chat interaction on surface `none` holds a `jarvis.calc_run` grant; a call without a valid grant is refused.
5. On a Linux host with bwrap and the flag set, an integration test runs a real script that integrates a small ODE
   with scipy, converts a unit with pint and prints both; the tool returns the printed values (skipped, with reason,
   when bwrap is absent).
6. Lint, mypy ratchet and the focused Hermes, thread-service and studio-runtime tests pass offline.

## Required tests

- Dispatch tests with a runtime double (criteria 1, 2) and a test that the tool cannot select another profile.
- Router and grant tests (criteria 3, 4).
- Sandbox integration test (criterion 5).

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, CI green on the exact head, a full diff review by a
second qualified session, and the real-environment proof: on a proof backend with the flag set, a chat turn that
needs a simulation calls `jarvis_python` and the sandbox runs it. Then the coordinator reruns the chat gate (hard
and example sets) and compares with the spec 370 run (`j-20261125T065633Z-8ad83c`, taken while Inkling was being restarted, so re-measure 370 if the comparison is unclear): completed turns should not fall, maximum actual input tokens
should stay at or under about 6.5k, and calculation tasks should answer from `jarvis_python` results. The answers
go to the maintainer's chat session for grading. Enabling the flag on the live app is reported to the maintainer
as a setting, not done silently.
