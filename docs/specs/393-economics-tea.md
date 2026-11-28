# 393 — ECONOMICS-TEA-1

Status: ready
Depends on: none

## Goal

Add a deterministic, zero-new-dependency techno-economic analysis (TEA) and cost-hotspot engine (`backend/app/modules/economics/`) with workspace-scoped persistence (`model.json` under `JarvisPaths.economics_workspace_dir`), REST endpoints (`GET /workspaces/{workspace_id}/economics`, `PUT /workspaces/{workspace_id}/economics`, `POST /workspaces/{workspace_id}/economics/evaluate`), and a read-only Hermes `jarvis_knowledge_read` view (`economics_summary`). For every Design version / workspace, it computes equipment-level CAPEX, subcategory OPEX, annualized total cost with depreciation (straight-line or capital recovery factor) and standard contingency/periodic-maintenance allowances, explicit missing-item completeness tracking with an always-visible `"This version costs X/yr"` headline, and ranked CAPEX/OPEX/annualized cost hotspots.

## Why

Implements Maintainer Directive `L20261127191802-5834` (items 9 & 18 — Economics & Cost hotspots, explicitly listed among the clearly-needed parallel pieces after the reuse check).

### Reuse check (invariants 1 & 3)

1. **Inside JarvisOS:** Searched `backend/app/` and `docs/specs/STATUS.md` for `capex`, `opex`, `techno-economic`, `biosteam`, `depreciation`, and `annualized`: zero existing economics or TEA modules exist. Existing `workspaces`, `design_project_versions`, `design_components`, `JarvisPaths`, and `jarvis_knowledge_read` provide version isolation, component inventory, data-root storage, and governed Hermes read access.
2. **Outside JarvisOS:** Checked **BioSTEAM** (`biosteam`, University of Illinois/NCSA permissive open-source license, PyPI). BioSTEAM provides full biorefinery process simulation and TEA (`FCI`, `Lang`/`bare-module` factors, `MACRS`/`IRR`/`NPV`/`MSP`), but requires BioSTEAM-native `Unit`/`Stream`/`System` objects and pulls a heavy dependency tree (`thermosteam`, `chemicals`, `fluids`, `numba`, `scipy`, `pint`, `pandas`, `openpyxl`). Per `AGENTS.md` Never #10 ("Never add infrastructure, a framework or a dependency the accepted outcome does not need"), we do not add BioSTEAM to core `backend/requirements.txt`. Instead, we implement BioSTEAM / Peters & Timmerhaus / Turton-aligned deterministic TEA formulas directly in pure Python + Pydantic, leaving an optional external BioSTEAM adapter possible later if full stream-level biorefinery simulation is ever needed.

## Code facts checked at `ca488a43`

- `backend/app/core/paths.py::JarvisPaths` defines `bio_models_workspace_dir` using `self.workspaces_dir / workspace_id / ...` (no SQLite migration required for workspace-scoped JSON storage).
- `backend/app/core/schema.py::CURRENT_SCHEMA_MIGRATION_ID` tracks the additive schema that already defines `workspaces`, `design_project_versions`, and `design_components` (allowing completeness checks against the workspace's Design components without schema changes).
- `backend/app/main.py::create_app` registers module FastAPI routers via `app.include_router(...)`.
- `backend/app/modules/agents/hermes/knowledge_read_tool.py::dispatch_knowledge_read_tool` dispatches `_ALLOWED_VIEWS` with `_enforce_payload_bounds` and cloud IP boundary sanitization (`sanitize_record`).
- `backend/app/modules/agents/hermes/broker_mcp.py::main` serves the `jarvis_knowledge_read` MCP tool schema.

## Scope

In scope:
- **Workspace path helpers in `backend/app/core/paths.py`:**
  - `economics_workspace_dir(self, workspace_id: str) -> Path` (`self.workspaces_dir / workspace_id / "economics"`).
  - `economics_model_file(self, workspace_id: str) -> Path` (`self.economics_workspace_dir(workspace_id) / "model.json"`).
- **Deterministic TEA engine & storage in `backend/app/modules/economics/`:**
  - `module.toml` (`status = "live"`, entrypoints `["/workspaces/{id}/economics", "service.py"]`).
  - `models.py`: Pydantic v2 models:
    - `EconomicsAssumptions`: `currency` (default `"EUR"`), `default_lifetime_years` (`> 0`, default `10.0`), `depreciation_method` (`"straight_line" | "crf"`, default `"straight_line"`), `discount_rate` (`>= 0.0`, default `0.0`), `contingency_pct` (`>= 0.0`, default `15.0`), `periodic_maintenance_pct` (`>= 0.0`, default `3.0`), `operating_hours_per_year` (`> 0`, default `8000.0`).
    - `CapexItem`: `id`, `name`, `category` (`"equipment" | "instrumentation" | "piping" | "civil" | "installation" | "other"`), `quantity` (`> 0`, default `1.0`), `unit_cost: float | None = None`, `installation_factor` (`>= 0`, default `1.0`), `lifetime_years: float | None = None` (`> 0` if set), `component_id: str | None = None`, `notes: str = ""`.
    - `OpexItem`: `id`, `name`, `subcategory` (`"utilities_energy" | "raw_materials" | "consumables" | "labor" | "maintenance" | "waste_treatment" | "insurance_overhead" | "other"`), `annual_cost: float | None = None`, `unit_rate: float | None = None`, `annual_usage: float | None = None`, `unit_label: str = ""`, `notes: str = ""`.
    - `EconomicsModelInput`: `assumptions: EconomicsAssumptions`, `capex_items: list[CapexItem]`, `opex_items: list[OpexItem]`.
    - `CapexItemBreakdown`, `OpexItemBreakdown`, `CostHotspot`, `MissingCostItem`, `EconomicsEvaluation`, `WorkspaceEconomicsResponse` (`workspace_id`, `model`, `evaluation`, `updated_at`).
  - `service.py`:
    - `evaluate_economics(model: EconomicsModelInput, *, design_components: list[dict[str, str]] | None = None) -> EconomicsEvaluation`:
      - Pure deterministic calculation (no I/O, no side effects).
      - Per-item CAPEX: `purchased_cost = quantity * unit_cost`, `installed_cost = purchased_cost * installation_factor`, `annual_depreciation` via straight-line (`installed_cost / life`) or CRF (`installed_cost * (r * (1+r)^n) / ((1+r)^n - 1)` when `r > 0`, else `installed_cost / n`). If `unit_cost is None`, records a `MissingCostItem(kind="missing_capex_unit_cost", ...)`.
      - Contingency: `contingency_amount = total_capex_installed * (contingency_pct / 100.0)`, depreciated over `default_lifetime_years` using the same method.
      - Per-item OPEX: uses `annual_cost` if provided, else `unit_rate * annual_usage` if both are provided, else records `MissingCostItem(kind="missing_opex_cost", ...)`.
      - Periodic maintenance allowance: `periodic_maintenance_annual = total_capex_installed * (periodic_maintenance_pct / 100.0)` added to `opex_by_subcategory["maintenance"]` and `total_annual_opex`.
      - Component coverage check: when `design_components` are supplied, any component not matched by `CapexItem.component_id` or case-insensitive `CapexItem.name` is recorded as `MissingCostItem(kind="uncosted_component", ...)`.
      - `is_complete`: `True` iff `len(capex_items) > 0` and `len(missing_items) == 0`.
      - `headline`: deterministic human-readable summary, e.g. `"This version costs 42,500 EUR/yr (complete)"` or `"This version costs 42,500 EUR/yr (incomplete: 2 missing items)"` (with up to 3 missing item labels listed).
      - **Cost hotspots (item 18):** deterministic descending rankings (`capex_hotspots`, `opex_hotspots`, `opex_subcategory_hotspots`, `annualized_hotspots`) with `share_pct`, plus `hotspot_summary`.
    - Atomic JSON persistence (`load_workspace_economics`, `save_workspace_economics`, `get_workspace_economics_response`).
  - `routes.py`:
    - `GET /workspaces/{workspace_id}/economics` -> `WorkspaceEconomicsResponse` (404 if workspace does not exist).
    - `PUT /workspaces/{workspace_id}/economics` -> validates, saves atomically, logs `EconomicsModelUpdated` event, returns `WorkspaceEconomicsResponse`.
    - `POST /workspaces/{workspace_id}/economics/evaluate` -> stateless dry-run evaluation returning `EconomicsEvaluation`.
  - Register `economics_router` in `backend/app/main.py`.
- **Read-only Hermes `jarvis_knowledge_read` view (`economics_summary`):**
  - Add `"economics_summary"` to `_ALLOWED_VIEWS`, `_VIEW_ALLOWED_KEYS`, `dispatch_knowledge_read_tool` in `backend/app/modules/agents/hermes/knowledge_read_tool.py`, and `broker_mcp.py` schema.
  - Enforce cloud IP boundary sanitization (`sanitize_record`) on item lists and headline/summary fields and `_enforce_payload_bounds` (16 KB cap).
- **Generated contracts & module registry:**
  - Regenerate `frontend/src/api/generated/*` via `scripts/generate_frontend_contracts.py` and `docs/MODULES.md` via `scripts/gen_modules.py`.
- **Offline tests in `backend/tests/test_economics_tea.py`:**
  - Hand-verified arithmetic for straight-line vs CRF depreciation, Lang/installation factors, contingency, periodic maintenance, OPEX subcategories (`annual_cost` and `unit_rate * annual_usage`), and annualized total.
  - Completeness and `headline` checks (empty model, missing unit cost, missing OPEX cost, uncosted `design_components`).
  - Cost hotspot rankings (CAPEX, OPEX, subcategory, annualized) and `hotspot_summary`.
  - HTTP `GET`, `PUT`, `POST /evaluate`, and 404 on unknown workspace.
  - Hermes `jarvis_knowledge_read(view="economics_summary")` (local + cloud IP boundary).

Out of scope (binding non-goals):
- No SQLite schema changes or migrations.
- No new Python dependencies (BioSTEAM stays optional/external).
- No frontend UI screens in this slice (only regenerated OpenAPI TypeScript contracts).

## Files likely touched

- `backend/app/core/paths.py`
- `backend/app/main.py`
- `backend/app/modules/economics/__init__.py` (new)
- `backend/app/modules/economics/module.toml` (new)
- `backend/app/modules/economics/models.py` (new)
- `backend/app/modules/economics/service.py` (new)
- `backend/app/modules/economics/routes.py` (new)
- `backend/app/modules/agents/hermes/knowledge_read_tool.py`
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/tests/test_economics_tea.py` (new)
- `backend/tests/test_hermes_knowledge_read_343.py`
- `frontend/src/api/generated/*` (regenerated)
- `docs/MODULES.md` (regenerated)

## Design constraints

- **Strictly deterministic:** Pure Python arithmetic; identical inputs yield bit-for-bit identical totals, hotspot ordering, and headline strings.
- **Strictly read-only reads:** `GET /economics`, `POST /economics/evaluate`, and `jarvis_knowledge_read(view="economics_summary")` never mutate SQLite or disk.
- **Zero private pack literals:** Strictly generic chemical-engineering equipment and cost terminology.
- **Backward compatible:** Existing workspaces without an economics file return a clean empty model with `is_complete=False` and zero errors.

## Acceptance criteria

1. `evaluate_economics` accurately computes installed CAPEX, contingency, straight-line and CRF annualized CAPEX, subcategory OPEX (including periodic maintenance allowance), total annualized cost, completeness (`is_complete`, `missing_items`, `headline`), and descending cost hotspots (`capex_hotspots`, `opex_hotspots`, `opex_subcategory_hotspots`, `annualized_hotspots`, `hotspot_summary`).
2. `GET /workspaces/{workspace_id}/economics`, `PUT /workspaces/{workspace_id}/economics`, and `POST /workspaces/{workspace_id}/economics/evaluate` persist and evaluate models per workspace (404 on unknown workspace) and detect uncosted `design_components`.
3. `jarvis_knowledge_read(view="economics_summary")` returns the bounded, cloud-IP-sanitized economics evaluation and hotspots for the active workspace.
4. All offline tests in `backend/tests/test_economics_tea.py`, `test_hermes_knowledge_read_343.py`, and `test_hermes_broker_mcp.py` pass, and `ruff`, `check_spec_status.py`, `generate_frontend_contracts.py --check`, `gen_modules.py`, `check_doc_paths.py`, and `check_architecture_enforcement.py` pass cleanly.

## Required tests

- `backend/tests/test_economics_tea.py`:
  - Unit tests verifying straight-line and CRF depreciation arithmetic against exact hand calculations, installation factors, contingency, periodic maintenance, and OPEX rate*usage.
  - Completeness tests verifying missing CAPEX unit cost, missing OPEX cost, and uncosted `design_components` produce `is_complete=False` and accurate `headline` / `missing_items`.
  - Hotspot tests verifying descending `share_pct` ordering and dominant-driver `hotspot_summary`.
  - API tests for `GET`, `PUT`, `POST /evaluate`, persistence, and 404.
  - Hermes `jarvis_knowledge_read(view="economics_summary")` tests (local and cloud IP boundary).

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator review completed, spec status updated, summary written.
