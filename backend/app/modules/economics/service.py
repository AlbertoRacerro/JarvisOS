"""Pure deterministic TEA evaluation and workspace-scoped storage (spec 393)."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.paths import build_paths
from app.modules.economics.models import (
    CapexItemBreakdown,
    CostHotspot,
    DepreciationMethod,
    EconomicsEvaluation,
    EconomicsModelInput,
    MissingCostItem,
    OpexItemBreakdown,
    WorkspaceEconomicsResponse,
)
from app.modules.events.service import utc_now


def _annualize_capital(
    capital: float,
    lifetime_years: float,
    method: DepreciationMethod,
    discount_rate: float,
) -> float:
    if capital <= 0.0:
        return 0.0
    if method == "crf" and discount_rate > 0.0:
        factor_pow = (1.0 + discount_rate) ** lifetime_years
        crf = (discount_rate * factor_pow) / (factor_pow - 1.0)
        return capital * crf
    return capital / lifetime_years


def _build_hotspots(entries: list[tuple[str, str, str, float]], total: float) -> list[CostHotspot]:
    if total <= 0.0:
        return []
    positive = [(iid, name, cat, amt) for iid, name, cat, amt in entries if amt > 0.0]
    positive.sort(key=lambda row: (-row[3], row[1].lower(), row[0]))
    return [
        CostHotspot(
            item_id=iid,
            name=name,
            category=cat,
            amount=round(amt, 2),
            share_pct=round((amt / total) * 100.0, 2),
        )
        for iid, name, cat, amt in positive
    ]


def _format_money(value: float, currency: str) -> str:
    if abs(value - round(value)) < 1e-6:
        return f"{round(value):,} {currency}"
    return f"{value:,.2f} {currency}"


def evaluate_economics(
    model: EconomicsModelInput,
    *,
    design_components: list[dict[str, str]] | None = None,
) -> EconomicsEvaluation:
    """Evaluate CAPEX, OPEX, annualized total, completeness, and cost hotspots deterministically."""
    assumptions = model.assumptions
    currency = assumptions.currency.strip() or "EUR"
    default_life = assumptions.default_lifetime_years
    method = assumptions.depreciation_method
    rate = assumptions.discount_rate

    capex_breakdown: list[CapexItemBreakdown] = []
    opex_breakdown: list[OpexItemBreakdown] = []
    missing_items: list[MissingCostItem] = []

    total_purchased = 0.0
    total_installed = 0.0
    annualized_capex_items = 0.0

    capex_entries: list[tuple[str, str, str, float]] = []
    annualized_entries: list[tuple[str, str, str, float]] = []

    costed_component_ids: set[str] = set()
    costed_capex_names: set[str] = set()

    for item in model.capex_items:
        life = item.lifetime_years if item.lifetime_years is not None else default_life
        if item.component_id:
            costed_component_ids.add(item.component_id.strip())
        costed_capex_names.add(item.name.strip().lower())

        if item.unit_cost is None:
            missing_items.append(
                MissingCostItem(
                    kind="missing_capex_unit_cost",
                    item_id=item.id,
                    name=item.name,
                    message=f"Missing unit cost for CAPEX item '{item.name}'.",
                )
            )
            capex_breakdown.append(
                CapexItemBreakdown(
                    id=item.id,
                    name=item.name,
                    category=item.category,
                    quantity=item.quantity,
                    unit_cost=None,
                    installation_factor=item.installation_factor,
                    lifetime_years=life,
                    purchased_cost=None,
                    installed_cost=None,
                    annual_depreciation=None,
                    component_id=item.component_id,
                    missing_cost=True,
                )
            )
            continue

        purchased = item.quantity * item.unit_cost
        installed = purchased * item.installation_factor
        ann_dep = _annualize_capital(installed, life, method, rate)

        total_purchased += purchased
        total_installed += installed
        annualized_capex_items += ann_dep

        capex_entries.append((item.id, item.name, item.category, installed))
        annualized_entries.append((f"capex:{item.id}", f"{item.name} (CAPEX)", item.category, ann_dep))

        capex_breakdown.append(
            CapexItemBreakdown(
                id=item.id,
                name=item.name,
                category=item.category,
                quantity=item.quantity,
                unit_cost=round(item.unit_cost, 4),
                installation_factor=item.installation_factor,
                lifetime_years=life,
                purchased_cost=round(purchased, 2),
                installed_cost=round(installed, 2),
                annual_depreciation=round(ann_dep, 2),
                component_id=item.component_id,
                missing_cost=False,
            )
        )

    contingency_amount = total_installed * (assumptions.contingency_pct / 100.0)
    total_capex_with_contingency = total_installed + contingency_amount
    annualized_contingency = _annualize_capital(contingency_amount, default_life, method, rate)
    annualized_capex = annualized_capex_items + annualized_contingency

    if contingency_amount > 0.0:
        capex_entries.append(("allowance:contingency", "Contingency allowance", "allowance", contingency_amount))
        annualized_entries.append(
            ("allowance:contingency", "Contingency allowance (CAPEX)", "allowance", annualized_contingency)
        )

    opex_by_subcat_raw: dict[str, float] = {}
    opex_entries: list[tuple[str, str, str, float]] = []

    for item in model.opex_items:
        computed_from_rate = False
        ann_cost: float | None = None
        if item.annual_cost is not None:
            ann_cost = item.annual_cost
        elif item.unit_rate is not None and item.annual_usage is not None:
            ann_cost = item.unit_rate * item.annual_usage
            computed_from_rate = True

        if ann_cost is None:
            missing_items.append(
                MissingCostItem(
                    kind="missing_opex_cost",
                    item_id=item.id,
                    name=item.name,
                    message=f"Missing annual cost or rate/usage for OPEX item '{item.name}'.",
                )
            )
            opex_breakdown.append(
                OpexItemBreakdown(
                    id=item.id,
                    name=item.name,
                    subcategory=item.subcategory,
                    annual_cost=None,
                    computed_from_rate=False,
                    missing_cost=True,
                )
            )
            continue

        opex_by_subcat_raw[item.subcategory] = opex_by_subcat_raw.get(item.subcategory, 0.0) + ann_cost
        opex_entries.append((item.id, item.name, item.subcategory, ann_cost))
        annualized_entries.append((f"opex:{item.id}", f"{item.name} (OPEX)", item.subcategory, ann_cost))

        opex_breakdown.append(
            OpexItemBreakdown(
                id=item.id,
                name=item.name,
                subcategory=item.subcategory,
                annual_cost=round(ann_cost, 2),
                computed_from_rate=computed_from_rate,
                missing_cost=False,
            )
        )

    periodic_maintenance_annual = total_installed * (assumptions.periodic_maintenance_pct / 100.0)
    if periodic_maintenance_annual > 0.0:
        opex_by_subcat_raw["maintenance"] = opex_by_subcat_raw.get("maintenance", 0.0) + periodic_maintenance_annual
        opex_entries.append(
            (
                "allowance:periodic_maintenance",
                "Periodic maintenance allowance",
                "maintenance",
                periodic_maintenance_annual,
            )
        )
        annualized_entries.append(
            (
                "allowance:periodic_maintenance",
                "Periodic maintenance allowance (OPEX)",
                "maintenance",
                periodic_maintenance_annual,
            )
        )

    opex_by_subcategory = {k: round(v, 2) for k, v in sorted(opex_by_subcat_raw.items())}
    total_annual_opex = sum(opex_by_subcat_raw.values())
    total_annualized_cost = annualized_capex + total_annual_opex

    # Component completeness check
    if design_components:
        for comp in sorted(design_components, key=lambda c: (c.get("name", "").lower(), c.get("id", ""))):
            comp_id = str(comp.get("id", "")).strip()
            comp_name = str(comp.get("name", "")).strip()
            if not comp_name:
                continue
            matched = (comp_id and comp_id in costed_component_ids) or (
                comp_name.lower() in costed_capex_names
            )
            if not matched:
                missing_items.append(
                    MissingCostItem(
                        kind="uncosted_component",
                        item_id=comp_id or None,
                        name=comp_name,
                        message=f"No CAPEX item linked to Design component '{comp_name}'.",
                    )
                )

    if not model.capex_items and not model.opex_items:
        missing_items.insert(
            0,
            MissingCostItem(
                kind="empty_model",
                item_id=None,
                name="economics_model",
                message="No CAPEX or OPEX items configured yet.",
            ),
        )

    is_complete = len(model.capex_items) > 0 and len(missing_items) == 0

    capex_hotspots = _build_hotspots(capex_entries, total_capex_with_contingency)
    opex_hotspots = _build_hotspots(opex_entries, total_annual_opex)
    subcat_entries = [(f"subcat:{k}", k, k, v) for k, v in opex_by_subcat_raw.items()]
    opex_subcategory_hotspots = _build_hotspots(subcat_entries, total_annual_opex)
    annualized_hotspots = _build_hotspots(annualized_entries, total_annualized_cost)

    summary_parts: list[str] = []
    if capex_hotspots:
        top_c = capex_hotspots[0]
        summary_parts.append(f"Top CAPEX driver: {top_c.name} ({top_c.share_pct:.1f}%)")
    if opex_subcategory_hotspots:
        top_o = opex_subcategory_hotspots[0]
        summary_parts.append(f"Top OPEX subcategory: {top_o.name} ({top_o.share_pct:.1f}%)")
    elif opex_hotspots:
        top_o = opex_hotspots[0]
        summary_parts.append(f"Top OPEX driver: {top_o.name} ({top_o.share_pct:.1f}%)")
    hotspot_summary = "; ".join(summary_parts) if summary_parts else "No cost drivers recorded yet."

    cost_str = _format_money(round(total_annualized_cost, 2), currency)
    if is_complete:
        headline = f"This version costs {cost_str}/yr (complete)."
    else:
        preview = ", ".join(m.name for m in missing_items[:3])
        extra = f" (+{len(missing_items) - 3} more)" if len(missing_items) > 3 else ""
        headline = (
            f"This version costs {cost_str}/yr "
            f"(incomplete: {len(missing_items)} missing item{'s' if len(missing_items) != 1 else ''} — {preview}{extra})."
        )

    return EconomicsEvaluation(
        currency=currency,
        depreciation_method=method,
        discount_rate=rate,
        default_lifetime_years=default_life,
        total_capex_purchased=round(total_purchased, 2),
        total_capex_installed=round(total_installed, 2),
        contingency_amount=round(contingency_amount, 2),
        total_capex_with_contingency=round(total_capex_with_contingency, 2),
        annualized_capex=round(annualized_capex, 2),
        periodic_maintenance_annual=round(periodic_maintenance_annual, 2),
        opex_by_subcategory=opex_by_subcategory,
        total_annual_opex=round(total_annual_opex, 2),
        total_annualized_cost=round(total_annualized_cost, 2),
        capex_breakdown=capex_breakdown,
        opex_breakdown=opex_breakdown,
        capex_hotspots=capex_hotspots,
        opex_hotspots=opex_hotspots,
        opex_subcategory_hotspots=opex_subcategory_hotspots,
        annualized_hotspots=annualized_hotspots,
        hotspot_summary=hotspot_summary,
        is_complete=is_complete,
        missing_items=missing_items,
        headline=headline,
    )


def _model_path(workspace_id: str) -> Path:
    return build_paths().economics_model_file(workspace_id)


def load_workspace_economics(workspace_id: str) -> tuple[EconomicsModelInput, str | None]:
    path = _model_path(workspace_id)
    if not path.is_file():
        return EconomicsModelInput(), None
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    updated_at = raw.get("updated_at")
    model_data = raw.get("model", raw)
    return EconomicsModelInput.model_validate(model_data), (str(updated_at) if updated_at else None)


def save_workspace_economics(workspace_id: str, model: EconomicsModelInput) -> tuple[EconomicsModelInput, str]:
    path = _model_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    payload = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "updated_at": now,
        "model": model.model_dump(mode="json"),
    }
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)
    return model, now


def ensure_workspace_exists(connection: sqlite3.Connection, workspace_id: str) -> None:
    row = connection.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if row is None:
        raise HTTPException(404, detail="Workspace not found.")


def list_workspace_design_components(
    connection: sqlite3.Connection, workspace_id: str
) -> list[dict[str, str]]:
    ver_row = connection.execute(
        "SELECT id FROM design_project_versions WHERE data_workspace_id = ? LIMIT 1",
        (workspace_id,),
    ).fetchone()
    if ver_row is None:
        ver_row = connection.execute(
            "SELECT id FROM design_project_versions WHERE project_id = ? AND active = 1 LIMIT 1",
            (workspace_id,),
        ).fetchone()
    if ver_row is None:
        return []
    rows = connection.execute(
        "SELECT id, name FROM design_components WHERE version_id = ? ORDER BY name ASC, id ASC",
        (ver_row["id"],),
    ).fetchall()
    return [{"id": str(r["id"]), "name": str(r["name"])} for r in rows]


def get_workspace_economics_response(
    connection: sqlite3.Connection, workspace_id: str
) -> WorkspaceEconomicsResponse:
    ensure_workspace_exists(connection, workspace_id)
    model, updated_at = load_workspace_economics(workspace_id)
    components = list_workspace_design_components(connection, workspace_id)
    evaluation = evaluate_economics(model, design_components=components)
    return WorkspaceEconomicsResponse(
        workspace_id=workspace_id,
        model=model,
        evaluation=evaluation,
        updated_at=updated_at,
    )
