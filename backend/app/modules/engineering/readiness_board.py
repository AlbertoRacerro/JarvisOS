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
from app.modules.engineering.process_run_values import has_usable_process_run, newest_process_run_value
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
    quantity: str | None = None
    run_id: str
    run_label: str | None = None
    computed: ComparisonValue
    computed_in_published_unit: float | None = None
    published: ComparisonValue
    deviation: float | None = None
    rel_tol: float


class StoredVerdictSummary(BaseModel):
    id: str
    verdict: Literal["pass", "fail"]
    status: Literal["recorded", "accepted", "superseded"]
    run_id: str
    deviation: float
    rationale: str | None = None


class StepResult(BaseModel):
    step: str
    status: Literal["green", "red"]
    reason: str | None = None
    missing: list[str] = Field(default_factory=list)
    detail: str | None = None
    comparison: Comparison | None = None
    stored_verdict: StoredVerdictSummary | None = None


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


def _run_output(root: Path, run_id: str, quantity: str) -> tuple[float, str] | None:
    """The named numeric output of one bench run, read inside the bench root; None when absent or unreadable."""
    result_file = (root / run_id / "result.json").resolve()
    if not result_file.is_relative_to(root) or not result_file.is_file():
        return None
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    outputs = data.get("outputs") if isinstance(data, dict) else None
    if not isinstance(outputs, list):
        return None
    for out in outputs:
        if not isinstance(out, dict) or out.get("name") != quantity or not isinstance(out.get("value"), dict):
            continue
        value, unit = out["value"].get("value"), out["value"].get("unit")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            continue
        if isinstance(unit, str) and unit.strip():
            return float(value), unit.strip()
    return None


def _convert(value: float, unit: str, target: str) -> float | None:
    """Value in the target unit through the single Pint owner; None when the units are not comparable."""
    try:
        magnitude = float(unit_registry().Quantity(value, parse_unit_token(unit)).to(parse_unit_token(target)).magnitude)
    except (ProcessKernelError, PintError, ValueError, TypeError):
        return None
    return magnitude if math.isfinite(magnitude) else None


def _compare_step(compare: CaseCompare | None, workspace_id: str, runs: list[dict[str, Any]]) -> StepResult:
    if compare is None:
        return StepResult(step="compare", status="red", reason=REASON_NOT_DECLARED,
                          detail="Case does not declare a published comparison value")
    root = bench_root(workspace_id)
    found: tuple[str, float, str] | None = None
    for run in runs:
        run_id = run.get("run_id")
        if run.get("verdict") not in ("usable", "usable_with_warnings") or not isinstance(run_id, str):
            continue
        output = _run_output(root, run_id, compare.quantity)
        if output is not None:
            found = (run_id, *output, None)
            break
    if found is None:
        process_value = newest_process_run_value(workspace_id, compare.quantity)
        if process_value is not None:
            run_id, run_label, value, unit = process_value
            found = (run_id, value, unit, run_label)
    if found is None:
        return StepResult(step="compare", status="red", reason=REASON_MISSING_RECORD, missing=[compare.quantity],
                          detail=f"No usable bench run or converged process run with output '{compare.quantity}'")
    run_id, value, unit, run_label = found
    converted = _convert(value, unit, compare.unit)
    deviation = None if converted is None else abs(converted - compare.value) / abs(compare.value)
    comparison = Comparison(
        quantity=compare.quantity,
        run_id=run_id,
        run_label=run_label,
        computed=ComparisonValue(value=value, unit=unit),
        computed_in_published_unit=converted,
        published=ComparisonValue(value=compare.value, unit=compare.unit),
        deviation=deviation,
        rel_tol=compare.rel_tol,
    )
    if deviation is None:
        return StepResult(step="compare", status="red", reason=REASON_UNIT_MISMATCH, comparison=comparison,
                          detail=f"Unit '{unit}' cannot be converted to '{compare.unit}'")
    if deviation <= compare.rel_tol:
        return StepResult(step="compare", status="green", comparison=comparison)
    return StepResult(step="compare", status="red", reason=REASON_OUT_OF_TOLERANCE, comparison=comparison,
                      detail=f"Deviation {deviation:.4f} exceeds relative tolerance {compare.rel_tol}")


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
    if not has_usable_run:
        has_usable_run = has_usable_process_run(workspace_id)

    if has_usable_run:
        step_run = StepResult(step="run", status="green", reason=None, missing=[], detail=None)
    else:
        step_run = StepResult(
            step="run",
            status="red",
            reason=REASON_MISSING_RECORD,
            missing=["bench_run"],
            detail="No usable bench run or converged process run in workspace",
        )

    # 5. compare
    step_compare = _compare_step(case.compare, workspace_id, runs)
    if step_compare.comparison is not None and step_compare.comparison.run_id:
        v_row = connection.execute(
            """
            SELECT id, verdict, status, run_id, deviation, rationale
            FROM case_verdicts
            WHERE workspace_id = ? AND case_id = ? AND run_id = ? AND status != 'superseded'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (workspace_id, case.id, step_compare.comparison.run_id),
        ).fetchone()
        if v_row is not None:
            step_compare.stored_verdict = StoredVerdictSummary(
                id=str(v_row["id"]),
                verdict=v_row["verdict"],
                status=v_row["status"],
                run_id=str(v_row["run_id"]),
                deviation=float(v_row["deviation"]),
                rationale=v_row["rationale"],
            )

    # 6. decide
    dec_row = connection.execute(
        """
        SELECT d.id FROM decisions d
        JOIN case_verdicts v ON v.id = d.case_verdict_id AND v.workspace_id = d.workspace_id
        WHERE d.workspace_id = ? AND d.status = 'accepted' AND v.case_id = ? AND v.status = 'accepted'
        LIMIT 1
        """,
        (workspace_id, case.id),
    ).fetchone()

    if dec_row is not None:
        step_decide = StepResult(step="decide", status="green", reason=None, missing=[], detail=None)
    else:
        step_decide = StepResult(
            step="decide",
            status="red",
            reason=REASON_MISSING_RECORD,
            missing=["decision"],
            detail="No accepted decision linked to an accepted verdict of this case",
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
