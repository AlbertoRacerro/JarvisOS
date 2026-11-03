"""Draft authoring and immutable publication for Studio calculations."""

import ast
import hashlib
import json
from math import isfinite
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app.core.database import open_sqlite_connection
from app.modules.events.service import log_event, utc_now
from app.modules.modeling.models import ModelSpecCreate, ModelSpecProjectUpdate
from app.modules.modeling.project_knowledge_owner import (
    create_model_spec_in_transaction,
    update_model_spec_in_transaction,
)
from app.modules.modeling.service import create_model_spec
from app.modules.process_kernel.units import parse_unit_token, resolve_unit, unit_registry
from app.modules.runner.input_contracts import canonicalize_input_contract
from app.modules.runner.safety import model_implementation_root
from app.modules.studio import geometry, procedure, procedure_service
from app.modules.studio.derive import derive_documentation
from app.modules.studio.geometry import GEOMETRY_KIND, GeometryCreate, GeometryTestRequest
from app.modules.studio.models import StudioCreate, StudioError, StudioUpdate, TestRequest, model_payload
from app.modules.studio.procedure import ProcedureCreate, ProcedureTestRequest
from app.modules.studio.unit_checks import UnitCheckError, check_result, classification_code, host_stream
from app.modules.studio.unit_models import (
    StudioUnitCreate,
    UnitTestRequest,
    unit_contract,
    unit_scaffold,
)
from app.modules.studio_runtime.runtime import RunRequest, run, status

UNIT_KIND = "studio_unit_v1"


def proposal_revision(model: dict) -> str:
    """Bind a proposal to the selected Studio draft, including metadata-only edits."""
    # Title and question live on the canonical model spec, which other owners may edit.
    return "sha256:" + _sha(_json({key: model[key] for key in
                               ("id", "name", "engineering_question", "source_sha256", "contract_sha256",
                                "updated_at")}))


def validate_proposed_source(category: str, name: str, question: str, contract: dict,
                             source: str) -> dict:
    """Validate inert proposal data against the existing category contracts."""
    if not isinstance(contract, dict) or {"name", "engineering_question", "category"} & set(contract):
        # Identity and category come from the typed action, never from the free-form contract.
        raise StudioError("studio_category_mismatch", "The proposed contract may not restate name or category.")
    if category == "calculation":
        validated = StudioCreate.model_validate({"name": name, "engineering_question": question, **contract})
        normalized, _ = _contract(validated)
        _input_contract(normalized)
    elif category == "geometry":
        shape = GeometryCreate.model_validate({"category": category, "name": name,
                                               "engineering_question": question, **contract})
        normalized = geometry.geometry_contract(shape)
        _input_contract(normalized)
    elif category == "process_unit":
        unit = StudioUnitCreate.model_validate({"category": category, "name": name,
                                                "engineering_question": question, **contract})
        normalized = unit_contract(unit)
    else:
        raise StudioError("studio_category_mismatch", "Only executable Studio categories may be proposed.")
    if len(source) > 65536 or "\x00" in source:
        raise StudioError("studio_source_invalid", "Proposed Python source is invalid or too large.")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise StudioError("studio_source_invalid", "Proposed Python source has invalid syntax.") from exc
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
               for node in tree.body):
        raise StudioError("studio_source_invalid", "Proposed Python source must define main.")
    return normalized


def apply_proposal(workspace_id: str, *, model_spec_id: str | None, base_revision: str,
                   category: str, name: str, engineering_question: str, contract: dict,
                   source: str, assumptions: str, actor: str, request_digest: str) -> dict:
    """One explicit, transactional Studio draft mutation; never runs model code."""
    normalized = validate_proposed_source(category, name, engineering_question, contract, source)
    contract_json, source_sha = _json(normalized), _sha(source)
    contract_sha, now = _sha(contract_json), utc_now()
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_workspace(connection, workspace_id)
        if model_spec_id is None:
            if base_revision != "new":
                raise StudioError("studio_proposal_stale", "Create proposal is stale.", status=409)
            model_spec_id = create_model_spec_in_transaction(
                connection, workspace_id,
                ModelSpecCreate(title=name, engineering_question=engineering_question,
                                assumptions_summary=assumptions or None),
                actor=actor, event_payload={"action_request_digest": request_digest})["id"]
            connection.execute("""INSERT INTO studio_drafts
                (model_spec_id,workspace_id,contract_json,source,source_sha256,contract_sha256,
                 assumption_notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (model_spec_id, workspace_id, contract_json, source, source_sha, contract_sha,
                 assumptions, now, now))
        else:
            try:
                row = _draft_row(connection, workspace_id, model_spec_id)
            except StudioError as exc:
                raise StudioError("studio_proposal_stale", "Selected draft is unavailable.", status=409) from exc
            current = _read(connection, workspace_id, model_spec_id)
            if (row["model_status"] != "draft" or proposal_revision(current) != base_revision
                    or _category(json.loads(row["contract_json"])) != category):
                raise StudioError("studio_proposal_stale", "Selected draft changed; request a fresh proposal.", status=409)
            spec_row = connection.execute("SELECT updated_at FROM model_specs WHERE id=? AND workspace_id=?",
                                          (model_spec_id, workspace_id)).fetchone()
            update_model_spec_in_transaction(connection, model_spec_id, ModelSpecProjectUpdate(
                workspace_id=workspace_id, expected_updated_at=spec_row["updated_at"], title=name,
                engineering_question=engineering_question))
            changed = source_sha != row["source_sha256"] or contract_sha != row["contract_sha256"]
            connection.execute("""UPDATE studio_drafts SET contract_json=?,source=?,source_sha256=?,
                contract_sha256=?,assumption_notes=?,test_report_json=?,updated_at=?
                WHERE model_spec_id=? AND workspace_id=?""",
                (contract_json, source, source_sha, contract_sha, assumptions,
                 None if changed else row["test_report_json"], now, model_spec_id, workspace_id))
            log_event(connection, event_type="StudioDraftUpdated", actor=actor, target_type="ModelSpec",
                      target_id=model_spec_id, workspace_id=workspace_id,
                      payload={"action_request_digest": request_digest})
        connection.commit()
        return _read(connection, workspace_id, model_spec_id)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scaffold(contract: dict) -> str:
    """Contract-only deterministic source; user edits the placeholder."""
    names = [item["name"] for item in contract["inputs"]]
    signature = ", ".join(f"{name}: float" for name in names)
    arguments = ", ".join(f"{name}=inputs[{name!r}]" for name in names)
    lines = ["# Studio calculation. Magnitudes use the declared contract units.",
             f"def evaluate({signature}) -> dict[str, float]:",
             '    """Return every declared output from the declared inputs.', "", "    Inputs:",
             *(f"        {item['name']} [{item['unit']}]" for item in contract["inputs"]), "    Outputs:",
             *(f"        {item['name']} [{item['unit']}]" for item in contract["outputs"]), '    """',
             '    raise NotImplementedError("Complete evaluate before testing")', "",
             "", "def main(inputs: dict[str, float]) -> dict[str, float]:",
             f"    return evaluate({arguments})", ""]
    return "\n".join(lines)


def _contract(data: StudioCreate) -> tuple[dict, str]:
    contract = model_payload(data)
    return contract, _sha(_json(contract))


def _input_contract(contract: dict) -> tuple[str, str]:
    variables = []
    for item in contract["inputs"]:
        try:
            resolved = resolve_unit(item["unit"])
            physical_dimension = resolved.physical_dimension
            semantic_basis = resolved.semantic_basis
        except Exception:
            # Studio's runner path understands Pint's general dimensionality string.
            physical_dimension = str(unit_registry().Unit(parse_unit_token(item["unit"])).dimensionality)
            semantic_basis = None
        domain = {name: item[name] for name in ("min", "max") if name in item}
        variable = {"name": item["name"], "label": item["label"], "unit": item["unit"],
                    "required": True, "category": "model_parameter", "description": item["description"],
                    "physical_dimension": physical_dimension}
        if semantic_basis:
            variable["semantic_basis"] = semantic_basis
        if domain:
            variable["domain"] = domain
        variables.append(variable)
    payload, digest, _ = canonicalize_input_contract(
        {"schema_version": 2, "evaluation_mode": "forward", "variables": variables})
    return payload, digest


def _category(contract: dict) -> str:
    return contract.get("category", "calculation")


def _artifact_name(category: str) -> str:
    return {"process_unit": "studio_unit.py", "geometry": "studio_geometry.py",
            "procedure": procedure_service.ARTIFACT_NAME}.get(category, "studio_calc.py")


_KINDS = {"process_unit": UNIT_KIND, "geometry": GEOMETRY_KIND,
          "procedure": procedure.PROCEDURE_KIND, "calculation": "studio_calc_v1"}
_CATEGORY_OF_KIND = {kind: category for category, kind in _KINDS.items()}


def category_of_kind(implementation_kind: str) -> str:
    return _CATEGORY_OF_KIND.get(implementation_kind, "calculation")


def _require_workspace(connection, workspace_id: str) -> None:
    if connection.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is None:
        raise StudioError("studio_workspace_not_found", "Workspace not found.")


def _draft_row(connection, workspace_id: str, model_spec_id: str):
    row = connection.execute("""SELECT d.*, m.title, m.engineering_question, m.status AS model_status FROM studio_drafts d
        JOIN model_specs m ON m.id = d.model_spec_id
        WHERE d.workspace_id = ? AND d.model_spec_id = ?""", (workspace_id, model_spec_id)).fetchone()
    if row is None:
        raise StudioError("studio_model_not_found", "Studio model not found.")
    return row


def _versions(connection, model_spec_id: str) -> list[dict]:
    rows = connection.execute("""SELECT mv.id, mv.version_label, sp.source_sha256, sp.contract_sha256, mv.created_at
        FROM studio_published_versions sp JOIN model_versions mv ON mv.id = sp.version_id
        WHERE sp.model_spec_id = ? ORDER BY sp.rowid""", (model_spec_id,)).fetchall()
    return [dict(row) for row in rows]


def _read(connection, workspace_id: str, model_spec_id: str) -> dict:
    row = _draft_row(connection, workspace_id, model_spec_id)
    contract = json.loads(row["contract_json"])
    report = json.loads(row["test_report_json"]) if row["test_report_json"] else None
    return {"id": row["model_spec_id"], "workspace_id": workspace_id, "name": row["title"],
            "engineering_question": row["engineering_question"], "category": _category(contract),
            **contract, "source": row["source"], "source_sha256": row["source_sha256"],
            "contract_sha256": row["contract_sha256"], "annotations": row["annotations"],
            "assumptions": row["assumption_notes"],
            "annotations_stale": bool(row["annotations"] and row["annotations_source_sha256"] != row["source_sha256"]),
            "status": row["model_status"],
            "documentation": ({"derived": None, **contract} if _category(contract) == "procedure"
                              else {"derived": derive_documentation(
                                  row["source"], entry="build" if _category(contract) == "geometry" else "evaluate"
                              ), **contract}),
            **({"steps": procedure.steps_of(row["source"]), "notice": procedure.NOTICE}
               if _category(contract) == "procedure" else {}),
            "test_cases": json.loads(row["test_cases_json"]), "last_test_report": report,
            "versions": _versions(connection, model_spec_id), "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def _procedure_parts(data: ProcedureCreate) -> tuple[dict, str]:
    procedure.validate_steps(data.steps, data.parameters)
    return procedure.procedure_contract(data.parameters), procedure.procedure_source(data.steps)


def create(workspace_id: str, data: StudioCreate | StudioUnitCreate | GeometryCreate | ProcedureCreate) -> dict:
    if isinstance(data, ProcedureCreate):
        contract, source = _procedure_parts(data)
        contract_sha = _sha(_json(contract))
    elif isinstance(data, StudioUnitCreate):
        contract = unit_contract(data)
        contract_sha, source = _sha(_json(contract)), unit_scaffold(contract)
    elif isinstance(data, GeometryCreate):
        contract = geometry.geometry_contract(data)
        contract_sha, source = _sha(_json(contract)), geometry.geometry_scaffold(contract)
        _input_contract(contract)
    else:
        contract, contract_sha = _contract(data)
        _input_contract(contract)
        source = scaffold(contract)
    now = utc_now()
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
    model_id = create_model_spec(workspace_id, ModelSpecCreate(
        title=data.name, engineering_question=data.engineering_question)).id
    with open_sqlite_connection() as connection:
        connection.execute("""INSERT INTO studio_drafts
            (model_spec_id, workspace_id, contract_json, source, source_sha256, contract_sha256, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (model_id, workspace_id, _json(contract), source, _sha(source), contract_sha, now, now))
        connection.commit()
        return _read(connection, workspace_id, model_id)


def get(workspace_id: str, model_spec_id: str) -> dict:
    with open_sqlite_connection() as connection:
        return _read(connection, workspace_id, model_spec_id)


def list_models(workspace_id: str) -> list[dict]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        ids = [row["model_spec_id"] for row in connection.execute(
            "SELECT model_spec_id FROM studio_drafts WHERE workspace_id = ? ORDER BY updated_at DESC", (workspace_id,))]
        return [_read(connection, workspace_id, model_id) for model_id in ids]


def update(workspace_id: str, model_spec_id: str, data: StudioUpdate) -> dict:
    with open_sqlite_connection() as connection:
        row = _draft_row(connection, workspace_id, model_spec_id)
        if row["model_status"] != "draft":
            raise StudioError("studio_revision_required", "Revise the published model before editing.")
        old_contract = json.loads(row["contract_json"])
        name = data.name or row["title"]
        question = data.engineering_question or row["engineering_question"]
        unit_fields = (data.inlets, data.outlets, data.parameters, data.culture_generation)
        symbol_supplied = "symbol" in data.model_fields_set
        if _category(old_contract) == "procedure":
            if any(item is not None for item in (data.inputs, data.outputs, data.inlets, data.outlets,
                                                  data.culture_generation, data.source)) or symbol_supplied:
                raise StudioError("studio_category_mismatch", "A procedure declares parameters and steps.")
            validated_procedure = ProcedureCreate.model_validate({
                "category": "procedure", "name": name, "engineering_question": question,
                "parameters": data.parameters if data.parameters is not None else old_contract["parameters"],
                "steps": data.steps if data.steps is not None else procedure.steps_of(row["source"])})
            contract, new_source = _procedure_parts(validated_procedure)
            contract_sha = _sha(_json(contract))
        elif _category(old_contract) == "process_unit":
            if data.inputs is not None or data.outputs is not None:
                raise StudioError("studio_category_mismatch", "A process unit declares inlets, outlets and parameters.")
            validated_unit = StudioUnitCreate.model_validate({
                "category": "process_unit", "name": name, "engineering_question": question,
                "inlets": data.inlets if data.inlets is not None else old_contract["inlets"],
                "outlets": data.outlets if data.outlets is not None else old_contract["outlets"],
                "parameters": data.parameters if data.parameters is not None else old_contract["parameters"],
                "symbol": data.symbol if symbol_supplied else old_contract["symbol"],
                "culture_generation": (data.culture_generation if data.culture_generation is not None
                                       else old_contract["culture_generation"])})
            contract = unit_contract(validated_unit)
            contract_sha = _sha(_json(contract))
        elif _category(old_contract) == "geometry":
            if data.outputs is not None or any(item is not None for item in unit_fields) or symbol_supplied:
                raise StudioError("studio_category_mismatch", "A geometry model declares inputs only.")
            contract = geometry.geometry_contract(GeometryCreate.model_validate({
                "category": "geometry", "name": name, "engineering_question": question,
                "inputs": data.inputs if data.inputs is not None else old_contract["inputs"]}))
            contract_sha = _sha(_json(contract))
            _input_contract(contract)
        else:
            if any(item is not None for item in unit_fields) or symbol_supplied:
                raise StudioError("studio_category_mismatch", "A calculation declares inputs and outputs.")
            validated_calc = StudioCreate.model_validate({
                "name": name, "engineering_question": question,
                "inputs": data.inputs if data.inputs is not None else old_contract["inputs"],
                "outputs": data.outputs if data.outputs is not None else old_contract["outputs"],
            })
            contract, contract_sha = _contract(validated_calc)
            _input_contract(contract)
        source = (new_source if _category(old_contract) == "procedure"
                  else data.source if data.source is not None else row["source"])
        annotations = data.annotations if data.annotations is not None else row["annotations"]
        assumptions = data.assumptions if data.assumptions is not None else row["assumption_notes"]
        annotation_hash = _sha(source) if data.annotations is not None else row["annotations_source_sha256"]
        changed = _sha(source) != row["source_sha256"] or contract_sha != row["contract_sha256"]
        now = utc_now()
        spec_row = connection.execute("SELECT updated_at FROM model_specs WHERE id = ?", (model_spec_id,)).fetchone()
        update_model_spec_in_transaction(connection, model_spec_id, ModelSpecProjectUpdate(
            workspace_id=workspace_id, expected_updated_at=spec_row["updated_at"],
            title=name, engineering_question=question))
        connection.execute("""UPDATE studio_drafts SET contract_json = ?, source = ?, source_sha256 = ?,
            contract_sha256 = ?, annotations = ?, assumption_notes = ?, annotations_source_sha256 = ?,
            test_report_json = ?, updated_at = ? WHERE model_spec_id = ?""",
            (_json(contract), source, _sha(source), contract_sha, annotations, assumptions, annotation_hash,
             None if changed else row["test_report_json"], now, model_spec_id))
        connection.commit()
        return _read(connection, workspace_id, model_spec_id)


def _unit_case(source: str, contract: dict, case) -> dict:
    errors: list[str] = []
    inlets = {name: stream.model_dump() for name, stream in case.inlets.items()}
    params = case.params
    if set(inlets) != {item["name"] for item in contract["inlets"]}:
        errors.append("Inlet names do not match the contract.")
    if set(params) != {item["name"] for item in contract["parameters"]}:
        errors.append("Parameter names do not match the contract.")
    for item in contract["parameters"] if not errors else []:
        value = params[item["name"]]
        if item.get("min") is not None and value < item["min"] or item.get("max") is not None and value > item["max"]:
            errors.append(f"Parameter {item['name']} is outside its bounds.")
    outlet_names = [item["name"] for item in contract["outlets"]]
    if case.expected_outlets is not None and not set(case.expected_outlets) <= set(outlet_names):
        errors.append("Expected outlet names do not match the contract.")
    codes: list[str] = []
    outcome = None
    classification = "invalid_case"
    if not errors:
        compounds = sorted({name for stream in inlets.values() for name in stream["mass_fractions"]})
        record = run(RunRequest(source=source, inputs={"inlets": {name: host_stream(stream) for name, stream in inlets.items()},
                                                       "params": params}))
        classification = record.classification
        if classification != "ok":
            codes.append(classification_code(classification))
            errors.append(f"{codes[-1]}: runtime {classification}: {record.stderr.strip() or status()['reason'] or 'execution failed'}")
        else:
            try:
                outcome = check_result(record.result, inlets=inlets, outlet_names=outlet_names, compounds=compounds,
                                       generation=contract["culture_generation"])
            except UnitCheckError as exc:
                codes.append(exc.code)
                errors.append(f"{exc.code}: {exc.message}")
    if outcome is not None and case.expected_outlets is not None:
        for port, expected in case.expected_outlets.items():
            actual = outcome.outlets[port]
            for key, value in expected.model_dump(exclude_none=True).items():
                pairs = ([(f"{key}.{name}", actual[key].get(name), fraction) for name, fraction in value.items()]
                         if key == "mass_fractions" else [(key, actual[key], value)])
                for label, found, wanted in pairs:
                    if found is None or abs(found - wanted) > case.tolerance:
                        errors.append(f"Outlet {port} {label} differs from expected by more than tolerance.")
    return {"passed": not errors, "classification": classification, "codes": codes,
            "outputs": outcome.outlets if outcome else None,
            "culture_generation": outcome.culture_generation if outcome else None, "errors": errors}


def test(workspace_id: str, model_spec_id: str,
         data: TestRequest | UnitTestRequest | GeometryTestRequest | ProcedureTestRequest) -> dict:
    with open_sqlite_connection() as connection:
        row = _draft_row(connection, workspace_id, model_spec_id)
        if row["model_status"] != "draft":
            raise StudioError("studio_revision_required", "Revise the published model before testing.")
        source, contract = row["source"], json.loads(row["contract_json"])
        # A bare {"inputs": ...} case fits both request shapes; the stored category decides.
        wanted = GeometryTestRequest if _category(contract) == "geometry" else TestRequest
        if isinstance(data, (TestRequest, GeometryTestRequest)) and not isinstance(data, wanted):
            try:
                data = wanted.model_validate(data.model_dump(exclude_unset=True))
            except ValidationError as exc:
                raise StudioError("studio_category_mismatch", "The test cases do not match the model category.") from exc
        category = _category(contract)
        if (category == "geometry") != isinstance(data, GeometryTestRequest):
            raise StudioError("studio_category_mismatch", "The test cases do not match the model category.")
        if isinstance(data, GeometryTestRequest):
            cases = geometry.run_cases(workspace_id, model_spec_id, source, contract, data.cases)
            report = {"source_sha256": row["source_sha256"], "contract_sha256": row["contract_sha256"],
                      "passed": all(case["passed"] for case in cases), "cases": cases}
            connection.execute("""UPDATE studio_drafts SET test_cases_json = ?, test_report_json = ?, updated_at = ?
                WHERE model_spec_id = ?""", (_json([case.model_dump() for case in data.cases]), _json(report),
                                             utc_now(), model_spec_id))
            connection.commit()
            return report
        if ((category == "process_unit") != isinstance(data, UnitTestRequest)
                or (category == "procedure") != isinstance(data, ProcedureTestRequest)):
            raise StudioError("studio_category_mismatch", "The test cases do not match the model category.")
        if isinstance(data, ProcedureTestRequest):
            report = procedure_service.run_tests(workspace_id, row, data)
            connection.execute("""UPDATE studio_drafts SET test_cases_json = ?, test_report_json = ?, updated_at = ?
                WHERE model_spec_id = ?""", (_json([case.model_dump() for case in data.cases]), _json(report),
                                             utc_now(), model_spec_id))
            connection.commit()
            return report
        if isinstance(data, UnitTestRequest):
            cases = [_unit_case(source, contract, case) for case in data.cases]
            report = {"source_sha256": row["source_sha256"], "contract_sha256": row["contract_sha256"],
                      "passed": all(case["passed"] for case in cases), "cases": cases}
            connection.execute("""UPDATE studio_drafts SET test_cases_json = ?, test_report_json = ?, updated_at = ?
                WHERE model_spec_id = ?""", (_json([case.model_dump() for case in data.cases]), _json(report),
                                             utc_now(), model_spec_id))
            connection.commit()
            return report
        input_names = {item["name"] for item in contract["inputs"]}
        output_names = {item["name"] for item in contract["outputs"]}
        cases = []
        for case in data.cases:
            errors = []
            if set(case.inputs) != input_names:
                errors.append("Input names do not match the contract.")
            if case.expected_outputs is not None and not set(case.expected_outputs).issubset(output_names):
                errors.append("Expected output names do not match the contract.")
            if not errors:
                for item in contract["inputs"]:
                    value = case.inputs[item["name"]]
                    if item.get("min") is not None and value < item["min"] or item.get("max") is not None and value > item["max"]:
                        errors.append(f"Input {item['name']} is outside its bounds.")
            result = run(RunRequest(source=source, inputs=case.inputs)) if not errors else None
            outputs = result.result if result and result.classification == "ok" else None
            if result and result.classification != "ok":
                errors.append(f"Runtime {result.classification}: {result.stderr.strip() or status()['reason'] or 'execution failed'}")
            if outputs is not None:
                if not isinstance(outputs, dict) or set(outputs) != output_names or any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
                    for value in outputs.values()
                ):
                    errors.append("Every declared output must be present and finite numeric data.")
                elif case.expected_outputs is not None:
                    for name, expected in case.expected_outputs.items():
                        if abs(outputs[name] - expected) > case.tolerance:
                            errors.append(f"Output {name} differs from expected by more than tolerance.")
            cases.append({"passed": not errors, "classification": result.classification if result else "invalid_case",
                          "outputs": outputs, "errors": errors})
        report = {"source_sha256": row["source_sha256"], "contract_sha256": row["contract_sha256"],
                  "passed": all(case["passed"] for case in cases), "cases": cases}
        connection.execute("""UPDATE studio_drafts SET test_cases_json = ?, test_report_json = ?, updated_at = ?
            WHERE model_spec_id = ?""", (_json([case.model_dump() for case in data.cases]), _json(report), utc_now(), model_spec_id))
        connection.commit()
        return report


def publish(workspace_id: str, model_spec_id: str) -> dict:
    with open_sqlite_connection() as connection:
        row = _draft_row(connection, workspace_id, model_spec_id)
        if row["model_status"] != "draft":
            raise StudioError("studio_revision_required", "Revise the published model before publishing again.")
        report = json.loads(row["test_report_json"]) if row["test_report_json"] else None
        if not report or not report["passed"] or not report["cases"] or report["source_sha256"] != row["source_sha256"] or report["contract_sha256"] != row["contract_sha256"]:
            raise StudioError("studio_publish_report_required", "A passing test report for the current source and contract is required.")
        contract = json.loads(row["contract_json"])
        category = _category(contract)
        input_payload, input_sha = (_input_contract(contract) if category in {"calculation", "geometry"}
                                    else (None, None))
        next_number = len(_versions(connection, model_spec_id)) + 1
        version_id, artifact_id, now = str(uuid4()), str(uuid4()), utc_now()
        version_label = f"{row['title']} v{next_number}"  # the calculation surface lists versions by label alone
        directory = model_implementation_root(workspace_id) / version_id
        directory.mkdir(parents=True, exist_ok=False)
        artifact_path = directory / _artifact_name(category)
        try:
            artifact_path.write_bytes(row["source"].encode("utf-8"))
            connection.execute("""INSERT INTO artifacts (id, workspace_id, filename, stored_path, artifact_type,
                mime_type, sha256, source_ref, status, created_at) VALUES (?, ?, ?, ?, ?,
                ?, ?, ?, 'registered', ?)""",
                (artifact_id, workspace_id, artifact_path.name, str(artifact_path),
                 "studio_procedure" if category == "procedure" else "python_script",
                 "application/json" if category == "procedure" else "text/x-python", row["source_sha256"],
                 f"model_spec:{model_spec_id}", now))
            connection.execute("""INSERT INTO model_versions (id, workspace_id, model_spec_id, version_label,
                implementation_artifact_id, implementation_kind, status, changelog, input_contract_payload,
                input_contract_sha256, created_at, notes) VALUES (?, ?, ?, ?, ?, ?, 'published',
                ?, ?, ?, ?, 'Scientifically unqualified')""",
                (version_id, workspace_id, model_spec_id, version_label, artifact_id,
                 _KINDS[category], f"Studio {category.replace('_', ' ')} publication",
                 input_payload, input_sha, now))
            connection.execute("""INSERT INTO studio_published_versions
                (version_id, model_spec_id, contract_json, source_sha256, contract_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (version_id, model_spec_id, row["contract_json"], row["source_sha256"], row["contract_sha256"], now))
            spec_row = connection.execute("SELECT updated_at FROM model_specs WHERE id = ?", (model_spec_id,)).fetchone()
            update_model_spec_in_transaction(connection, model_spec_id, ModelSpecProjectUpdate(
                workspace_id=workspace_id, expected_updated_at=spec_row["updated_at"], status="published"))
            connection.commit()
        except Exception:
            artifact_path.unlink(missing_ok=True)
            directory.rmdir()
            raise
        return {"id": version_id, "model_spec_id": model_spec_id, "name": row["title"],
                "version_label": version_label, "source_sha256": row["source_sha256"],
                "contract_sha256": row["contract_sha256"], "created_at": now,
                "scientific_status": "unqualified", "category": category}


def revise(workspace_id: str, model_spec_id: str) -> dict:
    with open_sqlite_connection() as connection:
        row = _draft_row(connection, workspace_id, model_spec_id)
        versions = _versions(connection, model_spec_id)
        if not versions:
            raise StudioError("studio_no_published_version", "Publish a version before revising.")
        if row["model_status"] == "draft":
            raise StudioError("studio_draft_open", "A draft is already open; revising again would discard it.")
        version = versions[-1]
        artifact = connection.execute("""SELECT a.stored_path FROM artifacts a
            JOIN model_versions mv ON mv.implementation_artifact_id = a.id WHERE mv.id = ?""", (version["id"],)).fetchone()
        if artifact is None:
            raise StudioError("studio_artifact_missing", "Published artifact is missing.")
        path = Path(artifact["stored_path"])
        name = _artifact_name(_category(json.loads(row["contract_json"])))
        if path != model_implementation_root(workspace_id) / version["id"] / name or _sha(path.read_text(encoding="utf-8")) != version["source_sha256"]:
            raise StudioError("studio_artifact_hash_mismatch", "Published source artifact is invalid.")
        published = connection.execute("SELECT contract_json FROM studio_published_versions WHERE version_id = ?", (version["id"],)).fetchone()
        now = utc_now()
        connection.execute("""UPDATE studio_drafts SET contract_json = ?, source = ?, source_sha256 = ?,
            contract_sha256 = ?, test_cases_json = '[]', test_report_json = NULL, revision_of_version_id = ?,
            updated_at = ? WHERE model_spec_id = ?""",
            (published["contract_json"], path.read_text(encoding="utf-8"), version["source_sha256"],
             version["contract_sha256"], version["id"], now, model_spec_id))
        spec_row = connection.execute("SELECT updated_at FROM model_specs WHERE id = ?", (model_spec_id,)).fetchone()
        update_model_spec_in_transaction(connection, model_spec_id, ModelSpecProjectUpdate(
            workspace_id=workspace_id, expected_updated_at=spec_row["updated_at"], status="draft"))
        connection.commit()
        return _read(connection, workspace_id, model_spec_id)


def library(workspace_id: str) -> list[dict]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute("""SELECT mv.id, mv.model_spec_id, m.title AS name, mv.version_label,
            mv.implementation_kind, sp.source_sha256, sp.contract_sha256, mv.created_at FROM studio_published_versions sp
            JOIN model_versions mv ON mv.id = sp.version_id JOIN model_specs m ON m.id = mv.model_spec_id
            WHERE mv.workspace_id = ? ORDER BY sp.rowid DESC""", (workspace_id,)).fetchall()
        return [{**dict(row), "category": _CATEGORY_OF_KIND.get(row["implementation_kind"], "calculation"),
                 "scientific_status": "unqualified"} for row in rows]



def published_source(connection, workspace_id: str, version_id: str) -> str | None:
    """The published Studio source, or None when its artifact is missing or no longer matches its digest."""
    row = connection.execute(
        "SELECT sp.source_sha256, a.stored_path FROM studio_published_versions sp "
        "JOIN model_versions mv ON mv.id = sp.version_id JOIN artifacts a ON a.id = mv.implementation_artifact_id "
        "WHERE sp.version_id = ? AND mv.workspace_id = ? AND a.status = 'registered'", (version_id, workspace_id),
    ).fetchone()
    path = Path(row["stored_path"]) if row else None
    if path is None or not path.is_file():
        return None
    source = path.read_text(encoding="utf-8")
    return source if hashlib.sha256(source.encode("utf-8")).hexdigest() == row["source_sha256"] else None


def _unit_row(connection, workspace_id: str, version_id: str):
    row = connection.execute("""SELECT mv.id, mv.model_spec_id, mv.version_label, m.title AS name, sp.contract_json,
        sp.source_sha256, sp.contract_sha256, a.stored_path FROM studio_published_versions sp
        JOIN model_versions mv ON mv.id = sp.version_id JOIN model_specs m ON m.id = mv.model_spec_id
        JOIN artifacts a ON a.id = mv.implementation_artifact_id
        WHERE mv.id = ? AND mv.workspace_id = ? AND mv.implementation_kind = ?""",
                             (version_id, workspace_id, UNIT_KIND)).fetchone()
    if row is None:
        raise StudioError("studio_unit_version_not_found", "Published Studio process unit version not found.")
    return row


def _unit_version(connection, workspace_id: str, row, *, with_source: bool) -> dict:
    contract = json.loads(row["contract_json"])
    number = connection.execute("""SELECT COUNT(*) FROM studio_published_versions WHERE model_spec_id = ?
        AND rowid <= (SELECT rowid FROM studio_published_versions WHERE version_id = ?)""",
                                (row["model_spec_id"], row["id"])).fetchone()[0]
    value = {"version_id": row["id"], "model_spec_id": row["model_spec_id"], "name": row["name"],
             "version_label": row["version_label"], "version_number": number, "contract": contract,
             "source_sha256": row["source_sha256"], "contract_sha256": row["contract_sha256"]}
    if with_source:
        path = Path(row["stored_path"])
        if (path != model_implementation_root(workspace_id) / row["id"] / _artifact_name("process_unit")
                or not path.is_file() or _sha(path.read_text(encoding="utf-8")) != row["source_sha256"]
                or _sha(_json(contract)) != row["contract_sha256"]):
            raise StudioError("studio_artifact_hash_mismatch", "Published source artifact is invalid.")
        value["source"] = path.read_text(encoding="utf-8")
    return value


def _lineage(connection, workspace_id: str) -> list[str]:
    """The workspace followed by the data workspaces of its Design version ancestors, nearest first.

    Ordinary workspaces (not a Design version data workspace) have no ancestors.
    """
    chain = [workspace_id]
    row = connection.execute("SELECT parent_version_id, project_id FROM design_project_versions "
                             "WHERE data_workspace_id = ?", (workspace_id,)).fetchone()
    while row is not None and row["parent_version_id"] is not None and len(chain) < 64:
        row = connection.execute("SELECT data_workspace_id, parent_version_id, project_id FROM design_project_versions "
                                 "WHERE id = ? AND project_id = ?", (row["parent_version_id"], row["project_id"])).fetchone()
        if row is not None:
            chain.append(row["data_workspace_id"])
    return chain


def published_unit(workspace_id: str, version_id: str) -> dict:
    """One published process-unit version with its verified source; refuses a tampered artifact.

    A Design version forks Process drafts that pin units published in an ancestor version's workspace, so the
    pin is resolved in the nearest workspace of the lineage that owns it; hashes are verified as always.
    """
    with open_sqlite_connection() as connection:
        for owner in _lineage(connection, workspace_id):
            try:
                row = _unit_row(connection, owner, version_id)
            except StudioError:
                continue
            return _unit_version(connection, owner, row, with_source=True)
        raise StudioError("studio_unit_version_not_found", "Published Studio process unit version not found.")


def published_units_lineage(workspace_id: str) -> list[dict]:
    """published_units over the Design version lineage, oldest ancestor first; for pin currency checks."""
    with open_sqlite_connection() as connection:
        owners = _lineage(connection, workspace_id)
    return [item for owner in reversed(owners) for item in published_units(owner)]


def published_units(workspace_id: str) -> list[dict]:
    """Every published process-unit version of a workspace (no source), oldest first; used by the palette."""
    with open_sqlite_connection() as connection:
        ids = [row["id"] for row in connection.execute("""SELECT mv.id FROM studio_published_versions sp
            JOIN model_versions mv ON mv.id = sp.version_id WHERE mv.workspace_id = ? AND mv.implementation_kind = ?
            ORDER BY sp.rowid""", (workspace_id, UNIT_KIND))]
        return [_unit_version(connection, workspace_id, _unit_row(connection, workspace_id, item), with_source=False)
                for item in ids]


def geometry_preview(workspace_id: str, model_spec_id: str, preview_id: str, name: str) -> Path:
    """One verified artifact from the current draft test report (spec 189)."""
    with open_sqlite_connection() as connection:
        row = _draft_row(connection, workspace_id, model_spec_id)
    report = json.loads(row["test_report_json"]) if row["test_report_json"] else None
    return geometry.preview_file(workspace_id, model_spec_id, report, preview_id, name)
