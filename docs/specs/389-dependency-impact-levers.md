# 389 — DEPENDENCY-IMPACT-LEVERS-1

Status: ready
Depends on: none

## Goal

Extend the existing workspace dependency and freshness graph (`backend/app/modules/flowsheet/freshness.py`) with two strictly read-only deterministic queries — downstream change-impact preview (`preview_change_impact`) and upstream lever discovery (`find_upstream_levers`) — exposed on the existing flowsheet HTTP router and as read-only views (`change_impact`, `upstream_levers`) in `jarvis_knowledge_read`.

## Why

Implements the dependency-graph parallel track explicitly authorized in Maintainer Directive `L20261127191802-5834` (Order of work, Phase 2 items 2, 13, and 26, built on Contract 248's freshness graph per Invariant 1 — reuse before building). Before executing a parameter, assumption, or material change, both the UI and Jarvis chat can deterministically preview every downstream result, solver rerun, and decision that will go stale (with an estimated rerun time when historical run timings exist), and for any model, run, KPI, or decision can list all upstream levers that feed it.

## Code facts checked at `d0ee6915`

- `backend/app/modules/flowsheet/service.py::build_flowsheet_graph_from_connection` builds the complete workspace `FlowsheetGraphRead` over parameters, assumptions, decisions, requirements, models, simulation runs, runner jobs, artifacts, bluecad candidates/attempts, and evidence.
- `backend/app/modules/flowsheet/freshness.py::prepare_freshness_invalidation` computes shortest-path downstream closure over `dependency` and `executed_by` edges bounded by `MAX_FRESHNESS_PATH_NODES` and `MAX_FRESHNESS_MARKS_PER_INVALIDATION`.
- `backend/app/modules/flowsheet/freshness.py::get_node_freshness_from_connection` resolves freshness state on an open SQLite connection.
- `backend/app/modules/flowsheet/models.py::FlowsheetGraphRead` and `backend/app/modules/flowsheet/models.py::FlowsheetNodeRead` define the canonical graph response models.
- `backend/app/modules/flowsheet/routes.py::router` and `backend/app/modules/flowsheet/routes.py::_flowsheet_error` expose read-only `/workspaces/{workspace_id}/flowsheet/*` endpoints.
- `backend/app/modules/agents/hermes/knowledge_read_tool.py::dispatch_knowledge_read_tool` dispatches strictly read-only workspace-scoped views (`PRAGMA query_only = ON`) with secret scrubbing and payload bounds.
- `backend/app/modules/agents/hermes/broker_mcp.py::_KNOWLEDGE_READ_TOOL` declares the `jarvis_knowledge_read` MCP input schema.

## Scope

In scope:
- **`backend/app/modules/flowsheet/models.py`:**
  - `FlowsheetAffectedNodeRead`: `ref`, `kind`, `id`, `label`, `distance` (int >= 1), `path` (list[str]), `requires_rerun` (bool).
  - `FlowsheetChangeImpactRead`: `workspace_id`, `source_ref`, `source_kind`, `source_label`, `affected_count`, `counts_by_kind` (dict[str, int]), `affected_nodes` (list[FlowsheetAffectedNodeRead]), `required_reruns` (list[str]), `affected_decisions` (list[str]), `estimated_rerun_seconds` (float | None), `cycle_count` (int), `unresolved_diagnostic_count` (int).
  - `FlowsheetUpstreamLeverRead`: `ref`, `kind`, `id`, `label`, `distance` (int >= 1), `path` (list[str], ordered from lever to target), `status` (str | None).
  - `FlowsheetUpstreamLeversRead`: `workspace_id`, `target_ref`, `target_kind`, `target_label`, `lever_count`, `counts_by_kind` (dict[str, int]), `levers` (list[FlowsheetUpstreamLeverRead]).
- **`backend/app/modules/flowsheet/freshness.py`:**
  - `preview_change_impact_from_connection(connection, workspace_id, node_ref) -> FlowsheetChangeImpactRead` and `preview_change_impact(workspace_id, node_ref) -> FlowsheetChangeImpactRead`:
    - Strictly read-only (zero DB writes).
    - Accepts any canonical `node_ref` present in the workspace graph (raises `FlowsheetError("flowsheet_node_not_found", ...)` when absent or `FlowsheetError("flowsheet_ref_invalid", ...)` when malformed).
    - Walks downstream edges (`edge.edge_class == "dependency" or edge.relation == "executed_by"`) via shortest-path search bounded by `MAX_FRESHNESS_PATH_NODES` and `MAX_FRESHNESS_MARKS_PER_INVALIDATION`.
    - Marks downstream nodes of kind `simulation_run`, `model_version`, `runner_job`, or `bluecad_attempt` as `requires_rerun=True` and collects their refs in `required_reruns`.
    - Collects downstream nodes of kind `decision` in `affected_decisions`.
    - Computes `estimated_rerun_seconds` from completed `runner_jobs` (`started_at`, `finished_at` or `completed_at` / `updated_at` if present, strictly defensive against missing/null timestamps) in the workspace as `len(required_reruns) * median_job_seconds`, or `None` when `required_reruns` is empty or no valid historical durations exist.
  - `find_upstream_levers_from_connection(connection, workspace_id, node_ref) -> FlowsheetUpstreamLeversRead` and `find_upstream_levers(workspace_id, node_ref) -> FlowsheetUpstreamLeversRead`:
    - Strictly read-only; walks reverse edges (`edge.edge_class == "dependency" or edge.relation == "executed_by"`) from `node_ref`, bounded by `MAX_FRESHNESS_PATH_NODES` and `MAX_FRESHNESS_MARKS_PER_INVALIDATION`.
    - Returns upstream nodes ordered by `(distance, kind, ref)` with `path` oriented from the upstream lever to `target_ref`.
- **`backend/app/modules/flowsheet/routes.py`:**
  - `GET /workspaces/{workspace_id}/flowsheet/nodes/{node_ref}/impact` -> `FlowsheetChangeImpactRead`
  - `GET /workspaces/{workspace_id}/flowsheet/nodes/{node_ref}/levers` -> `FlowsheetUpstreamLeversRead`
- **`backend/app/modules/agents/hermes/knowledge_read_tool.py` & `backend/app/modules/agents/hermes/broker_mcp.py`:**
  - Add `"change_impact"` and `"upstream_levers"` to `_ALLOWED_VIEWS`, `_VIEW_ALLOWED_KEYS` (`{"view", "workspace_id", "node_ref"}`), and `_KNOWLEDGE_READ_TOOL` schema (`node_ref` string property).
  - Dispatch both on the existing `PRAGMA query_only = ON` read-only connection, returning scrubbed, payload-bounded results; return typed error codes (`"invalid_arguments"`, `"node_not_found"`) cleanly on bad or missing `node_ref`.
- **Frontend contracts regeneration:**
  - Run `python3 scripts/generate_frontend_contracts.py` so `frontend/src/api/generated` stays in sync with the new routes.

Out of scope (binding non-goals):
- No SQLite schema migration or DB writes.
- No frontend UI panel changes in this slice (backend + Hermes tool + generated API contracts only).
- No automatic execution of reruns (preview is read-only).

## Files likely touched

- `backend/app/modules/flowsheet/models.py`
- `backend/app/modules/flowsheet/freshness.py`
- `backend/app/modules/flowsheet/routes.py`
- `backend/app/modules/agents/hermes/knowledge_read_tool.py`
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `frontend/src/api/generated/*` (generated via `python3 scripts/generate_frontend_contracts.py`)
- `backend/tests/test_flowsheet_impact_levers.py` (new)

## Design constraints

- Strictly read-only: neither HTTP endpoints nor `jarvis_knowledge_read` views may mutate SQLite state.
- Reuse `build_flowsheet_graph_from_connection` and existing `MAX_FRESHNESS_PATH_NODES` / `MAX_FRESHNESS_MARKS_PER_INVALIDATION` bounds; do not introduce a second graph builder.
- Workspace isolation: cross-workspace `node_ref` or `workspace_id` queries remain denied fail-closed.
- No secrets or raw host paths in responses (scrubbed via `_scrub_value` and `_enforce_payload_bounds` in `knowledge_read_tool.py`).

## Acceptance criteria

1. `GET /workspaces/{workspace_id}/flowsheet/nodes/{node_ref}/impact` returns exact downstream affected nodes, `counts_by_kind`, `required_reruns`, `affected_decisions`, shortest paths, and `estimated_rerun_seconds` (when historical durations exist, else `None`) without writing to `freshness_invalidations`.
2. `GET /workspaces/{workspace_id}/flowsheet/nodes/{node_ref}/levers` returns exact upstream levers with shortest paths oriented `lever -> ... -> target_ref` and `counts_by_kind`.
3. Both endpoints return 404 on unknown `node_ref` / workspace and 400 on malformed `node_ref`.
4. `jarvis_knowledge_read` supports `view="change_impact"` and `view="upstream_levers"` with `node_ref` on a read-only SQLite connection, enforces workspace isolation, and rejects missing/invalid `node_ref`.
5. `python3 scripts/generate_frontend_contracts.py --check`, `ruff`, and focused + existing flowsheet and knowledge_read tests pass offline.

## Required tests

- `backend/tests/test_flowsheet_impact_levers.py`:
  - Downstream change-impact preview from a `parameter:*` node through `model_version` -> `simulation_run` and `decision`, verifying `affected_count`, `counts_by_kind`, `required_reruns`, `affected_decisions`, shortest paths, and zero rows added to `freshness_invalidations`.
  - Downstream impact preview from a `decision:*` or `assumption:*` node (non-parameter source).
  - Upstream levers query from a `simulation_run:*` and `decision:*` node verifying reverse traversal, distance ordering, and path orientation.
  - 404 for unknown `node_ref`, 400 for malformed `node_ref`.
  - `dispatch_knowledge_read_tool` for `view="change_impact"` and `view="upstream_levers"` (valid, missing `node_ref`, unknown `node_ref`, cross-workspace denial).

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
