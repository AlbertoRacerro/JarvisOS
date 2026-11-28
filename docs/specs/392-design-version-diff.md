# 392 — DESIGN-VERSION-DIFF-1

Status: ready
Depends on: none

## Goal

Add a strictly read-only, deterministic semantic diff between any two Design versions of a project (`backend/app/modules/workspaces/version_diff.py`, exposed via `GET /workspaces/{project_id}/design-versions/diff` in `backend/app/modules/workspaces/design_versions.py` and via `jarvis_knowledge_read` views `design_versions` and `version_diff` in `backend/app/modules/agents/hermes/knowledge_read_tool.py` and `broker_mcp.py`). The diff compares Project Basis parameters, Design components (including geometry candidate and pinned models), process draft unit parameters, simulation result values/KPIs, and requirement validations between `base_version_id` and `target_version_id`, computing numeric/relative deltas and a concise deterministic summary.

## Why

Implements Maintainer Directive `L20261127191802-5834` (items 10 & 27 — Versioning everywhere on Contract 248 and Semantic diff between versions, explicitly listed among the clearly-needed pieces that may start in parallel with Phase 1 exploration after the reuse check). Today, JarvisOS already has Design versions (`design_project_versions`, fork/activate in `workspaces/design_versions.py`) and single-value version comparison (`project_knowledge/value_history.py`, spec 258), but there is no cross-version semantic diff answering "what changed between variant A and variant B" across parameters, components, flowsheet units, simulation outputs, and requirement checks, and Hermes cannot inspect Design versions or compare them via `jarvis_knowledge_read`.

## Code facts checked at `65592ad9`

- `backend/app/core/schema.py` already defines `design_project_versions`, `design_components`, `design_component_units`, `design_component_geometry`, `design_component_models`, `parameters`, and `requirements` (no schema migration required).
- `backend/app/core/project_knowledge_schema.py` already defines `simulation_result_batches`, `simulation_result_values`, and `project_knowledge_validation` (no schema migration required).
- `backend/app/modules/workspaces/design_versions.py` owns `router = APIRouter(prefix="/workspaces/{project_id}/design-versions", tags=["design"])` with `list_design_versions`, `create_design_version`, and `activate_design_version`, where each `design_project_versions` row points to an isolated `data_workspace_id` in `workspaces`.
- `backend/app/modules/process_stack/draft.py` provides `list_drafts(workspace_id)`, `draft_dir(workspace_id, draft_id)`, and `load_revision(directory, revision)` to read process draft documents on disk.
- `backend/app/modules/agents/hermes/knowledge_read_tool.py` defines `_ALLOWED_VIEWS`, `_VIEW_ALLOWED_KEYS`, `_VIEW_LIMIT_BOUNDS`, and `dispatch_knowledge_read_tool` with strict workspace scoping and `_enforce_payload_bounds`.
- `backend/app/modules/agents/hermes/broker_mcp.py` defines the `jarvis_knowledge_read` MCP tool schema.

## Scope

In scope:
- **Deterministic semantic diff engine in `backend/app/modules/workspaces/version_diff.py`:**
  - Pydantic response models (`ParameterDiffItem`, `ComponentDiffItem`, `UnitParamDiffItem`, `ResultDiffItem`, `ValidationDiffItem`, `DesignVersionDiff`).
  - `diff_design_versions(connection, project_id: str, base_version_id: str, target_version_id: str) -> DesignVersionDiff`:
    - Validates `project_id` exists (404) and both `base_version_id` and `target_version_id` belong to `project_id` (404 if either is missing or belongs to another project).
    - Resolves `base.data_workspace_id` and `target.data_workspace_id`.
    - Compares 5 layers deterministically (sorted by canonical key):
      1. **Parameters** (`parameters` table, latest non-superseded row per `LOWER(TRIM(name))`): `added`, `removed`, `changed` with `name`, `symbol`, `base_value`, `target_value`, `base_unit`, `target_unit`, `numeric_delta: float | None` (when both values parse as finite floats and units match or both unspecified), `relative_delta_pct: float | None` (when `base_num != 0`), `base_status`, `target_status`.
      2. **Components** (`design_components` + `design_component_geometry` + `design_component_models`, keyed by `LOWER(TRIM(name))`): `added`, `removed`, `changed` tracking `geometry_candidate_changed`, `base_candidate_id`, `target_candidate_id`, `models_added`, `models_removed`, `models_changed`.
      3. **Process draft units** (`draft.list_drafts(data_workspace_id)` + `draft.load_revision`, keyed by `(draft_name, unit_tag)`): `units_added`, `units_removed`, `unit_params_changed` (`draft_name`, `unit_tag`, `unit_type`, `param_key`, `base_value`, `target_value`, `numeric_delta`). Returns empty lists safely when a workspace has no drafts on disk.
      4. **Simulation results / KPIs** (latest `simulation_result_values` per `LOWER(TRIM(output_name))` joined via `simulation_result_batches` for each `data_workspace_id`): `added`, `removed`, `changed` with `output_name`, `quantity`, `base_value`, `target_value`, `base_unit`, `target_unit`, `numeric_delta`, `relative_delta_pct`, `base_status`, `target_status`.
      5. **Requirement validations** (latest `project_knowledge_validation` per `LOWER(TRIM(requirements.statement))` for each `data_workspace_id`): `added`, `removed`, `changed` with `statement`, `base_outcome`, `target_outcome`, `base_reason_code`, `target_reason_code`.
    - Computes `has_changes: bool`, `counts: dict[str, int]`, and a concise deterministic `summary: str` (e.g. `"2 parameters changed, 1 component changed, 1 KPI changed"` or `"No differences between versions."`).
    - When `base_version_id == target_version_id`, returns `has_changes=False` with empty diff lists and `"No differences between versions."`.
- **Read-only HTTP route in `backend/app/modules/workspaces/design_versions.py`:**
  - `GET /workspaces/{project_id}/design-versions/diff` with required query parameters `base_version_id: str` and `target_version_id: str`, returning `DesignVersionDiff`.
  - Regenerate `frontend/src/api/generated` via `python3 scripts/generate_frontend_contracts.py`.
- **Read-only Hermes `jarvis_knowledge_read` views in `backend/app/modules/agents/hermes/knowledge_read_tool.py` and `broker_mcp.py`:**
  - Add `"design_versions"` and `"version_diff"` to `_ALLOWED_VIEWS`, `_VIEW_ALLOWED_KEYS`, and `broker_mcp.py` `jarvis_knowledge_read` schema.
  - Resolve the owning `project_id` cleanly whether `workspace_id` is the parent project workspace ID or one of its `design_project_versions.data_workspace_id` values.
  - `view="design_versions"` returns the project's versions (`id`, `label`, `active`, `parent_version_id`, `source_mode`, `data_workspace_id`, `created_at`).
  - `view="version_diff"` requires `base_version_id` and `target_version_id` (both must belong to the resolved project; reject missing/invalid args with `"invalid_arguments"` and cross-project IDs with `"not_found"`), runs `diff_design_versions`, and bounds the payload with `_enforce_payload_bounds`.
- **Offline tests in `backend/tests/test_design_version_diff.py`:**
  - End-to-end offline tests covering all 5 diff layers (parameters, components/geometry/models, process draft unit params, simulation results/KPIs, requirement validations), identical-version zero-diff, 404 on unknown/cross-project version IDs, and Hermes `jarvis_knowledge_read` (`design_versions` and `version_diff`) from both project and data-version workspace IDs.

Out of scope (binding non-goals):
- No SQLite schema changes or migrations.
- No version deletion/pruning mutations or CAD 3D geometry mesh diffing (separate slices).
- No frontend UI changes (only regenerated OpenAPI TypeScript contracts).

## Files likely touched

- `backend/app/modules/workspaces/version_diff.py` (new)
- `backend/app/modules/workspaces/design_versions.py`
- `backend/app/modules/agents/hermes/knowledge_read_tool.py`
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/tests/test_design_version_diff.py` (new)
- `frontend/src/api/generated/*` (regenerated via `scripts/generate_frontend_contracts.py`)
- `docs/MODULES.md` (regenerated via `scripts/gen_modules.py`)

## Design constraints

- **Strictly read-only:** `diff_design_versions`, the HTTP route, and both `jarvis_knowledge_read` views perform zero writes to SQLite or disk.
- **Deterministic & bounded:** Output ordering is deterministic (sorted by canonical key) and Hermes responses respect `_enforce_payload_bounds` (16 KB cap) and secret scrubbing.
- **Zero private pack literals:** Strictly generic engineering terms.
- **Backward compatible:** Existing `design-versions` endpoints and `jarvis_knowledge_read` views remain unchanged.

## Acceptance criteria

1. `GET /workspaces/{project_id}/design-versions/diff?base_version_id=...&target_version_id=...` returns a deterministic `DesignVersionDiff` covering parameters, components (geometry + models), process draft unit parameters, simulation result values/KPIs, and requirement validations, with numeric/relative deltas, `has_changes`, `counts`, and `summary`.
2. Identical `base_version_id == target_version_id` returns `has_changes=False` and empty diff lists; unknown or cross-project version IDs return HTTP 404.
3. `jarvis_knowledge_read(view="design_versions")` lists the current project's Design versions (working from either project workspace ID or a version's `data_workspace_id`), and `jarvis_knowledge_read(view="version_diff", base_version_id=..., target_version_id=...)` returns the bounded semantic diff while refusing cross-project IDs.
4. All offline tests in `backend/tests/test_design_version_diff.py` and existing `test_hermes_knowledge_read_343.py` / `test_hermes_broker_mcp.py` pass, and `ruff`, `check_spec_status.py`, `generate_frontend_contracts.py --check`, `gen_modules.py`, `check_doc_paths.py`, `check_architecture_enforcement.py`, and `check_typecheck_ratchet.py` pass cleanly.

## Required tests

- `backend/tests/test_design_version_diff.py`:
  - Fork a Design version, mutate parameters, component geometry/model pin, process draft unit parameter, simulation result values, and requirement validation; assert exact diff items, numeric/relative deltas, `has_changes=True`, `counts`, and `summary`.
  - Assert identical versions yield `has_changes=False` and empty lists.
  - Assert unknown or cross-project version IDs return 404.
  - Assert `jarvis_knowledge_read` `design_versions` and `version_diff` succeed from both project workspace ID and version `data_workspace_id`, and reject cross-project version IDs and missing arguments.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator review completed, spec status updated, summary written.
