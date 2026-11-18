"""Read-only readiness board for literature cases (spec 303).

Computes, per literature case declared by installed packs, whether each of the seven steps is
green or red, naming what is missing for red cells.
"""

from __future__ import annotations

import json
import math
import tomllib
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter
from pint import PintError
from pydantic import BaseModel, Field

from app.core.database import get_database_path, open_sqlite_connection
from app.core.packs import readiness_case_files
from app.modules.engineering.bench_runs_view import bench_root, list_runs
from app.modules.memory.literature_checks import check_datum
from app.modules.memory.literature_service import BackingText
from app.modules.process_kernel.errors import ProcessKernelError
from app.modules.process_kernel.units import parse_unit_token, unit_registry

router = APIRouter(tags=["readiness"])

STEPS: tuple[str, ...] = ("paper", "facts", "bound", "run", "compare", "decide", "answer")
MAX_CASES = 20
MAX_NAMES_PER_CASE = 50

# Reason codes per spec
REASON_WORKSPACE_MISSING = "workspace_missing"
REASON_MISSING_RECORD = "missing_record"
REASON_NOT_DECLARED = "not_declared"
REASON_UNIT_MISMATCH = "unit_mismatch"
REASON_OUT_OF_TOLERANCE = "out_of_tolerance"


class ComparisonValue(BaseModel):
    value: float
    unit: str


class Comparison(BaseModel):
    run_id: str
    computed: ComparisonValue
    computed_in_published_unit: float | None = None
    published: ComparisonValue
    deviation: float | None = None
    rel_tol: float


class StepResult(BaseModel):
    step: str
    status: Literal["green", "red"]
    reason: str | None = None
    missing: list[str] = Field(default_factory=list)
    detail: str | None = None
    comparison: Comparison | None = None


class CaseCompare(BaseModel):
    quantity: str
    value: float
    unit: str
    rel_tol: float


class CaseDefinition(BaseModel):
    id: str
    title: str
    workspace: str
    source_title: str
    facts: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    compare: CaseCompare | None = None


class CaseResult(BaseModel):
    id: str
    title: str
    workspace: str
    workspace_id: str | None = None
    source_title: str
    steps: dict[str, StepResult]


class BoardSummary(BaseModel):
    total_cases: int
    total_cells: int
    green_cells: int
    red_cells: int
    first_red: str | None = None


class BoardError(BaseModel):
    case_id: str | None = None
    message: str


class ReadinessBoardRead(BaseModel):
    cases: list[CaseResult] = Field(default_factory=list)
    errors: list[BoardError] = Field(default_factory=list)
    summary: BoardSummary


def _validate_case_entry(raw: Any) -> tuple[CaseDefinition | None, str | None]:
    if not isinstance(raw, dict):
        return None, "case entry must be a table"
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        return None, "case id must be a non-empty string"
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, f"case '{case_id}': title must be a non-empty string"
    workspace = raw.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        return None, f"case '{case_id}': workspace must be a non-empty string"
    source_title = raw.get("source_title")
    if not isinstance(source_title, str) or not source_title.strip():
        return None, f"case '{case_id}': source_title must be a non-empty string"
    facts = raw.get("facts", [])
    if not isinstance(facts, list) or not all(isinstance(f, str) and f.strip() for f in facts):
        return None, f"case '{case_id}': facts must be a list of non-empty strings"
    if len(facts) > MAX_NAMES_PER_CASE:
        return None, f"case '{case_id}': facts exceeds maximum of {MAX_NAMES_PER_CASE} items"
    inputs = raw.get("inputs", [])
    if not isinstance(inputs, list) or not all(isinstance(inp, str) and inp.strip() for inp in inputs):
        return None, f"case '{case_id}': inputs must be a list of non-empty strings"
    if len(inputs) > MAX_NAMES_PER_CASE:
        return None, f"case '{case_id}': inputs exceeds maximum of {MAX_NAMES_PER_CASE} items"
    raw_compare = raw.get("compare")
    compare: CaseCompare | None = None
    compare_err: str | None = None
    if raw_compare is not None:
        if not isinstance(raw_compare, dict):
            return None, f"case '{case_id}': compare must be a table"
        qty = raw_compare.get("quantity")
        if not isinstance(qty, str) or not qty.strip():
            return None, f"case '{case_id}': compare.quantity must be a non-empty string"
        val = raw_compare.get("value")
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return None, f"case '{case_id}': compare.value must be a number"
        unit = raw_compare.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            return None, f"case '{case_id}': compare.unit must be a non-empty string"
        rel_tol = raw_compare.get("rel_tol")
        if isinstance(rel_tol, bool) or not isinstance(rel_tol, (int, float)):
            return None, f"case '{case_id}': compare.rel_tol must be a number"

        fval = float(val)
        ftol = float(rel_tol)
        if fval == 0.0 or not math.isfinite(fval):
            compare_err = f"case '{case_id}': compare.value must be non-zero and finite"
        elif not (0.0 < ftol <= 1.0) or not math.isfinite(ftol):
            compare_err = f"case '{case_id}': compare.rel_tol must be in (0, 1]"
        else:
            compare = CaseCompare(quantity=qty.strip(), value=fval, unit=unit.strip(), rel_tol=ftol)

    return CaseDefinition(
        id=case_id.strip(),
        title=title.strip(),
        workspace=workspace.strip(),
        source_title=source_title.strip(),
        facts=[f.strip() for f in facts],
        inputs=[inp.strip() for inp in inputs],
        compare=compare,
    ), compare_err


def load_case_definitions(case_files: tuple[Path, ...] | None = None) -> tuple[list[CaseDefinition], list[BoardError]]:
    files = readiness_case_files() if case_files is None else case_files
    definitions: list[CaseDefinition] = []
    errors: list[BoardError] = []
    for file_path in files:
        if not file_path.is_file():
            errors.append(BoardError(message=f"Readiness case file {file_path} not found"))
            continue
        try:
            data = tomllib.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            errors.append(BoardError(message=f"Cannot read case file {file_path}: {exc}"))
            continue
        raw_cases = data.get("cases")
        if not isinstance(raw_cases, list):
            errors.append(BoardError(message=f"{file_path}: 'cases' must be a list of tables"))
            continue
        if len(raw_cases) > MAX_CASES:
            errors.append(BoardError(message=f"{file_path}: at most {MAX_CASES} cases allowed; extra cases ignored"))
            raw_cases = raw_cases[:MAX_CASES]
        for raw in raw_cases:
            case_def, err = _validate_case_entry(raw)
            if err:
                cid = raw.get("id") if isinstance(raw, dict) else None
                errors.append(BoardError(case_id=str(cid) if cid is not None else None, message=err))
            if case_def is not None:
                definitions.append(case_def)
    if len(definitions) > MAX_CASES:
        errors.append(BoardError(message=f"Total cases ({len(definitions)}) exceeds maximum of {MAX_CASES}"))
        definitions = definitions[:MAX_CASES]
    return definitions, errors


def _workspace_missing(case: CaseDefinition) -> CaseResult:
    steps = {
        step: StepResult(
            step=step,
            status="red",
            reason=REASON_WORKSPACE_MISSING,
            missing=[case.workspace],
            detail=f"Workspace '{case.workspace}' does not exist",
        )
        for step in STEPS
    }
    return CaseResult(
        id=case.id,
        title=case.title,
        workspace=case.workspace,
        workspace_id=None,
        source_title=case.source_title,
        steps=steps,
    )


def _compute_case(connection, case: CaseDefinition) -> CaseResult:
    ws_row = connection.execute(
        "SELECT id FROM workspaces WHERE slug = ? OR id = ?",
        (case.workspace, case.workspace),
    ).fetchone()

    if ws_row is None:
        return _workspace_missing(case)

    workspace_id = str(ws_row["id"])

    # 1. paper
    src_row = connection.execute(
        "SELECT id, artifact_id, title FROM literature_sources WHERE workspace_id = ? AND title = ? ORDER BY created_at DESC LIMIT 1",
        (workspace_id, case.source_title),
    ).fetchone()

    source_id: str | None = None
    backing_lines: list[str] | None = None
    backing_reason: str = ""

    if src_row is None:
        step_paper = StepResult(
            step="paper",
            status="red",
            reason=REASON_MISSING_RECORD,
            missing=[case.source_title],
            detail=f"Literature source '{case.source_title}' not found",
        )
    else:
        source_id = str(src_row["id"])
        lines, backing_reason = BackingText(connection, workspace_id, src_row).get()
        if lines is None:
            step_paper = StepResult(
                step="paper",
                status="red",
                reason=REASON_MISSING_RECORD,
                missing=[case.source_title],
                detail=f"Backing file unreadable: {backing_reason or 'missing'}",
            )
        else:
            backing_lines = lines
            step_paper = StepResult(
                step="paper",
                status="green",
                reason=None,
                missing=[],
                detail=None,
            )

    # 2. facts
    if source_id is None:
        step_facts = StepResult(
            step="facts",
            status="red",
            reason=REASON_MISSING_RECORD,
            missing=list(case.facts) if case.facts else [case.source_title],
            detail=f"Source '{case.source_title}' not found",
        )
    else:
        missing_facts: list[str] = []
        for qty in case.facts:
            datum_rows = connection.execute(
                """
                SELECT id, value_number, value_text, unit, locator_kind, locator_start, locator_end
                FROM literature_entries
                WHERE workspace_id = ? AND source_id = ? AND entry_kind = 'datum' AND status = 'accepted' AND quantity = ?
                """,
                (workspace_id, source_id, qty),
            ).fetchall()
            satisfied = False
            for d in datum_rows:
                _, verdict = check_datum(
                    value_number=d["value_number"],
                    value_text=d["value_text"],
                    unit=d["unit"],
                    locator_kind=d["locator_kind"],
                    locator_start=d["locator_start"],
                    locator_end=d["locator_end"],
                    lines=backing_lines,
                    unavailable=backing_reason if backing_lines is None else "",
                )
                if verdict in ("usable", "usable_with_warnings"):
                    satisfied = True
                    break
            if not satisfied:
                missing_facts.append(qty)
        if missing_facts:
            step_facts = StepResult(
                step="facts",
                status="red",
                reason=REASON_MISSING_RECORD,
                missing=missing_facts,
                detail=f"Facts missing accepted usable datum: {', '.join(missing_facts)}",
            )
        else:
            step_facts = StepResult(
                step="facts",
                status="green",
                reason=None,
                missing=[],
                detail=None,
            )

    # 3. bound
    if source_id is None:
        step_bound = StepResult(
            step="bound",
            status="red",
            reason=REASON_MISSING_RECORD,
            missing=list(case.inputs) if case.inputs else [case.source_title],
            detail=f"Source '{case.source_title}' not found",
        )
    else:
        missing_inputs: list[str] = []
        for inp in case.inputs:
            param_rows = connection.execute(
                "SELECT id, source_ref FROM parameters WHERE workspace_id = ? AND name = ?",
                (workspace_id, inp),
            ).fetchall()
            bound = False
            for p in param_rows:
                sref = p["source_ref"]
                if sref and sref.startswith("literature_entry:"):
                    eid = sref.split(":", 1)[1]
                    entry = connection.execute(
                        "SELECT id, source_id, status FROM literature_entries WHERE workspace_id = ? AND id = ?",
                        (workspace_id, eid),
                    ).fetchone()
                    if entry and str(entry["source_id"]) == source_id and str(entry["status"]) == "accepted":
                        bound = True
                        break
            if not bound:
                missing_inputs.append(inp)
        if missing_inputs:
            step_bound = StepResult(
                step="bound",
                status="red",
                reason=REASON_MISSING_RECORD,
                missing=missing_inputs,
                detail=f"Inputs not bound to accepted datum of source: {', '.join(missing_inputs)}",
            )
        else:
            step_bound = StepResult(
                step="bound",
                status="green",
                reason=None,
                missing=[],
                detail=None,
            )

    # 4. run
    runs = list_runs(workspace_id)["runs"]
    has_usable_run = any(r.get("verdict") in ("usable", "usable_with_warnings") for r in runs)

    if has_usable_run:
        step_run = StepResult(step="run", status="green", reason=None, missing=[], detail=None)
    else:
        step_run = StepResult(
            step="run",
            status="red",
            reason=REASON_MISSING_RECORD,
            missing=["bench_run"],
            detail="No bench run with usable verdict in workspace",
        )

    # 5. compare
    if case.compare is None:
        step_compare = StepResult(
            step="compare",
            status="red",
            reason=REASON_NOT_DECLARED,
            missing=[],
            detail="Case does not declare a published comparison value",
        )
    else:
        matched_run_id: str | None = None
        matched_computed_val: float | None = None
        matched_computed_unit: str | None = None

        root = bench_root(workspace_id)
        for r in runs:
            if r.get("verdict") not in ("usable", "usable_with_warnings"):
                continue
            run_id = r.get("run_id")
            if not isinstance(run_id, str):
                continue
            run_dir = (root / run_id).resolve()
            if not run_dir.is_relative_to(root) or not run_dir.is_dir():
                continue
            result_file = run_dir / "result.json"
            if not result_file.is_file():
                continue
            try:
                data = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            outputs = data.get("outputs")
            if not isinstance(outputs, list):
                continue

            found_output = False
            for out in outputs:
                if not isinstance(out, dict):
                    continue
                if out.get("name") != case.compare.quantity:
                    continue
                val_obj = out.get("value")
                if not isinstance(val_obj, dict):
                    continue
                out_val = val_obj.get("value")
                out_unit = val_obj.get("unit")
                if isinstance(out_val, bool) or not isinstance(out_val, (int, float)):
                    continue
                if not math.isfinite(float(out_val)):
                    continue
                if not isinstance(out_unit, str) or not out_unit.strip():
                    continue

                matched_run_id = run_id
                matched_computed_val = float(out_val)
                matched_computed_unit = out_unit.strip()
                found_output = True
                break

            if found_output:
                break

        if matched_run_id is None or matched_computed_val is None or matched_computed_unit is None:
            step_compare = StepResult(
                step="compare",
                status="red",
                reason=REASON_MISSING_RECORD,
                missing=[case.compare.quantity],
                detail=f"No usable bench run with output '{case.compare.quantity}'",
            )
        else:
            published_val = ComparisonValue(value=case.compare.value, unit=case.compare.unit)
            computed_val = ComparisonValue(value=matched_computed_val, unit=matched_computed_unit)
            converted_val: float | None = None
            try:
                src_pint = parse_unit_token(computed_val.unit)
                tgt_pint = parse_unit_token(published_val.unit)
                reg = unit_registry()
                converted_qty = reg.Quantity(computed_val.value, src_pint).to(tgt_pint)
                mag = float(converted_qty.magnitude)
                if not math.isfinite(mag):
                    raise ProcessKernelError("quantity_value_invalid", "Converted quantity must be finite")
                converted_val = mag
            except (ProcessKernelError, PintError, Exception):
                converted_val = None

            if converted_val is None:
                step_compare = StepResult(
                    step="compare",
                    status="red",
                    reason=REASON_UNIT_MISMATCH,
                    missing=[],
                    detail=f"Unit '{computed_val.unit}' cannot be converted to '{published_val.unit}'",
                    comparison=Comparison(
                        run_id=matched_run_id,
                        computed=computed_val,
                        computed_in_published_unit=None,
                        published=published_val,
                        deviation=None,
                        rel_tol=case.compare.rel_tol,
                    ),
                )
            else:
                deviation = abs(converted_val - published_val.value) / abs(published_val.value)
                comparison = Comparison(
                    run_id=matched_run_id,
                    computed=computed_val,
                    computed_in_published_unit=converted_val,
                    published=published_val,
                    deviation=deviation,
                    rel_tol=case.compare.rel_tol,
                )
                if deviation <= case.compare.rel_tol:
                    step_compare = StepResult(
                        step="compare",
                        status="green",
                        reason=None,
                        missing=[],
                        detail=None,
                        comparison=comparison,
                    )
                else:
                    step_compare = StepResult(
                        step="compare",
                        status="red",
                        reason=REASON_OUT_OF_TOLERANCE,
                        missing=[],
                        detail=f"Deviation {deviation:.4f} exceeds relative tolerance {case.compare.rel_tol}",
                        comparison=comparison,
                    )

    # 6. decide
    dec_row = connection.execute(
        """
        SELECT id FROM decisions
        WHERE workspace_id = ? AND status = 'accepted' AND linked_run_id IS NOT NULL AND trim(linked_run_id) != ''
        LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()

    if dec_row is not None:
        step_decide = StepResult(step="decide", status="green", reason=None, missing=[], detail=None)
    else:
        step_decide = StepResult(
            step="decide",
            status="red",
            reason=REASON_MISSING_RECORD,
            missing=["decision"],
            detail="No accepted decision linked to a run",
        )

    # 7. answer
    has_answer_run = any(r.get("evaluator_id") == "retrieval.eval" and r.get("verdict") == "usable" for r in runs)

    if has_answer_run:
        step_answer = StepResult(step="answer", status="green", reason=None, missing=[], detail=None)
    else:
        step_answer = StepResult(
            step="answer",
            status="red",
            reason=REASON_MISSING_RECORD,
            missing=["retrieval.eval run"],
            detail="No retrieval.eval bench run with usable verdict",
        )

    steps = {
        "paper": step_paper,
        "facts": step_facts,
        "bound": step_bound,
        "run": step_run,
        "compare": step_compare,
        "decide": step_decide,
        "answer": step_answer,
    }
    return CaseResult(
        id=case.id,
        title=case.title,
        workspace=case.workspace,
        workspace_id=workspace_id,
        source_title=case.source_title,
        steps=steps,
    )


def compute_readiness_board(case_files: tuple[Path, ...] | None = None) -> ReadinessBoardRead:
    definitions, errors = load_case_definitions(case_files)
    case_results: list[CaseResult] = []
    if definitions:
        if get_database_path().is_file():
            with open_sqlite_connection(read_only=True) as connection:
                case_results = [_compute_case(connection, cdef) for cdef in definitions]
        else:
            case_results = [_workspace_missing(cdef) for cdef in definitions]

    total_cases = len(case_results)
    total_cells = 7 * total_cases
    green_cells = sum(1 for c in case_results for s in c.steps.values() if s.status == "green")
    red_cells = total_cells - green_cells
    first_red: str | None = None
    for c in case_results:
        for sname in STEPS:
            if c.steps[sname].status == "red":
                first_red = f"{c.id} {sname}"
                break
        if first_red is not None:
            break

    summary = BoardSummary(
        total_cases=total_cases,
        total_cells=total_cells,
        green_cells=green_cells,
        red_cells=red_cells,
        first_red=first_red,
    )
    return ReadinessBoardRead(cases=case_results, errors=errors, summary=summary)


@router.get("/readiness", response_model=ReadinessBoardRead)
def get_readiness() -> ReadinessBoardRead:
    return compute_readiness_board()
