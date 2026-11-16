# 283 — Bench tools for the Jarvis agent

Status: in_review
Depends on: 282

## Goal

The Jarvis agent (Hermes, through the existing Jarvis MCP broker) can list the engineering evaluators, run one of
them, read a stored run and compare two stored runs, with the same request shapes, checks, verdicts and bounded
view as the headless bench command of 282. Runs live in the workspace's bench folder under the data root and are
named by run id; the agent never sees or supplies a filesystem path.

## Why

Directive L20261115204046-4be1 (H1) asks for one door to the engines, as a command and as MCP tools; the local
model in the "no cloud" mode reaches the bench only through these tools. Reusing the 282 core keeps one
implementation of request handling, verdicts and the bounded view, so the agent and a shell user see the same
result for the same request.

## Code facts checked at `3a6f41cb` plus PR #132

- `backend/app/modules/agents/hermes/broker_mcp.py::_reply` lists `_TOOLS` and relays a `tools/call` to the
  local broker endpoint with `tool_name` added; it is the only MCP server the agent has.
- `backend/app/modules/agents/hermes/supervisor.py::dispatch_tool` checks the live grant (capability, workspace,
  expiry) before calling a handler; `_TOOL_CAPABILITIES` maps tool names to capability ids, and the capability
  descriptors are registered on the `ai-threads` route at import.
- `backend/app/modules/agents/hermes/worker_shim.py::ADMITTED_TOOLS` and `HERMES_TOOL_ALLOWLIST` in
  `backend/app/modules/agents/hermes/supervisor.py` are the two allowlists a new tool must enter.
- `backend/app/modules/ai/thread_service.py::_install_surface_grants` issues ten-minute grants per surface
  (`process`, `bluecad`, `studio`) and tells the agent their ids.
- `backend/app/modules/ai/jarvis_context_models.py::JarvisCapabilityDescriptor` takes an `action_class`;
  `EXECUTE` exists and no Hermes capability uses it yet.
- `backend/app/modules/engineering/operator_service.py::workspace_dir` validates the workspace id and returns
  `<workspaces>/<id>/engineering/studies` under the data root.
- `backend/app/modules/engineering/bench.py::handle_run` and `handle_compare` (282) hold the run and compare logic
  inside argparse handlers.

## Scope

In scope:

1. Move the logic of `run`, `compare` and the list rows of 282 into plain functions in `bench.py` that take
   Python values (evaluator id, request object, output folder, earlier run folders, root, tool registry, timeout)
   and return the result document and the bounded view. The CLI keeps its arguments, output and exit codes and
   calls them.
2. Two capabilities and four tools:
   - `jarvis.bench_read` (`READ`): `jarvis_bench_read` with `op` = `list` (the 282 list rows), `show` (the bounded
     view of a stored run) or `compare` (two stored runs).
   - `jarvis.bench_run` (`EXECUTE`): `jarvis_bench_run` with `evaluator_id`, `request` (object), optional
     `from_run` (run id of the mesh run for FEM) and `geometry_run` (run id of the solid run for mesh); it returns
     the run id, status, verdict and the bounded view.
3. Runs are written to `<workspaces>/<id>/engineering/bench/<run_id>/`, the sibling of the folder `workspace_dir`
   returns, with a server-made run id. A `from_run` or
   `geometry_run` must be a run of the same workspace with the matching evaluator; the tool fills
   `geometry.step_path` and `geometry.manifest_path` from the solid run's own files, and refuses a request that
   names any path itself.
4. Grants for both capabilities are issued with the `bluecad` surface grants and named in the same surface
   instruction lines. Other surfaces are unchanged.
5. The thread status text gets labels for the two tools, as the other tools have.

Out of scope (binding non-goals):

- A second MCP server, an MCP library, HTTP routes for the bench, frontend controls.
- Promoting a run's outputs into design values or the value store; a run is evidence, not accepted state.
- Long runs: a run whose timeout would exceed the tool deadline is refused, not queued. A job-backed run is a
  later slice.
- Grants on the `process` or `studio` surfaces; external harness sessions (H6).

## Files likely touched

- `backend/app/modules/engineering/bench.py`
- `backend/app/modules/agents/hermes/broker_mcp.py`, `supervisor.py`, `worker_shim.py`
- `backend/app/modules/ai/thread_service.py`
- tests next to the existing Hermes broker, dispatch and surface-grant tests; `backend/tests/test_engineering_bench.py`

## Design constraints

- **No path from the model.** Tool arguments carry run ids and request objects only. Any key named `*_path` in a
  request, or a run id that does not resolve to a folder of the same workspace's bench folder, is refused with a
  one-line reason before any engine runs.
- **Bounded.** The run timeout is at most 100 s (the tool call deadline is 120 s); `--tool-registry` is not
  exposed; at most one bench run per worker at a time (a second call while one runs is refused `busy`); the
  returned view is the 282 view, at most 8000 characters.
- **Agent-runnable evaluators only.** The agent may run the three BLUECAD engines and the three screening
  evaluators (pipe pressure drop, internal convection, pure-fluid properties), which write only inside the run
  folder or nothing and finish within the tool timeout. DWSIM, OpenFOAM and the day/night simulator write outside
  the run folder or run on their own deadlines and are refused. For registry evaluators the server sets
  `request_ref.workspace_id` to the grant's workspace and the request window to the tool timeout; a request whose
  other refs name another workspace is refused. Absolute paths in any returned view are reduced to their last
  component.
- **Same verdict.** The tool result's `status`, `verdict` and checks are the ones `result.json` holds; the tool
  never computes a second verdict. A non-succeeded evaluation is a successful tool call carrying that status.
- **Grant discipline.** Both capabilities go through `dispatch_tool`; a call with a missing, expired,
  other-workspace or other-capability grant is refused like the existing tools. The existing tool-event logging
  records the call; arguments logged follow `_safe_tool_arguments` (no request bodies beyond its bounds).
- **No authority.** The run writes only inside its run folder; it changes no draft, candidate, value or
  setting.

## Acceptance criteria

1. The CLI tests of 282 still pass unchanged against the refactored functions.
2. `jarvis_bench_read list` through `dispatch_tool` with a live `jarvis.bench_read` grant returns the nine rows;
   without a grant, with an expired grant, or with a `jarvis.bluecad_read` grant it is refused.
3. `jarvis_bench_run` with fake native engines runs solid, then mesh with `geometry_run`, then FEM with
   `from_run`, writing three run folders under the workspace bench folder whose `result.json` verdicts equal those
   in the tool results.
4. A request naming a path, a run id from another workspace or of the wrong evaluator, and a timeout above 100 s
   are refused before any engine runs; a second run while one is active is refused `busy`.
5. `show` and `compare` return the 282 views of stored runs.
6. The four allowlists (`_TOOLS`, `_TOOL_CAPABILITIES`, `ADMITTED_TOOLS`, `HERMES_TOOL_ALLOWLIST`) list the new
   tools, and the `bluecad` surface brief names both grant ids; the `process` and `studio` briefs do not.
7. On the coordinator's machine, one solid → mesh → FEM chain through `dispatch_tool` with the installed tools
   and a temporary data root returns `usable` for all three; a missing tool is reported as a gap.

## Required tests

- Offline dispatch tests for criteria 2–6 with fake native engines and a temporary data root.
- Existing Hermes broker, dispatch, surface-grant and bench tests; lint; typecheck ratchet.
- The local engine proof of criterion 7.
- Exact-head CI and a full diff review by a second qualified session (high risk: agent tool allowlist and a new
  `EXECUTE` capability).

## Definition of done

Acceptance criteria met, the STATUS row truthful, exact-head CI green, the engine proof and the second-session
review recorded.
