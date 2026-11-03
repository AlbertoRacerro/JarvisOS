"""Governed Process and BLUECAD actions with durable outcomes."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from app.core.database import open_sqlite_connection
from app.modules.ai.sensitivity import deterministic_floor_trigger
from app.modules.bio_models import service as bio_models
from app.modules.bluecad.ledger import get_candidate
from app.modules.bluecad.spec import SUPPORTED_PART_KINDS, canonicalize_geometry_spec
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import (
    QUANTITY_UNITS,
    STREAM_SPECS,
    AddStream,
    AddUnit,
    Delete,
    Disconnect,
    DraftOp,
    DraftQuantity,
    KineticReaction,
    Move,
    Rename,
    SetOrientation,
    SetReactorReaction,
    SetStreamCulture,
    SetStreamSpec,
    SetUnitParams,
    UnitHydraulicsPin,
    UnitModelPin,
    spec_for,
)
from app.modules.process_stack.draft_models import Connect as DraftConnect
from app.modules.process_stack.draft_models import (
    SetUnitHydraulics as DraftSetUnitHydraulics,
)
from app.modules.process_stack.draft_models import (
    SetUnitModel as DraftSetUnitModel,
)
from app.modules.workspace_actions.models import (
    ActionOrigin,
    ActionOutcome,
    ActionRequest,
    BluecadAction,
    ChangeLine,
    ProcessAction,
    Quantity,
    SetCfdStudyTemplate,
    StudioDraftAction,
    SurfaceBrief,
    SurfaceRef,
)


class ActionError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _find_draft(workspace_id: str, revision: str | None = None, draft_id: str | None = None) -> tuple[str, dict] | None:
    matches = []
    for item in draft.list_drafts(workspace_id):
        if draft_id and item["draft_id"] != draft_id:
            continue
        if revision is None or item["revision"] == revision:
            record = draft.load_revision(draft.draft_dir(workspace_id, item["draft_id"]), item["revision"])
            matches.append((item["draft_id"], record))
    return matches[0] if len(matches) == 1 else None


def _brief_none(workspace_id: str, ref: SurfaceRef | None, summary: str) -> SurfaceBrief:
    route = ref.route_id if ref else "unknown"
    payload: dict[str, object] = {
        "surface": "none", "route_id": route, "workspace_id": workspace_id,
        "base_revision": None, "draft_id": None, "candidate_id": None, "selected": [],
        "summary": summary, "text": summary, "actions": [], "limits": [],
    }
    return SurfaceBrief(
        surface="none",
        route_id=route,
        workspace_id=workspace_id,
        summary=summary,
        text=summary,
        digest=_digest(payload),
    )


def _secret_like(*texts: str | None) -> bool:
    trigger = deterministic_floor_trigger("\n".join(text for text in texts if text))
    return bool(trigger and trigger[0] == "S4")


def _studio_brief(workspace_id: str, ref: SurfaceRef) -> SurfaceBrief:
    from app.modules.studio import service as studio

    model = None
    if ref.studio_model_id:
        try:
            model = studio.get(workspace_id, ref.studio_model_id)
        except studio.StudioError:
            return _brief_none(workspace_id, ref, "Selected Studio draft is unavailable in this workspace.")
        if model["status"] != "draft" or model["category"] not in {"calculation", "geometry", "process_unit"}:
            return _brief_none(workspace_id, ref, "Select an open calculation, geometry or process-unit draft.")
        if _secret_like(model["name"], model["engineering_question"]):
            # The name also travels in brief.selected and the Hermes read tool response.
            return _brief_none(workspace_id, ref, "Selected Studio draft contains secret-like content.")
    revision = studio.proposal_revision(model) if model else "new"
    summary = (f"Studio {model['category']} draft: {_safe_surface_label(model['name'], 'model')}"
               if model else "Studio: new calculation, geometry or steady process unit")
    text = ("Studio proposals create or update a draft only. The operator must review and click Apply, "
            "then separately Test and Publish. Python source is inert until Test. "
            "Use exactly one studio_draft action with mode create or update, category calculation, geometry or "
            "process_unit, name, engineering_question, contract, source defining main(inputs), and assumptions. "
            "Calculation contract: inputs and outputs. Geometry: inputs, build123d lengths in mm. "
            "Steady process unit: inlets, outlets, parameters, symbol, culture_generation; no energy balance.\n")
    selected: list[dict[str, object]] = []
    if model:
        contract = {key: model[key] for key in ("inputs", "outputs", "inlets", "outlets", "parameters",
                                               "symbol", "culture_generation") if key in model}
        source = model["source"]
        contract_text = _canonical(contract)
        if _secret_like(contract_text, source):
            contract_text, snippet = "[omitted: secret-like content]", "[omitted: secret-like content]"
        else:
            snippet = source[:2500]
            if len(source) > 2500:
                snippet += "\n[truncated; ask operator to edit large source in Studio]"
        # Only the proposal revision is shown: a model shown raw content hashes echoes them as base_revision.
        text += (f"Selected draft id={model['id']} category={model['category']} base_revision={revision} "
                 f"(copy exactly into jarvis_studio_act).\nContract: {contract_text[:1800]}\n"
                 f"Source: {snippet}\n")
        selected = [{"kind": "studio_model", "id": model["id"], "category": model["category"],
                     "name": model["name"]}]
    text = text[:6000]
    payload: dict[str, Any] = {"surface": "studio", "route_id": ref.route_id, "workspace_id": workspace_id,
               "base_revision": revision, "draft_id": model["id"] if model else None,
               "candidate_id": None, "selected": selected, "summary": summary, "text": text,
               "actions": ["studio_draft"], "limits": ["draft only", "explicit Apply", "no Test or Publish"]}
    return SurfaceBrief.model_validate({**payload, "digest": _digest(payload)})


def surface_brief(workspace_id: str, ref: SurfaceRef | None) -> SurfaceBrief:
    if ref is None or ref.route_id not in {"design-process", "design-bluecad", "design-studio"}:
        return _brief_none(workspace_id, ref, "No engineering workspace surface is active.")
    if ref.route_id == "design-studio":
        return _studio_brief(workspace_id, ref)
    if ref.route_id == "design-process":
        from app.modules.bio_models.forms import kinetics_explanation
        found = _find_draft(workspace_id, draft_id=ref.draft_id) if ref.draft_id else None
        if ref.draft_id and not found:
            return _brief_none(
                workspace_id,
                ref,
                "The referenced Process draft is unknown or belongs to another workspace; no surface object was selected.",
            )
        if not ref.draft_id:
            latest = draft.list_drafts(workspace_id)
            found = _find_draft(workspace_id, draft_id=latest[0]["draft_id"]) if latest else None
        if not found:
            return _brief_none(workspace_id, ref, "No Process draft is available in this workspace.")
        draft_id, record = found
        document = record["document"]
        objects = list(document["objects"].values())
        by_tag = {item["tag"]: item for item in objects}
        selected = []
        ignored = False
        for selection in ref.process_selection:
            item = document["objects"].get(selection.id or "") if selection.id else None
            item = item or by_tag.get(selection.tag or "")
            if item is None or item["kind"] != selection.kind:
                ignored = True
                continue
            selected.append(_process_object_brief(item))
        projection = draft.projection(workspace_id, draft_id)
        summary = f"Process: {_safe_surface_label(document.get('name'), 'Process draft')} · revision {str(record['revision']).split(':', 1)[0]}"
        if selected:
            selected_object = selected[0]
            summary += f" · selected: {selected_object['kind'].capitalize()} {selected_object['tag']}"
        if ignored:
            summary += "; unknown or foreign selection ignored"
        selected_names = ", ".join(f"{item['type']} `{item['tag']}`" for item in selected)
        selected_stream = next((item for item in selected if item["kind"] == "stream"), None)
        selected_unit = next((item for item in selected if item["kind"] == "unit"), None)
        first_stream = next((item for item in objects if item["kind"] == "stream"), None)
        first_unit = next((item for item in objects if item["kind"] == "unit"), None)
        stream_tag = (selected_stream or first_stream or {"tag": "<stream tag from read>"})["tag"]
        unit_tag = (selected_unit or first_unit or {"tag": "<unit tag from read>"})["tag"]
        unit_tags = [item["tag"] for item in objects if item["kind"] == "unit"]
        next_unit_tag = next((f"P{index}" for index in range(1, len(unit_tags) + 2)
                              if f"P{index}" not in unit_tags), "<new tag>")
        selected_info = []
        if selected_unit:
            canonical_unit = document["objects"][selected_unit["id"]]
            declaration = spec_for(canonical_unit)
            selected_info.append(f"Selected unit owner: {declaration.owner}; culture rule: {declaration.culture_rule}.")
            if canonical_unit["type"] == "SpecifiedSeparator":
                recovery = float(canonical_unit.get("params", {}).get("biomass_recovery", {}).get("si", 0))
                factor = float(canonical_unit.get("params", {}).get("concentration_factor", {}).get("si", 0))
                selected_info.append(f"SpecifiedSeparator biomass recovery {recovery:g}%; concentration factor {factor:g}; "
                                     f"derived concentrate carrier split {recovery / factor if factor else 0:g}%.")
        if selected_stream and selected_stream.get("culture") is not None:
            fields = "; ".join(
                f"{name} {value['value']} {value['unit']}"
                for name, value in sorted(selected_stream["culture"].items())
            )
            selected_info.append(f"Selected feed culture values: {fields}.")
        selected_context = ("\n".join(selected_info) + "\n") if selected_info else ""
        pbr_units = [item for item in objects if item["kind"] == "unit"
                     and spec_for(item).culture_rule == "pbr"]
        run_summary = "No mixed solve recorded."
        last_attempt = projection["results"].get("last_attempt")
        if last_attempt and last_attempt.get("run_id"):
            try:
                last_run = draft.get_run(workspace_id, draft_id, last_attempt["run_id"])
            except draft.DraftError:
                last_run = None
            mixed_solve = (last_run or {}).get("mixed_solve")
            if mixed_solve:
                history = mixed_solve.get("history") or []
                last_row = history[-1] if history else {}
                residual_text = (
                    "no finite value (null/value pattern mismatch)" if last_row.get("non_finite")
                    else last_row.get("max_normalized_residual", "none"))
                run_summary = (f"Last mixed run: {mixed_solve.get('status')}; reason {mixed_solve.get('reason', 'unknown')}; "
                               f"{len(history)} iterations; worst field {last_row.get('worst_field', 'none')}; "
                               f"max normalized residual {residual_text}.")
        else:
            last_run = None
        pbr_rows = _pbr_brief_rows(workspace_id, pbr_units, last_run)
        reaction_rows = _reaction_brief_rows(document, last_run)
        # Variable parts first; the fixed vocabulary tail is never cut, so the brief shrinks its lists to fit.
        tail = (
            "Action JSON examples (submit one or more objects in actions): "
            f'{{"op":"set_value","target":"{stream_tag}","property":"pressure","value":{{"value":2,"unit":"bar"}}}}; '
            f'{{"op":"set_unit_model","unit":"{unit_tag}","card":"model card name or id"}}; '
            f'{{"op":"set_unit_hydraulics","unit":"{unit_tag}","reduced_model":"studio version id or null for the literature correlation"}}; '
            f'{{"op":"set_cfd_study_template","unit":"{unit_tag}","regime":"turbulent_rans","inlet":{{"mean_velocity_m_s":0.3}},'
            '"turbulence_model":"kOmegaSST","wall_treatment":"low_re_resolved","mesh_level":1}; '
            '{"op":"set_reaction","unit":"PFR_1","reaction_id":"r1","reaction":'
            '{"name":"Example","stoichiometry":{"Ethylene oxide":-1,"Water":-1,"Ethylene glycol":1},'
            '"base_reactant":"Ethylene oxide","phase":"Liquid","basis":"MolarConc",'
            '"rate_law":{"form":"monod","substrate":"Ethylene oxide",'
            '"v_max":{"value":5,"unit":"kmol/[m3.h]"},"k_s":{"value":2,"unit":"kmol/m3"}},'
            '"provenance":{"kind":"synthetic"}}}; '
            f'{{"op":"add_unit","type":"Pump","tag":"{next_unit_tag}","near":"{unit_tag}"}}; '
            f'{{"op":"insert_unit_after","type":"Pump","after":"{unit_tag}","tag":"{next_unit_tag}"}}; '
            f'{{"op":"connect","from":"{unit_tag}","from_port":"outlet","to":"{next_unit_tag}","to_port":"inlet"}}; '
            f'{{"op":"disconnect","stream":"{stream_tag}"}}; '
            f'{{"op":"mirror","target":"{unit_tag}","axis":"horizontal"}}; '
            f'{{"op":"move","target":"{unit_tag}","dx":20,"dy":0}}; '
            f'{{"op":"rename","target":"{unit_tag}","new_tag":"{next_unit_tag}"}}; '
            f'{{"op":"delete","target":"{unit_tag}"}}. '
            f'Example: {{"op":"set_value","target":"{unit_tag}","property":"concentration_factor",'
            '"value":{"value":20,"unit":"dimensionless"}}. '
            "A Photobioreactor (T1) needs a pinned model card (set_unit_model, confirm tier); the agent never edits or "
            "verifies parameter sets. set_unit_hydraulics (confirm tier) pins a current CFD reduced model attached to the unit's "
            "Design component, or returns the unit to the literature correlation with reduced_model null. "
            "set_cfd_study_template (confirm tier, must be the only action) records the CFD study inputs of the unit's linked "
            "Design component; the tube domain comes from its geometry, never from you, and nothing is recorded until Apply. "
            "Culture is feed-only and cannot pass through Flash, DistillationColumn or PFR. "
            "Mixed Recycles may be Jarvis tears; native Recycles inside mixed loops are refused. "
            "SpecifiedSeparator requires culture. For a non-convergence question, explain the recorded reason, "
            "iteration count, worst field and residual without proposing a change unless requested. "
            f"Kinetics: one typed reaction per PFR/CSTR; use a citation for literature provenance. {kinetics_explanation()} "
            "DWSIM Run stays operator-only; Thermo is edited in the operator editor."
        )
        text = _fit_process_brief(
            header=(f"Process workspace {workspace_id}; draft {draft_id}; head revision {record['revision']}\n"
                    f"Results: {projection['results']['state']}\n"),
            objects=objects, document=document, selected_line=f"Selected: {selected_names or 'none'}\n",
            pbr_rows=pbr_rows, reaction_rows=reaction_rows, run_summary=run_summary,
            selected_context=selected_context, tail=tail)
        bounded_text = text
        actions = ["set_value", "set_unit_model", "set_unit_hydraulics", "set_cfd_study_template", "set_reaction", "add_unit", "insert_unit_after", "connect", "disconnect", "mirror", "move", "rename", "delete"]
        limits = [f"One typed reaction per reactor; {kinetics_explanation()}", "DWSIM Run is operator-only"]
        payload = {
            "surface": "process",
            "route_id": ref.route_id,
            "workspace_id": workspace_id,
            "base_revision": record["revision"],
            "draft_id": draft_id,
            "candidate_id": None,
            "selected": selected,
            "summary": summary,
            "text": bounded_text,
            "actions": actions,
            "limits": limits,
        }
        return SurfaceBrief(
            surface="process",
            route_id=ref.route_id,
            workspace_id=workspace_id,
            base_revision=record["revision"],
            draft_id=draft_id,
            selected=selected,
            summary=summary,
            text=bounded_text,
            actions=actions,
            limits=limits,
            digest=_digest(payload),
        )

    candidate = get_candidate(workspace_id, ref.candidate_id) if ref.candidate_id else None
    if ref.candidate_id and candidate is None:
        return _brief_none(
            workspace_id,
            ref,
            "The referenced BLUECAD candidate is unknown or belongs to another workspace; no surface object was selected.",
        )
    if not ref.candidate_id:
        with open_sqlite_connection() as connection:
            row = connection.execute(
                "SELECT id FROM bluecad_candidates WHERE workspace_id=? AND status='valid' ORDER BY created_at DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
        candidate = get_candidate(workspace_id, str(row["id"])) if row else None
    if candidate is None or candidate.status != "valid" or not candidate.spec_artifact_id:
        return _brief_none(workspace_id, ref, "No valid BLUECAD candidate is available in this workspace.")
    spec = _load_candidate_spec(candidate.spec_artifact_id, workspace_id)
    part_by_id = {item["part_id"]: item for item in spec["parts"]}
    selected = [_bluecad_part_brief(part_by_id[item]) for item in ref.bluecad_part_ids if item in part_by_id]
    ignored = len(selected) != len(ref.bluecad_part_ids)
    summary = f"BLUECAD: {_safe_surface_label(candidate.brief_text, 'candidate')} · {len(spec['parts'])} part(s)"
    if selected:
        part = selected[0]
        summary += f" · selected: {part['part_id']} ({part['kind']})"
    if ignored:
        summary += "; unknown or foreign part selection ignored"
    display_parts = [_bluecad_part_brief(item) for item in spec["parts"][:20]]
    example_part = (selected[0] if selected else _bluecad_part_brief(spec["parts"][0]))["part_id"]
    text = (
        f"BLUECAD workspace {workspace_id}; candidate {candidate.id}; parts: "
        f"{json.dumps({'count': len(spec['parts']), 'items': display_parts}, separators=(',', ':'))}; selected: "
        f"{json.dumps(selected, separators=(',', ':'))}\nAction JSON examples: "
        f'{{"op":"duplicate_part","part":{json.dumps(example_part)},"placement":"beside","gap_mm":25}}; '
        f'{{"op":"set_part_param","part":{json.dumps(example_part)},"param":"length","value":2,"unit":"m"}}; '
        f'{{"op":"move_part","part":{json.dumps(example_part)},"dx":10,"dy":0,"dz":0,"unit":"mm"}}; '
        f'{{"op":"delete_part","part":{json.dumps(example_part)}}}.\n'
        f"Limits: supported part kinds {sorted(SUPPORTED_PART_KINDS)}; per-part supported parameters are listed above; "
        "base candidate is never modified; "
        "each action creates a child candidate."
    )
    bounded_text = text[:6000]
    actions = ["duplicate_part", "set_part_param", "move_part", "delete_part"]
    limits = [f"Supported GeometrySpec kinds: {', '.join(sorted(SUPPORTED_PART_KINDS))}",
              "base candidate is immutable; actions derive children"]
    payload = {
        "surface": "bluecad",
        "route_id": ref.route_id,
        "workspace_id": workspace_id,
        "base_revision": candidate.id,
        "draft_id": None,
        "candidate_id": candidate.id,
        "selected": selected,
        "summary": summary,
        "text": bounded_text,
        "actions": actions,
        "limits": limits,
    }
    return SurfaceBrief(
        surface="bluecad",
        route_id=ref.route_id,
        workspace_id=workspace_id,
        base_revision=candidate.id,
        candidate_id=candidate.id,
        selected=selected,
        summary=summary,
        text=bounded_text,
        actions=actions,
        limits=limits,
        digest=_digest(payload),
    )


BRIEF_TEXT_LIMIT = 6000  # SurfaceBrief.text max_length


def _owner_rows(objects: list[dict], *, compact: bool) -> str:
    """Registry-derived calculation owner of every unit (spec 170); grouped counts when space is tight."""
    units = [item for item in objects if item["kind"] == "unit"]
    if not compact:
        return "; ".join(f"{item['tag']}={spec_for(item).owner}" for item in units)
    counts: dict[str, int] = {}
    for item in units:
        owner = spec_for(item).owner
        counts[owner] = counts.get(owner, 0) + 1
    return "; ".join(f"{owner}: {count} unit(s)" for owner, count in sorted(counts.items()))


def _number(reported: dict, key: str, unit_fallback: str) -> str:
    row = reported.get(key)
    if not isinstance(row, dict):
        return "unknown"
    value = row.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    return f"{value:.4g} {row.get('units') or unit_fallback}"


def _verification_summary(workspace_id: str, card: dict) -> str:
    try:
        values = bio_models.get_set(workspace_id, card["parameter_set_id"])["values"]
    except Exception:  # noqa: BLE001 - a missing library record must not break the brief
        return "parameter set unavailable"
    if not values:
        return "parameter set has no values"
    counts: dict[str, int] = {}
    for value in values.values():
        state = str(value.get("state") or "candidate")
        counts[state] = counts.get(state, 0) + 1
    return "parameter values: " + ", ".join(f"{count} {state}" for state, count in sorted(counts.items()))


def _short_revision(revision: object) -> str:
    return f"revision {str(revision).removeprefix('r-')[:8]}" if revision else "revision unknown"


def _card_label(card: dict) -> str:
    return f"{card.get('name') or 'Unnamed card'} ({_short_revision(card.get('revision'))})"


def _pinned_card_label(workspace_id: str, pin: dict) -> str:
    """The currently pinned card by name and revision label; never a raw id."""
    if not pin:
        return "none"
    try:
        card = next((item for item in bio_models.list_cards(workspace_id) if item["id"] == pin.get("card_id")), None)
    except Exception:  # noqa: BLE001 - a broken library must not break the Apply card
        card = None
    if card is None:
        return f"Model card no longer in the library ({_short_revision(pin.get('card_revision'))})"
    return f"{card.get('name') or 'Unnamed card'} ({_short_revision(pin.get('card_revision'))})"


def _hydraulics_label(pin: dict | None) -> str:
    if not pin:
        return "literature correlation"
    return (f"CFD reduced model {_safe_surface_label(pin.get('reduced_model_version_id'), 'unknown')} "
            f"(studio version {_safe_surface_label(pin.get('studio_version_id'), 'unknown')})")


def _pbr_brief_rows(workspace_id: str, pbr_units: list[dict], last_run: dict | None) -> list[str]:
    """One bounded line per Photobioreactor: pinned card, verification summary and the last run's outcome."""
    if not pbr_units:
        return []
    try:
        cards = {card["id"]: card for card in bio_models.list_cards(workspace_id)}
    except Exception:  # noqa: BLE001
        cards = {}
    rows = []
    for item in pbr_units[:6]:
        pin = item.get("model") or {}
        card = cards.get(pin.get("card_id"))
        if not pin:
            model = "no model card pinned"
        elif card is None:
            model = "the pinned model card is unavailable"
        else:
            newer = "; a newer card revision exists" if card["revision"] != pin.get("card_revision") else ""
            model = (f"model card \"{_safe_surface_label(card.get('name'), 'unnamed')}\" revision "
                     f"{pin.get('card_revision')}{newer}; N source assumed {card.get('n_source', 'unknown')}; "
                     f"{_verification_summary(workspace_id, card)}")
        row = f"{item['tag']} (Photobioreactor): {model}; hydraulics {_hydraulics_label(item.get('hydraulics_model'))}."
        if last_run is None:
            row += " No run recorded."
        else:
            result = (last_run.get("units") or {}).get(item["tag"])
            status = last_run.get("status")
            if not isinstance(result, dict):
                solve = last_run.get("mixed_solve") or {}
                errors = solve.get("errors")
                ours = solve.get("failed_segment") == item["tag"] or item["tag"] in (solve.get("failed_units") or [])
                if ours and isinstance(errors, dict) and errors.get("code"):
                    meaning = _safe_surface_label(errors.get("message"), "", 240)
                    row += (f" Last run {status}: the PBR failed with {_safe_surface_label(errors['code'], 'unknown')}"
                            f"{' (' + meaning + ')' if meaning else ''}.")
                else:
                    reason = solve.get("reason", "unknown")
                    row += f" Last run {status}: no PBR result recorded (reason {reason})."
            else:
                reported = result.get("reported") or {}
                ratios = [abs(balance["residual"]) / balance["tolerance"]
                          for balance in (result.get("unit_balances") or {}).values()
                          if isinstance(balance, dict) and isinstance(balance.get("residual"), (int, float))
                          and isinstance(balance.get("tolerance"), (int, float)) and balance["tolerance"] > 0]
                residual = f"{max(ratios):.3g} of tolerance" if ratios else "unknown"
                closes = " (balances close)" if ratios and max(ratios) <= 1.0 else ""
                branch = result.get("branch", "unknown")
                row += (f" Last run {status}; branch {branch}; "
                        f"HRT {_number(reported, 'hrt_d', 'd')}; thin-culture growth rate Λ "
                        f"{_number(reported, 'lambda_h', '1/h')}; dilution rate D {_number(reported, 'dilution_h', '1/h')}; "
                        f"worst balance residual {residual}{closes}.{_BRANCH_MEANING.get(branch, '')}")
        rows.append(row)
    return rows


_BRANCH_MEANING = {
    "washout": " Washout: Λ < D, so growth cannot outrun dilution and the biomass leaves the tube; a longer HRT "
               "(lower flow or more volume) or more light would raise Λ relative to D.",
    "productive": " Productive: Λ > D, so the culture holds a positive periodic steady biomass.",
}


def _reaction_brief_rows(document: dict, last_run: dict | None) -> list[str]:
    rows: list[str] = []
    reactions = document.get("reactions") or {}
    solved = (last_run or {}).get("units") or {}
    for unit in document["objects"].values():
        if unit["kind"] != "unit" or unit["type"] not in {"PFR", "CSTR"}:
            continue
        for reaction_id in unit.get("reactions") or []:
            reaction = reactions.get(reaction_id) or {}
            law = reaction.get("rate_law") or {}
            verification = (solved.get(unit["tag"]) or {}).get("kinetics") or {}
            state = "verified" if verification.get("verified") else "not verified"
            rows.append(f"{unit['tag']} reaction {reaction_id}: {law.get('form', 'power law')}, "
                        f"base {reaction.get('base_reactant', 'unknown')}; last run {state}.")
    return rows[:12]


def _fit_process_brief(*, header: str, objects: list[dict], document: dict, selected_line: str,
                       pbr_rows: list[str], reaction_rows: list[str], run_summary: str,
                       selected_context: str, tail: str) -> str:
    """Assemble the Process brief within the model-facing cap; the vocabulary tail is never truncated."""
    def build(limit: int, compact: bool, rows: list[str], reactions: list[str]) -> str:
        return (f"{header}Objects ({len(objects)}): {_compact_objects(objects, document, limit)}\n{selected_line}"
                f"Unit owners: {_owner_rows(objects, compact=compact)}\n"
                f"{chr(10).join(rows) + chr(10) if rows else ''}"
                f"{chr(10).join(reactions) + chr(10) if reactions else ''}"
                f"{run_summary}\n{selected_context}{tail}")

    text = ""
    for limit, compact, count, reaction_count in ((35, False, 6, 12), (35, True, 6, 12),
                                                   (20, True, 6, 8), (10, True, 4, 5),
                                                   (5, True, 2, 2), (0, True, 1, 0)):
        text = build(limit, compact, pbr_rows[:count], reaction_rows[:reaction_count])
        if len(text) <= BRIEF_TEXT_LIMIT:
            return text
    return text[:BRIEF_TEXT_LIMIT]


def _process_object_brief(item: dict) -> dict:
    if item["kind"] == "stream":
        properties = {}
        if item["type"] == "EnergyStream":
            current = item.get("spec", {}).get("duty")
            properties["duty"] = _property_brief(current, "power")
        else:
            for key, (kind, _storage_key, _label) in STREAM_SPECS.items():
                properties[key] = _property_brief(item.get("spec", {}).get(key), kind)
            properties["composition"] = {
                "kind": "composition",
                "unit": item.get("spec", {}).get("composition_basis", "mass"),
                "value": item.get("spec", {}).get("composition"),
            }
            properties["culture"] = item.get("spec", {}).get("culture")
        return {
            "kind": item["kind"],
            "type": item["type"],
            "id": item["id"],
            "tag": item["tag"],
            "properties": properties,
            "culture": item.get("spec", {}).get("culture"),
        }
    unit_spec = spec_for(item)
    properties = {
        param.key: _property_brief(item.get("params", {}).get(param.key), param.kind)
        for param in unit_spec.params_for(item.get("mode"))
    }
    declaration = spec_for(item)
    return {
        "kind": item["kind"],
        "type": item["type"],
        "id": item["id"],
        "tag": item["tag"],
        "owner": declaration.owner,
        "culture_rule": declaration.culture_rule,
        "mode": item.get("mode"),
        "properties": properties,
    }


def _property_brief(current: dict | None, kind: str) -> dict:
    display_unit = current.get("unit") if current else QUANTITY_UNITS[kind][1][0]
    return {
        "kind": kind,
        "si_unit": QUANTITY_UNITS[kind][0],
        "unit": display_unit,
        "value": current.get("value") if current else None,
        "si_value": current.get("si") if current else None,
    }


def _bluecad_part_brief(part: dict) -> dict:
    return {
        "part_id": part["part_id"],
        "kind": part["kind"],
        "params": {
            key: {
                "value": value,
                "unit": "deg" if key == "angle" else "unitless" if key in {"n_out", "n_mounts", "branch_count"} else "mm",
            }
            for key, value in part["params"].items()
        },
        "frame": part.get("frame", {"origin": [0.0, 0.0, 0.0], "direction": [1.0, 0.0, 0.0]}),
    }


def _safe_surface_label(value: object, fallback: str, limit: int = 80) -> str:
    """Keep owner labels readable without echoing paths or credential-like text."""
    if not isinstance(value, str):
        return fallback
    label = re.sub(r"(?:[A-Za-z]:\\|/)[^\s,;]+", "[path]", value)
    label = re.sub(r"(?i)\b(api[_ -]?key|token|password|secret)\s*[:=]\s*\S+", r"\1=[redacted]", label)
    label = " ".join(label.split())[:limit].strip(" .,:;-")
    return label or fallback


def _compact_objects(objects: list[dict], document: dict, limit: int = 35) -> str:
    by_id = document["objects"]
    rows = []
    for item in objects[:limit]:
        row = f"{item['tag']}:{item['type']}"
        if item["kind"] == "stream":
            source = by_id.get((item.get("source") or {}).get("unit"), {}).get("tag", "external")
            target = by_id.get((item.get("target") or {}).get("unit"), {}).get("tag", "external")
            row += f" ({source} → {target})"
        rows.append(row)
    if len(objects) > limit:
        rows.append(f"… {len(objects) - limit} more")
    return ", ".join(rows) or "none"


def _load_candidate_spec(artifact_id: str, workspace_id: str) -> dict:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT stored_path FROM artifacts WHERE id=? AND workspace_id=? AND status='registered'",
            (artifact_id, workspace_id),
        ).fetchone()
    if not row:
        raise ActionError("candidate_artifact_missing", "Candidate geometry artifact is unavailable.", 404)
    from pathlib import Path

    path = Path(row["stored_path"])
    spec = json.loads(path.read_text(encoding="utf-8"))
    return canonicalize_geometry_spec(spec)


def _store(outcome: ActionOutcome, request: ActionRequest, origin: ActionOrigin) -> ActionOutcome:
    now = outcome.updated_at
    with open_sqlite_connection() as connection:
        connection.execute(
            """INSERT INTO workspace_actions
            (id,workspace_id,surface,state,tier,origin_json,thread_id,interaction_id,relay_run_id,request_json,request_digest,outcome_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                outcome.action_id,
                outcome.workspace_id,
                outcome.surface,
                outcome.state,
                outcome.tier,
                origin.model_dump_json(),
                origin.thread_id,
                origin.interaction_id,
                origin.relay_run_id,
                _canonical(request.model_dump(mode="json")),
                outcome.request_digest,
                outcome.model_dump_json(),
                outcome.created_at,
                now,
            ),
        )
        connection.commit()
    return outcome


def _outcome(
    workspace_id: str,
    request: ActionRequest,
    origin: ActionOrigin,
    state: Literal["applied", "proposed", "refused", "stale", "dismissed", "undone"],
    tier: Literal["immediate", "confirm", "none"],
    summary: str,
    *,
    changes: list[ChangeLine] | None = None,
    reason: str | None = None,
    reason_code: str | None = None,
    draft_id: str | None = None,
) -> ActionOutcome:
    now = _now()
    return ActionOutcome(
        action_id=str(uuid4()),
        workspace_id=workspace_id,
        surface=request.surface,
        state=state,
        tier=tier,
        summary=summary,
        changes=changes or [],
        base_revision=request.base_revision,
        draft_id=draft_id,
        reason_code=reason_code,
        reason=reason,
        origin=origin,
        request_digest=_digest(request.model_dump(mode="json")),
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
    )


def _action_summary(request: ActionRequest, changes: list[ChangeLine]) -> str:
    """Turn validated requests and their resolved changes into concise card text."""
    summaries = []
    resolved_unit_tags: set[str] = set()
    for action in request.actions:
        if action.op == "set_value":
            line = next((item for item in changes if item.label == f"{action.target} {action.property}"), None)
            value = re.sub(r"(?<=\d)\.0(?=\s|$)", "", line.after) if line and line.after else None
            summaries.append(f"Set {action.target} {action.property} to {value}" if value else
                             f"Set {action.target} {action.property}")
        elif action.op == "set_unit_model":
            line = next((item for item in changes if item.label == f"{action.unit} biological model card"), None)
            summaries.append(f"Pin model card {line.after} on {action.unit}" if line and line.after
                             else f"Pin a model card on {action.unit}")
        elif action.op == "set_unit_hydraulics":
            summaries.append(f"Pin CFD reduced model {action.reduced_model} on {action.unit}" if action.reduced_model
                             else f"Return {action.unit} to the literature correlation")
        elif action.op == "set_cfd_study_template":
            summaries.append(f"Set CFD {action.regime} study template on {action.unit}")
        elif action.op == "set_reaction":
            summaries.append(f"Set reaction {action.reaction_id} on {action.unit}")
        elif action.op == "add_unit":
            tag = action.tag
            if tag is None:
                line = next((item for item in changes
                             if item.label.endswith(f"— new {action.type}")
                             and item.label.split(" — ", 1)[0] not in resolved_unit_tags), None)
                tag = line.label.split(" — ", 1)[0] if line else action.type
            resolved_unit_tags.add(tag)
            summaries.append(f"Add {action.type} {tag}" + (f" near {action.near}" if action.near else ""))
        elif action.op == "insert_unit_after":
            tag = action.tag
            if tag is None:
                line = next((item for item in changes
                             if (item.label.endswith(f"— new {action.type}")
                                 or item.label.endswith(f" — inserted after {action.after}"))
                             and item.label.split(" — ", 1)[0] not in resolved_unit_tags), None)
                tag = line.label.split(" — ", 1)[0] if line else action.type
            resolved_unit_tags.add(tag)
            summaries.append(f"Add {action.type} {tag} after {action.after}")
        elif action.op == "mirror":
            summaries.append(f"Mirror {action.target} {action.axis}ly" if action.axis == "horizontal"
                             else f"Mirror {action.target} vertically")
        elif action.op == "move":
            summaries.append(f"Move {action.target} by ({action.dx:g}, {action.dy:g})")
        elif action.op == "connect":
            summaries.append(f"Connect {action.source} to {action.to}")
        elif action.op == "disconnect":
            summaries.append(f"Disconnect {action.stream}")
        elif action.op == "rename":
            summaries.append(f"Rename {action.target} to {action.new_tag}")
        elif action.op == "delete":
            summaries.append(f"Delete {action.target}")
    return "; ".join(summaries)[:400] or "Workspace action"


def _studio_proposal(workspace_id: str, request: ActionRequest, origin: ActionOrigin) -> ActionOutcome:
    from app.modules.studio import service as studio
    from app.modules.studio.geometry import geometry_scaffold
    from app.modules.studio.unit_models import unit_scaffold

    action = request.actions[0]
    assert isinstance(action, StudioDraftAction)
    try:
        contract = studio.validate_proposed_source(action.category, action.name,
                                                    action.engineering_question, action.contract, action.source)
        before = None
        if action.mode == "update":
            before = studio.get(workspace_id, request.draft_id or "")
            if (before["status"] != "draft" or before["category"] != action.category
                    or studio.proposal_revision(before) != request.base_revision):
                raise studio.StudioError("studio_proposal_stale", "Selected draft changed.")
        elif request.base_revision != "new" or request.draft_id is not None:
            raise studio.StudioError("studio_proposal_stale", "Create context changed.")
    except (studio.StudioError, ValidationError, ValueError) as exc:
        code = getattr(exc, "code", "studio_proposal_invalid")
        return _outcome(workspace_id, request, origin, "stale" if code.endswith("stale") else "refused",
                        "none", "Studio proposal could not be prepared.", reason=str(exc)[:300],
                        reason_code=code)
    if before is None:
        old_source = (unit_scaffold(contract) if action.category == "process_unit" else
                      geometry_scaffold(contract) if action.category == "geometry" else studio.scaffold(contract))
        old_contract = "No draft"
    else:
        old_source = before["source"]
        old_contract = _canonical({key: before[key] for key in contract})
        if _secret_like(before["name"], old_contract, old_source, before["assumptions"]):
            return _outcome(workspace_id, request, origin, "refused", "none", "Studio proposal refused.",
                            reason="Selected draft contains secret-like content.", reason_code="secret_detected")
    changes = [ChangeLine(label="Model", before=before["name"] if before else None, after=action.name),
               ChangeLine(label="Contract", before=old_contract, after=_canonical(contract)),
               ChangeLine(label="Source", before=old_source, after=action.source),
               ChangeLine(label="Assumptions", before=before["assumptions"] if before else None,
                          after=action.assumptions)]
    return _outcome(workspace_id, request, origin, "proposed", "confirm",
                    f"{('Revise' if before else 'Create')} {action.category} draft {action.name}; review contract, source and assumptions.",
                    changes=changes, draft_id=request.draft_id)


def submit(workspace_id: str, request: ActionRequest, origin: ActionOrigin) -> ActionOutcome:
    request = request.model_copy(update={"actions": _unique_actions(request)})
    digest = _digest(request.model_dump(mode="json"))
    if origin.kind == "relay" and not origin.relay_run_id:
        return _store(
            _outcome(
                workspace_id,
                request,
                origin,
                "refused",
                "none",
                "Relay action provenance is incomplete.",
                reason="A relay_run_id is required for Relay-origin actions.",
                reason_code="origin_invalid",
            ),
            request,
            origin,
        )
    if origin.kind == "local" and not origin.interaction_id:
        return _store(
            _outcome(
                workspace_id,
                request,
                origin,
                "refused",
                "none",
                "Local action provenance is incomplete.",
                reason="An interaction_id is required for local actions.",
                reason_code="origin_invalid",
            ),
            request,
            origin,
        )
    if origin.kind == "local":
        previous = find_local_duplicate(workspace_id, str(origin.interaction_id), request)
        if previous is not None:
            # One local turn may repeat an identical tool call; it must not
            # create a second card or a second BLUECAD child.
            return previous
    if request.surface == "studio":
        action = request.actions[0]
        if _secret_like(_canonical(action.model_dump(mode="json")), request.rationale):
            # Never persist secret-like proposal content, not even inside a refused card.
            redacted = request.model_copy(update={"rationale": None, "actions": [action.model_copy(update={
                "name": "[redacted]", "engineering_question": "[redacted]", "contract": {},
                "source": "# [redacted: secret-like content]\n", "assumptions": ""})]})
            return _store(_outcome(workspace_id, redacted, origin, "refused", "none", "Studio proposal refused.",
                                   reason="Secret-like content cannot be stored in a proposal.",
                                   reason_code="secret_detected"), redacted, origin)
        return _store(_studio_proposal(workspace_id, request, origin), request, origin)
    if request.surface == "bluecad":
        base_candidate = get_candidate(workspace_id, request.base_revision)
        if base_candidate is None or base_candidate.status != "valid":
            return _store(
                _outcome(
                    workspace_id,
                    request,
                    origin,
                    "stale",
                    "none",
                    "Base BLUECAD candidate is no longer valid.",
                    reason="The referenced base candidate is unavailable or archived.",
                    reason_code="stale",
                ),
                request,
                origin,
            )
        try:
            from app.modules.workspace_actions.bluecad_actions import apply_bluecad_request

            outcome = apply_bluecad_request(workspace_id, request, origin, digest)
        except (ValueError, KeyError) as exc:
            outcome = _outcome(
                workspace_id,
                request,
                origin,
                "refused",
                "none",
                "Action was refused.",
                reason=str(exc),
                reason_code="invalid_action",
            )
        return _store(outcome, request, origin)

    found = _find_draft(workspace_id, revision=request.base_revision, draft_id=request.draft_id)
    if not found:
        return _store(
            _outcome(
                workspace_id,
                request,
                origin,
                "stale",
                "none",
                "Process draft changed or could not be uniquely resolved; refresh the brief.",
                reason="No unique current Process draft matches this base revision. Include draft_id from the surface brief and retry.",
                reason_code="stale",
            ),
            request,
            origin,
        )
    draft_id, record = found
    if request.actions[0].op == "set_cfd_study_template":
        return _store(_study_template_proposal(workspace_id, request, origin, draft_id, record["document"]),
                      request, origin)
    try:
        ops, changes = _process_ops(record["document"], request, workspace_id, draft_id)
        after = draft.apply_ops(record["document"], ops)
        before_culture_blockers = {
            (item["code"], item["object"], item["field"])
            for item in draft.validate_document(record["document"])
            if item["severity"] == "blocker" and item["code"].startswith("CULTURE_")
        }
        after_culture_blockers = {
            (item["code"], item["object"], item["field"])
            for item in draft.validate_document(after)
            if item["severity"] == "blocker" and item["code"].startswith("CULTURE_")
        }
        if after_culture_blockers - before_culture_blockers:
            new_codes = sorted(code for code, _tag, _field in after_culture_blockers - before_culture_blockers)
            raise ValueError("Culture refusal: " + ", ".join(new_codes)
                             + ". Biology cannot pass through DWSIM VLE or reactors; no change was made.")
        if any(op.op == "delete" for op in ops):
            before_blockers = {
                (item["code"], item["object"], item["field"])
                for item in draft.validate_document(record["document"])
                if item["severity"] == "blocker"
            }
            after_blockers = {
                (item["code"], item["object"], item["field"])
                for item in draft.validate_document(after)
                if item["severity"] == "blocker"
            }
            if after_blockers - before_blockers:
                raise ValueError(
                    "Delete would leave required Process data invalid: "
                    + ", ".join(sorted(code for code, _obj, _field in after_blockers - before_blockers))
                )
    except Exception as exc:
        return _store(
            _outcome(
                workspace_id,
                request,
                origin,
                "refused",
                "none",
                "Action was refused.",
                reason=str(exc),
                reason_code="invalid_action",
                draft_id=draft_id,
            ),
            request,
            origin,
        )
    tier: Literal["immediate", "confirm"] = (
        "confirm"
        if origin.kind == "relay" or any(op.op not in {"move", "set_orientation"} for op in ops)
        else "immediate"
    )
    result = _outcome(
        workspace_id,
        request,
        origin,
        "proposed" if tier == "confirm" else "applied",
        tier,
        _action_summary(request, changes),
        changes=changes,
        draft_id=draft_id,
    )
    if tier == "immediate":
        try:
            updated = draft.patch(
                workspace_id,
                draft_id,
                request.base_revision,
                ops,
                actor=_actor(origin),
                provenance={"action_request_digest": digest},
            )
            result.result_revision = updated["revision"]
            result.undo_available = True
        except draft.DraftError as exc:
            result.state, result.reason_code, result.reason = "stale", exc.code, str(exc)
    return _store(result, request, origin)


_FINGERPRINT_RE = re.compile(r"^revision (\d+); contract (sha256:[0-9a-f]{16})$")


def _study_template_state(workspace_id: str, draft_id: str, document: dict, action: Any) -> tuple[str, Any, dict]:
    """Resolve unit tag -> (unit id, validated contract, provenance) through the Design owner; ValueError on refusal."""
    from app.modules.workspaces.design_components import StudyTemplateRefused, resolve_process_study_template

    target = _target(document, action.unit, "unit")
    inputs = action.model_dump(mode="json", exclude={"op", "unit", "regime"})
    try:
        contract, provenance = resolve_process_study_template(workspace_id, draft_id, target["id"], action.regime, inputs)
    except StudyTemplateRefused as error:
        raise ValueError(str(error)) from error
    return target["id"], contract, provenance


def _fingerprint(provenance: dict) -> str:
    return f"revision {provenance['component_revision']}; contract {provenance['contract_digest'][:23]}"


def _study_template_proposal(workspace_id: str, request: ActionRequest, origin: ActionOrigin, draft_id: str,
                             document: dict) -> ActionOutcome:
    """Always a confirm-tier proposal; nothing is written until Apply re-resolves and writes the same typed record."""
    action = request.actions[0]
    assert isinstance(action, SetCfdStudyTemplate)
    try:
        _, contract, provenance = _study_template_state(workspace_id, draft_id, document, action)
    except Exception as exc:
        return _outcome(workspace_id, request, origin, "refused", "none", "Action was refused.",
                        reason=str(exc), reason_code="invalid_action", draft_id=draft_id)
    tag = _target(document, action.unit, "unit")["tag"]
    previous = provenance["previous_template_digest"]
    turbulence = contract.turbulence_model or "no turbulence model"
    changes = [
        ChangeLine(label=f"{tag} CFD study template ({action.regime})",
                   before=previous[:19] if previous else "none",
                   after=(f"{provenance['template_digest'][:19]}: Re {contract.reynolds:.0f}, "
                          f"{contract.mean_velocity_m_s:.3g} m/s, mesh level {contract.mesh_level}, {turbulence}")),
        # Proposal-time fingerprint; Apply re-resolves and refuses as stale if either part moved.
        ChangeLine(label=f"{tag} Design component", before=None, after=_fingerprint(provenance)),
    ]
    return _outcome(workspace_id, request, origin, "proposed", "confirm", _action_summary(request, changes),
                    changes=changes, draft_id=draft_id)


def _apply_study_template(workspace_id: str, outcome: ActionOutcome, request: ActionRequest,
                          origin: ActionOrigin) -> None:
    """Re-resolve, compare with the proposal, then call the shared typed write (the GUI route's function)."""
    from fastapi import HTTPException

    from app.modules.workspaces.design_components import record_study_template

    action = request.actions[0]
    assert isinstance(action, SetCfdStudyTemplate)

    def stale(reason: str) -> None:
        outcome.state, outcome.reason_code, outcome.reason = "stale", "stale", reason

    try:
        found = _find_draft(workspace_id, draft_id=outcome.draft_id)
        if not found:
            stale("The Process draft could not be resolved; ask again.")
            return
        try:
            _, contract, provenance = _study_template_state(workspace_id, found[0], found[1]["document"], action)
        except ValueError as exc:
            stale(f"The component or its tube geometry changed since this proposal: {exc}")
            return
        fingerprint = next((_FINGERPRINT_RE.match(line.after or "") for line in outcome.changes
                            if line.label.endswith(" Design component")), None)
        if fingerprint is None:
            stale("The proposal carries no component fingerprint; ask again.")
            return
        if (int(fingerprint.group(1)) != provenance["component_revision"]
                or fingerprint.group(2) != provenance["contract_digest"][:23]):
            stale("The Design component or its tube geometry changed after this proposal; ask again.")
            return
        try:
            record_study_template(
                provenance["design_project_id"], provenance["design_version_id"], provenance["design_component_id"],
                contract, expected_revision=provenance["component_revision"],
                expected_active_version_id=provenance["active_version_id"], actor=_actor(origin))
        except HTTPException as exc:
            outcome.state = "stale" if exc.status_code == 409 else "refused"
            outcome.reason_code = "stale" if exc.status_code == 409 else "apply_failed"
            outcome.reason = str(exc.detail)[:500]
            return
        outcome.state, outcome.undo_available = "applied", False
        outcome.summary = outcome.summary + " (recorded in the Design component history; no undo for study templates)"
    except Exception as exc:  # never leave the claim stuck in 'applying'
        outcome.state, outcome.reason_code, outcome.reason = "refused", "apply_failed", str(exc)[:500]


def find_local_duplicate(workspace_id: str, interaction_id: str, request: ActionRequest) -> ActionOutcome | None:
    """Return the outcome of an identical request already submitted by the same local interaction."""
    request = request.model_copy(update={"actions": _unique_actions(request)})
    digest = _digest(request.model_dump(mode="json"))
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT outcome_json FROM workspace_actions WHERE workspace_id=? AND interaction_id=? AND request_digest=? "
            "AND json_extract(origin_json, '$.kind')='local' ORDER BY created_at LIMIT 1",
            (workspace_id, interaction_id, digest),
        ).fetchone()
    return ActionOutcome.model_validate_json(row["outcome_json"]) if row else None


def _unique_actions(request: ActionRequest) -> list[ProcessAction | BluecadAction | StudioDraftAction]:
    """Drop repeated idempotent assignments, preserving repeated additive actions."""
    actions: list[ProcessAction | BluecadAction | StudioDraftAction] = []
    seen: set[str] = set()
    for action in request.actions:
        if action.op not in {"set_value", "set_unit_model", "set_unit_hydraulics", "set_cfd_study_template", "set_part_param"}:
            actions.append(action)
            continue
        encoded = _canonical(action.model_dump(mode="json", by_alias=False))
        if encoded not in seen:
            seen.add(encoded)
            actions.append(action)
    return actions


def _target(document: dict, tag: str, kind: str | None = None) -> dict:
    item = document["objects"].get(tag) or next(
        (obj for obj in document["objects"].values() if obj["tag"] == tag), None
    )
    if item is None or kind and item["kind"] != kind:
        raise ValueError(f"Unknown {kind or 'Process object'} {tag!r}.")
    return item


def _port_index(unit_type: str, port_name: str | None, end: str) -> int:
    spec = draft.UNIT_REGISTRY[unit_type]
    ports = spec.outlets if end == "outlet" else spec.inlets
    if port_name is None:
        return 0
    if port_name not in ports:
        raise ValueError(f"{unit_type} {end} port must be one of: {', '.join(ports)}.")
    return ports.index(port_name)


def _next_id(objects: dict, prefix: str, ops: list | None = None) -> str:
    index = len(objects) + 1
    used = set(objects)
    for op in ops or []:
        if getattr(op, "id", None):
            used.add(op.id)
    while f"{prefix}{index}" in used:
        index += 1
    return f"{prefix}{index}"


def _next_tag(objects: dict, prefix: str, ops: list | None = None) -> str:
    used = {item["tag"] for item in objects.values()}
    used.update(tag for op in ops or [] if (tag := getattr(op, "tag", None)))
    index = 1
    while f"{prefix}{index}" in used:
        index += 1
    return f"{prefix}{index}"


def _registry_unit_type(name: str) -> str:
    """A registry type, or its operator label ("Photobioreactor (T1)", "Photobioreactor", "flash vessel")."""
    if name in draft.UNIT_REGISTRY:
        return name
    wanted = " ".join(name.lower().split())
    matches = {key for key, spec in draft.UNIT_REGISTRY.items()
               if wanted in {key.lower(), spec.label.lower(), re.sub(r"\s*\([^)]*\)$", "", spec.label).lower()}}
    if len(matches) == 1:
        return matches.pop()
    raise ValueError(f"Unsupported unit type {name!r}; supported types: {', '.join(sorted(draft.UNIT_REGISTRY))}.")


def _resolve_model_card(workspace_id: str, reference: str) -> dict:
    """A card id, else an exact unique name, resolved to its current revision and digest (spec 170)."""
    cards = bio_models.list_cards(workspace_id)
    by_id = [card for card in cards if card["id"] == reference]
    if by_id:
        return by_id[0]
    named = [card for card in cards if card["name"] == reference]
    if not named:
        # "use model card <name>" often reaches the action with the phrase's own words or quotes
        # around the name; the stripped name must still match exactly.
        stripped = re.sub(r"^(?:the\s+)?(?:model\s+)?card\s+", "", reference.strip().strip("\"'“”‘’"), flags=re.IGNORECASE)
        named = [card for card in cards if card["name"] == stripped.strip("\"'“”‘’")]
    if not named:
        raise ValueError(f"No model card has the id or exact name {reference!r}; ask the operator to create or name one "
                         "in the Biology model library.")
    if len(named) > 1:
        raise ValueError(f"Model card name {reference!r} matches more than one card; use one of these ids: "
                         f"{', '.join(card['id'] for card in named)}.")
    return named[0]


def _process_ops(document: dict, request: ActionRequest, workspace_id: str | None = None,
                 draft_id: str | None = None, pins: list[tuple[str, UnitHydraulicsPin, dict]] | None = None,
                 ) -> tuple[list[DraftOp], list[ChangeLine]]:
    ops: list[DraftOp] = []
    changes: list[ChangeLine] = []
    objects = document["objects"]
    culture_updates: dict[str, dict[str, DraftQuantity]] = {}
    for action in request.actions:
        if action.op in {"duplicate_part", "set_part_param", "move_part", "delete_part"}:
            raise ValueError("BLUECAD actions cannot execute on the Process surface.")
        if action.op == "set_unit_model":
            target = _target(document, action.unit, "unit")
            if target["type"] != "PhotobioreactorT1":
                raise ValueError(f"{target['tag']} is a {target['type']}; only a Photobioreactor (T1) pins a model card.")
            if workspace_id is None:
                raise ValueError("Model cards require a workspace context.")
            card = _resolve_model_card(workspace_id, action.card)
            pin = {"card_id": card["id"], "card_revision": card["revision"], "card_digest": card["digest"]}
            ops.append(DraftSetUnitModel(op="set_unit_model", unit=target["id"], model=UnitModelPin(**pin)))
            current = target.get("model") or {}
            changes.append(ChangeLine(
                label=f"{target['tag']} biological model card",
                before=_pinned_card_label(workspace_id, current),
                after=_card_label(card)))
        elif action.op == "set_unit_hydraulics":
            target = _target(document, action.unit, "unit")
            if target["type"] != "PhotobioreactorT1":
                raise ValueError(f"{target['tag']} is a {target['type']}; only a Photobioreactor (T1) pins a hydraulics model.")
            hyd_current = target.get("hydraulics_model")
            hyd_before = _hydraulics_label(hyd_current) if hyd_current else "literature correlation"
            if action.reduced_model is None:
                ops.append(DraftSetUnitHydraulics(op="set_unit_hydraulics", unit=target["id"], hydraulics=None))
                hyd_after = "literature correlation"
            else:
                if workspace_id is None or draft_id is None:
                    raise ValueError("Hydraulics models require a workspace and draft context.")
                from app.modules.workspaces.design_components import (
                    HydraulicsPinRefused,
                    resolve_process_hydraulics_pin,
                )

                try:
                    hyd_pin, hyd_provenance = resolve_process_hydraulics_pin(
                        workspace_id, draft_id, target["id"], action.reduced_model, action.out_of_range)
                except HydraulicsPinRefused as error:
                    raise ValueError(str(error)) from error
                ops.append(DraftSetUnitHydraulics(op="set_unit_hydraulics", unit=target["id"], hydraulics=hyd_pin))
                if pins is not None:
                    pins.append((target["id"], hyd_pin, hyd_provenance))
                hyd_after = f"CFD reduced model {hyd_pin.reduced_model_version_id}"
            changes.append(ChangeLine(label=f"{target['tag']} hydraulics model", before=hyd_before, after=hyd_after))
        elif action.op == "set_reaction":
            target = _target(document, action.unit, "unit")
            if target["type"] not in {"PFR", "CSTR"}:
                raise ValueError(f"{target['tag']} is not a kinetic reactor (PFR or CSTR).")
            try:
                reaction = KineticReaction.model_validate(action.reaction)
            except ValidationError as error:
                # Operator-facing refusal: name the fields, never pydantic's developer text.
                # A model-level rule has no field location; its own message is the actionable part.
                fields = sorted({".".join(str(part) for part in item["loc"]).replace("_", " ")
                                 or str(item["msg"]).removeprefix("Value error, ") for item in error.errors()})
                raise ValueError(f"The reaction for {target['tag']} is incomplete or invalid: "
                                 f"check {', '.join(fields[:6])}.") from None
            current = (document.get("reactions") or {}).get(action.reaction_id)
            ops.append(SetReactorReaction(op="set_reactor_reaction", unit=target["id"],
                                          reaction_id=action.reaction_id, reaction=reaction))
            changes.append(ChangeLine(label=f"{target['tag']} reaction {action.reaction_id}",
                                      before=(current or {}).get("name"), after=reaction.name))
        elif action.op == "set_value":
            target = _target(document, action.target)
            if target["kind"] == "stream":
                culture_fields = {"biomass", "nitrogen", "phosphorus", "oxygen", "dic", "ph", "salinity"}
                supported = {"temperature", "pressure", "mass_flow", "molar_flow", "vapor_fraction", "composition"} | culture_fields
                if action.property not in supported:
                    raise ValueError(
                        "Streams support temperature, pressure, mass_flow, molar_flow, vapor_fraction, composition and feed culture fields."
                    )
                value = action.value
                if action.property in culture_fields:
                    if target["type"] == "EnergyStream" or target.get("source") is not None:
                        raise ValueError("Culture can only be set on a material feed. Computed streams are read-only.")
                    stream_id = target["id"]
                    if stream_id not in culture_updates:
                        original = target.get("spec", {}).get("culture")
                        culture_updates[stream_id] = {
                            key: DraftQuantity(value=quantity["value"], unit=quantity["unit"])
                            for key, quantity in (original or {}).items()
                        }
                    if value is None:
                        culture_updates[stream_id].pop(action.property, None)
                    elif isinstance(value, Quantity):
                        culture_updates[stream_id][action.property] = DraftQuantity(value=value.value, unit=value.unit)
                    else:
                        raise ValueError("Culture fields require a numeric value and unit, or null to mark unknown.")
                    before = target.get("spec", {}).get("culture", {}).get(action.property)
                    after = value.model_dump() if isinstance(value, Quantity) else None
                elif action.property == "composition":
                    if not isinstance(value, dict):
                        raise ValueError("Stream composition must be a compound fraction map.")
                    ops.append(SetStreamSpec(op="set_stream_spec", stream=target["id"], composition=value))
                elif isinstance(value, Quantity):
                    quantity = DraftQuantity(value=value.value, unit=value.unit)
                    if action.property == "temperature":
                        ops.append(SetStreamSpec(op="set_stream_spec", stream=target["id"], temperature=quantity))
                    elif action.property == "pressure":
                        ops.append(SetStreamSpec(op="set_stream_spec", stream=target["id"], pressure=quantity))
                    elif action.property == "mass_flow":
                        ops.append(SetStreamSpec(op="set_stream_spec", stream=target["id"], mass_flow=quantity))
                    elif action.property == "molar_flow":
                        ops.append(SetStreamSpec(op="set_stream_spec", stream=target["id"], molar_flow=quantity))
                    elif action.property == "vapor_fraction":
                        ops.append(SetStreamSpec(op="set_stream_spec", stream=target["id"], vapor_fraction=quantity))
                else:
                    raise ValueError("Stream values must include a numeric value and unit.")
                before = target.get("spec", {}).get(action.property)
                after = value.model_dump() if isinstance(value, Quantity) else value
            elif target["kind"] == "unit":
                supported = {p.key for p in spec_for(target).params}
                if action.property not in supported:
                    raise ValueError(
                        f"{target['type']} supports parameters: {', '.join(sorted(supported)) or 'none'}; kinetics is unsupported."
                    )
                if not isinstance(action.value, Quantity):
                    raise ValueError("Unit parameter values must include a numeric value and unit.")
                quantity = DraftQuantity(value=action.value.value, unit=action.value.unit)
                ops.append(SetUnitParams(op="set_unit_params", unit=target["id"], values={action.property: quantity}))
                before = target.get("params", {}).get(action.property)
                after = quantity.model_dump()
            else:
                raise ValueError("This Process object cannot be edited.")
            changes.append(
                ChangeLine(
                    label=f"{target['tag']} {action.property}",
                    before=_display(before),
                    after=_display(after) or str(after),
                )
            )
        elif action.op == "add_unit":
            action = action.model_copy(update={"type": _registry_unit_type(action.type)})
            near = _target(document, action.near, "unit") if action.near else None
            tag = action.tag or _next_tag(objects, f"{action.type}_", ops)
            ops.append(
                AddUnit(
                    op="add_unit",
                    type=action.type,
                    tag=tag,
                    x=(near or {"x": max((o["x"] for o in objects.values()), default=0) + 160})["x"] + 120,
                    y=(near or {"y": 0})["y"],
                )
            )
            changes.append(ChangeLine(label=f"{tag} — new {action.type}", after="added"))
        elif action.op == "insert_unit_after":
            action = action.model_copy(update={"type": _registry_unit_type(action.type)})
            upstream = _target(document, action.after, "unit")
            outlets = [
                stream
                for stream in objects.values()
                if stream["kind"] == "stream"
                and stream["type"] != "EnergyStream"
                and (stream.get("source") or {}).get("unit") == upstream["id"]
            ]
            if len(outlets) != 1:
                raise ValueError(
                    f"Cannot insert after {upstream['tag']}: expected one material outlet stream, found {len(outlets)}."
                )
            outlet = outlets[0]
            target_end = outlet.get("target")
            new_tag = action.tag or _next_tag(objects, f"{action.type}_", ops)
            new_unit_id = _next_id(objects, "u", ops)
            stream_tag = _next_tag(objects, "S_auto", ops)
            stream_id = _next_id(objects, "s", ops)
            near = {"x": upstream["x"], "y": upstream["y"]}
            ops.extend(
                [
                    AddUnit(
                        op="add_unit", id=new_unit_id, type=action.type, tag=new_tag, x=near["x"] + 160, y=near["y"]
                    ),
                    AddStream(op="add_stream", id=stream_id, tag=stream_tag, x=near["x"] + 80, y=near["y"]),
                    Disconnect(op="disconnect", stream=outlet["id"], end="source"),
                    DraftConnect(
                        op="connect",
                        stream=stream_id,
                        end="source",
                        unit=upstream["id"],
                        port=(outlet.get("source") or {}).get("port", 0),
                    ),
                    DraftConnect(op="connect", stream=stream_id, end="target", unit=new_unit_id, port=0),
                    DraftConnect(op="connect", stream=outlet["id"], end="source", unit=new_unit_id, port=0),
                ]
            )
            if target_end:
                ops.append(
                    DraftConnect(
                        op="connect",
                        stream=outlet["id"],
                        end="target",
                        unit=target_end["unit"],
                        port=target_end["port"],
                    )
                )
            changes.append(
                ChangeLine(
                    label=f"{new_tag} — inserted after {upstream['tag']}",
                    after=f"{stream_tag} → {new_tag} → {outlet['tag']}",
                )
            )
        elif action.op == "connect":
            source = _target(document, action.source, "unit")
            target = _target(document, action.to, "unit")
            source_port = _port_index(source["type"], action.source_port, "outlet")
            target_port = _port_index(target["type"], action.to_port, "inlet")
            tag = _next_tag(objects, "S_auto", ops)
            stream_id = _next_id(objects, "s", ops)
            ops.extend(
                [
                    AddStream(
                        op="add_stream",
                        id=stream_id,
                        tag=tag,
                        x=(source["x"] + target["x"]) // 2,
                        y=(source["y"] + target["y"]) // 2,
                    ),
                    DraftConnect(op="connect", stream=stream_id, end="source", unit=source["id"], port=source_port),
                    DraftConnect(op="connect", stream=stream_id, end="target", unit=target["id"], port=target_port),
                ]
            )
            changes.append(ChangeLine(label=f"{tag} — connection", after=f"{source['tag']} → {target['tag']}"))
        elif action.op == "disconnect":
            stream = _target(document, action.stream, "stream")
            for end in ("source", "target"):
                if stream.get(end):
                    ops.append(Disconnect(op="disconnect", stream=stream["id"], end=end))
            changes.append(ChangeLine(label=f"{stream['tag']} connection", after="disconnected"))
        elif action.op == "mirror":
            target = _target(document, action.target, "unit")
            ops.append(
                SetOrientation(
                    op="set_orientation",
                    id=target["id"],
                    flip_y=action.axis == "horizontal",
                    flip_x=action.axis == "vertical",
                )
            )
            changes.append(ChangeLine(label=f"{target['tag']} orientation", after=f"mirrored {action.axis}"))
        elif action.op == "move":
            target = _target(document, action.target)
            ops.append(
                Move(
                    op="move",
                    id=target["id"],
                    x=max(-5000, min(5000, int(target["x"] + action.dx))),
                    y=max(-5000, min(5000, int(target["y"] + action.dy))),
                )
            )
            changes.append(
                ChangeLine(
                    label=f"{target['tag']} position",
                    after=f"({int(target['x'] + action.dx)}, {int(target['y'] + action.dy)})",
                )
            )
        elif action.op == "rename":
            target = _target(document, action.target)
            ops.append(Rename(op="rename", id=target["id"], tag=action.new_tag))
            changes.append(ChangeLine(label=f"{target['tag']} tag", before=target["tag"], after=action.new_tag))
        elif action.op == "delete":
            target = _target(document, action.target)
            ops.append(Delete(op="delete", id=target["id"]))
            changes.append(
                ChangeLine(label=f"{target['tag']} — delete {target['type']}", before="present", after="deleted")
            )
    for stream_id, culture in culture_updates.items():
        if "biomass" not in culture or "salinity" not in culture:
            raise ValueError("Creating or keeping a culture section requires biomass and salinity in the same action request.")
        ops.append(SetStreamCulture(op="set_stream_culture", stream=stream_id, culture=culture))
    if not ops:
        raise ValueError("No supported Process operations were supplied.")
    return ops, changes


def _display(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict) and "value" in value and "unit" in value:
        return f"{value['value']} {value['unit']}"
    return str(value)


def _actor(origin: ActionOrigin) -> str:
    if origin.kind == "relay":
        return f"agent:relay:{origin.relay_run_id}"
    return f"agent:local:{origin.thread_id}/{origin.interaction_id}"


def apply(workspace_id: str, action_id: str) -> ActionOutcome:
    row = _row(workspace_id, action_id)
    outcome = ActionOutcome.model_validate_json(row["outcome_json"])
    if outcome.state != "proposed":
        raise ActionError("action_not_proposed", "Only proposed actions can be applied.", 409)
    # Claim the proposal before any owner mutation. The outcome JSON remains proposed
    # during execution; the durable state column prevents another request from racing.
    with open_sqlite_connection() as connection:
        claimed = connection.execute(
            "UPDATE workspace_actions SET state='applying' WHERE id=? AND workspace_id=? AND state='proposed'",
            (action_id, workspace_id),
        ).rowcount == 1
        connection.commit()
    if not claimed:
        raise ActionError("action_not_proposed", "Action is not proposed or is already being applied.", 409)
    request = ActionRequest.model_validate_json(row["request_json"])
    origin = ActionOrigin.model_validate_json(row["origin_json"])
    if outcome.surface == "studio":
        from app.modules.studio import service as studio

        action = request.actions[0]
        assert isinstance(action, StudioDraftAction)
        try:
            result = studio.apply_proposal(
                workspace_id, model_spec_id=request.draft_id, base_revision=request.base_revision,
                category=action.category, name=action.name, engineering_question=action.engineering_question,
                contract=action.contract, source=action.source, assumptions=action.assumptions,
                actor=_actor(origin), request_digest=outcome.request_digest)
            outcome.state, outcome.draft_id = "applied", result["id"]
            outcome.result_revision = studio.proposal_revision(result)
            outcome.undo_available = False
        except studio.StudioError as exc:
            outcome.state = "stale" if exc.code == "studio_proposal_stale" else "refused"
            outcome.reason_code, outcome.reason = exc.code, exc.message
        except (ValidationError, ValueError):
            outcome.state, outcome.reason_code, outcome.reason = (
                "refused", "studio_proposal_invalid", "Studio proposal did not pass owner validation.")
        except Exception as exc:  # never leave the claim stuck in 'applying'
            outcome.state, outcome.reason_code, outcome.reason = "refused", "apply_failed", str(exc)[:500]
        _save_outcome(outcome)
        return outcome
    if outcome.surface == "bluecad":
        try:
            candidate = get_candidate(workspace_id, request.base_revision)
            if candidate is None or candidate.status != "valid":
                outcome.state, outcome.reason_code, outcome.reason = (
                    "stale",
                    "stale",
                    "Base candidate is no longer valid.",
                )
                _save_outcome(outcome)
                return outcome
            from app.modules.workspace_actions.bluecad_actions import execute_bluecad

            outcome.child_candidate_id = execute_bluecad(workspace_id, request, origin, outcome.request_digest)
            outcome.state, outcome.undo_available = "applied", True
        except Exception as exc:
            outcome.state, outcome.reason_code, outcome.reason = "refused", "build_failed", str(exc)
        _save_outcome(outcome)
        return outcome
    if request.actions[0].op == "set_cfd_study_template":
        _apply_study_template(workspace_id, outcome, request, origin)
        _save_outcome(outcome)
        return outcome
    found = _find_draft(workspace_id, revision=request.base_revision, draft_id=request.draft_id)
    if not found:
        outcome.state, outcome.reason_code, outcome.reason = "stale", "stale", "Process draft head has moved."
    else:
        draft_id, record = found
        try:
            pins: list[tuple[str, UnitHydraulicsPin, dict]] = []
            ops, derived = _process_ops(record["document"], request, workspace_id, draft_id, pins)
            proposed = {line.label: line.after for line in outcome.changes
                        if line.label.endswith(" biological model card")}
            moved = [line.label for line in derived
                     if line.label in proposed and line.after != proposed[line.label]]
            if moved:
                # A card revision moved between proposal and Apply: never pin a different one silently.
                outcome.state, outcome.reason_code = "stale", "stale"
                outcome.reason = "The model card changed after this proposal; ask again to pin its current revision."
                _save_outcome(outcome)
                return outcome
            updated = draft.patch(
                workspace_id,
                draft_id,
                request.base_revision,
                ops,
                actor=_actor(origin),
                provenance={"action_request_digest": outcome.request_digest},
            )
            outcome.state, outcome.result_revision, outcome.draft_id = "applied", updated["revision"], draft_id
            outcome.undo_available = True
            if pins:  # Design component history; no sqlite write transaction was held across the draft patch
                from app.modules.workspaces.design_components import record_process_hydraulics_pin

                for unit_id, pin, provenance in pins:
                    try:
                        record_process_hydraulics_pin(provenance, draft_id=draft_id, unit_id=unit_id, pin=pin,
                                                      draft_revision=updated["revision"], actor=_actor(origin))
                    except Exception:  # noqa: BLE001 - the draft change is already durable; history is best-effort
                        logging.getLogger(__name__).exception("Design hydraulics pin history event failed")
        except draft.DraftError as exc:
            outcome.state, outcome.reason_code, outcome.reason = "stale", exc.code, str(exc)
        except Exception as exc:
            outcome.state, outcome.reason_code, outcome.reason = "refused", "apply_failed", str(exc)[:500]
    _save_outcome(outcome)
    return outcome


def dismiss(workspace_id: str, action_id: str) -> ActionOutcome:
    row = _row(workspace_id, action_id)
    outcome = ActionOutcome.model_validate_json(row["outcome_json"])
    with open_sqlite_connection() as connection:
        claimed = connection.execute(
            "UPDATE workspace_actions SET state='dismissing' WHERE id=? AND workspace_id=? AND state='proposed'",
            (action_id, workspace_id),
        ).rowcount == 1
        connection.commit()
    if not claimed or outcome.state != "proposed":
        raise ActionError("action_not_proposed", "Only proposed actions can be dismissed.", 409)
    outcome.state = "dismissed"
    outcome.updated_at = _now()
    _save_outcome(outcome)
    return outcome


def undo(workspace_id: str, action_id: str) -> ActionOutcome:
    row = _row(workspace_id, action_id)
    outcome = ActionOutcome.model_validate_json(row["outcome_json"])
    if outcome.state != "applied" or not outcome.undo_available:
        raise ActionError("undo_unavailable", "This action has no available undo.", 409)
    with open_sqlite_connection() as connection:
        claimed = connection.execute(
            "UPDATE workspace_actions SET state='undoing' WHERE id=? AND workspace_id=? AND state='applied'",
            (action_id, workspace_id),
        ).rowcount == 1
        connection.commit()
    if not claimed:
        raise ActionError("undo_unavailable", "This action is already being undone.", 409)
    if outcome.surface == "bluecad":
        from app.modules.bluecad.ledger import archive_candidate

        try:
            archive_candidate(workspace_id, outcome.child_candidate_id or "")
        except ValueError as exc:
            _save_outcome(outcome)
            raise ActionError("undo_conflict", str(exc), 409) from exc
        except Exception as exc:
            _save_outcome(outcome)
            raise ActionError("undo_failed", str(exc)[:500], 409) from exc
    else:
        if not outcome.result_revision or not outcome.draft_id:
            _save_outcome(outcome)
            raise ActionError("undo_unavailable", "Action revision details are unavailable.", 409)
        try:
            head = draft.projection(workspace_id, outcome.draft_id)["revision"]
            if head != outcome.result_revision:
                _save_outcome(outcome)
                raise ActionError("stale", "Draft head moved after this action; undo is no longer safe.", 409)
            draft.restore(workspace_id, outcome.draft_id, head, outcome.base_revision)
        except ActionError:
            raise
        except Exception as exc:
            _save_outcome(outcome)
            raise ActionError("undo_failed", str(exc)[:500], 409) from exc
    outcome.state, outcome.undo_available = "undone", False
    outcome.updated_at = _now()
    _save_outcome(outcome)
    return outcome


def _row(workspace_id: str, action_id: str):
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT * FROM workspace_actions WHERE workspace_id=? AND id=?", (workspace_id, action_id)
        ).fetchone()
    if row is None:
        raise ActionError("action_not_found", "Workspace action was not found.", 404)
    return row


def get(workspace_id: str, action_id: str) -> ActionOutcome:
    return ActionOutcome.model_validate_json(_row(workspace_id, action_id)["outcome_json"])


def _save_outcome(outcome: ActionOutcome) -> None:
    outcome.updated_at = _now()
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE workspace_actions SET state=?,outcome_json=?,updated_at=? WHERE id=? AND workspace_id=?",
            (outcome.state, outcome.model_dump_json(), outcome.updated_at, outcome.action_id, outcome.workspace_id),
        )
        connection.commit()


def list_for(
    workspace_id: str, *, thread_id: str, interaction_id: str | None = None, relay_run_id: str | None = None
) -> list[ActionOutcome]:
    clauses, args = ["workspace_id=?", "thread_id=?"], [workspace_id, thread_id]
    if interaction_id:
        clauses.append("interaction_id=?")
        args.append(interaction_id)
    if relay_run_id:
        clauses.append("relay_run_id=?")
        args.append(relay_run_id)
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT outcome_json FROM workspace_actions WHERE " + " AND ".join(clauses) + " ORDER BY created_at", args
        ).fetchall()
    return [ActionOutcome.model_validate_json(row["outcome_json"]) for row in rows]
