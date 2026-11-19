"""Guided engineering question flow (spec 312).

Provides deterministic 7-step path bar evaluation, 'Prossimo passo' card computation,
and design question CRUD for Design.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.modules.engineering import bench_runs_view
from app.modules.engineering.readiness_board import compute_readiness_board
from app.modules.events.service import utc_now
from app.modules.memory import literature_ingest
from app.modules.process_stack import draft as process_draft

StepState = Literal["done", "blocked", "in_progress", "not_started"]
QuestionStatus = Literal["open", "closed"]


class GuidedStepDefinition(BaseModel):
    step: str
    order: int
    label_it: str
    route: str
    description_it: str


class NextAction(BaseModel):
    step: str
    title_it: str
    reason_it: str
    action_label_it: str
    route: str


class GuidedStepEvaluation(BaseModel):
    step: str
    order: int
    label_it: str
    route: str
    description_it: str
    state: StepState
    blocked_items: list[str] = Field(default_factory=list)


class DesignQuestionRead(BaseModel):
    id: str
    workspace_id: str
    title: str
    status: QuestionStatus
    what_changes: str | None = None
    sensitivity: str | None = None
    explore_skip_reason: str | None = None
    linked_decision_id: str | None = None
    created_at: str
    updated_at: str
    steps: list[GuidedStepEvaluation]
    next_action: NextAction | None = None


class GuidedQuestionsResponse(BaseModel):
    steps_definition: list[GuidedStepDefinition]
    questions: list[DesignQuestionRead]
    active_question_id: str | None = None
    steps: list[GuidedStepEvaluation]
    next_action: NextAction | None = None


class CreateQuestionRequest(BaseModel):
    title: str
    what_changes: str | None = None
    sensitivity: str | None = None
    explore_skip_reason: str | None = None
    linked_decision_id: str | None = None


class UpdateQuestionRequest(BaseModel):
    title: str | None = None
    status: QuestionStatus | None = None
    what_changes: str | None = None
    sensitivity: str | None = None
    explore_skip_reason: str | None = None
    linked_decision_id: str | None = None


def _process_draft_records(
    workspace_id: str,
) -> list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]]:
    try:
        drafts = process_draft.list_drafts(workspace_id)[:20]
    except Exception:
        return []

    records = []
    for draft_info in drafts:
        try:
            directory = process_draft.draft_dir(workspace_id, draft_info["draft_id"])
            document = process_draft.load_revision(directory, draft_info["revision"])["document"]
            runs = process_draft.list_runs(directory)
            if not isinstance(document, dict) or not isinstance(runs, list):
                continue
        except Exception:
            continue
        records.append((draft_info, document, runs))
    return records


GUIDED_STEPS: tuple[dict[str, Any], ...] = (
    {
        "step": "frame",
        "order": 1,
        "label_it": "Inquadra",
        "route": "/memory/project-basis",
        "description_it": "Definisci il quesito tecnico e associa almeno un requisito al progetto.",
    },
    {
        "step": "sources",
        "order": 2,
        "label_it": "Fonti",
        "route": "/memory/literature",
        "description_it": "Raccogli e verifica le fonti bibliografiche e sperimentali.",
    },
    {
        "step": "model",
        "order": 3,
        "label_it": "Modello",
        "route": "/design/project",
        "description_it": "Crea componenti, specifiche o bozze e vincola i parametri alle fonti.",
    },
    {
        "step": "run",
        "order": 4,
        "label_it": "Calcolo",
        "route": "/runs",
        "description_it": "Esegui e verifica i calcoli o le simulazioni di benchmark.",
    },
    {
        "step": "explore",
        "order": 5,
        "label_it": "Esplora",
        "route": "/design/studies",
        "description_it": "Esplora varianti di progetto o definisci studi parametrici.",
    },
    {
        "step": "compare",
        "order": 6,
        "label_it": "Confronta",
        "route": "/development/readiness",
        "description_it": "Confronta i risultati di calcolo con i requisiti e i casi di riferimento.",
    },
    {
        "step": "decide",
        "order": 7,
        "label_it": "Decidi",
        "route": "/memory/project-basis",
        "description_it": "Formalizza la decisione ingegneristica motivando la scelta.",
    },
)


def get_steps_definition() -> list[GuidedStepDefinition]:
    return [GuidedStepDefinition(**s) for s in GUIDED_STEPS]


def evaluate_step(
    connection: sqlite3.Connection,
    workspace_id: str,
    step_name: str,
    question: dict[str, Any] | None = None,
) -> tuple[StepState, list[str]]:
    """Evaluate a single guided step deterministically against workspace records."""
    if step_name == "frame":
        title = (question.get("title") or "").strip() if question else ""
        has_title = bool(title)
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM requirements WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        has_reqs = bool(row and row["c"] > 0)
        if has_title and has_reqs:
            return "done", []
        return "in_progress", []

    elif step_name == "sources":
        flagged = literature_ingest.flagged_entry_labels(connection, workspace_id)
        if flagged:
            return "blocked", flagged

        sources_row = connection.execute(
            "SELECT COUNT(*) AS c FROM literature_sources WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if sources_row and sources_row["c"] > 0:
            return "done", []
        return "not_started", []

    elif step_name == "model":
        unbound_params = connection.execute(
            """
            SELECT name
            FROM parameters
            WHERE workspace_id = ?
              AND (lifecycle_state = 'active' OR (lifecycle_state IS NULL AND status != 'superseded'))
              AND (source_ref IS NULL OR trim(source_ref) = '')
            ORDER BY name
            """,
            (workspace_id,),
        ).fetchall()
        if unbound_params:
            return "blocked", [row["name"] for row in unbound_params]

        comp_row = connection.execute(
            """
            SELECT 1 FROM design_components
            WHERE project_id = ? OR version_id IN (
                SELECT id FROM design_project_versions WHERE project_id = ? OR data_workspace_id = ?
            )
            LIMIT 1
            """,
            (workspace_id, workspace_id, workspace_id),
        ).fetchone()

        spec_row = connection.execute(
            "SELECT 1 FROM model_specs WHERE workspace_id = ? LIMIT 1",
            (workspace_id,),
        ).fetchone()

        draft_row = connection.execute(
            "SELECT 1 FROM studio_drafts WHERE workspace_id = ? LIMIT 1",
            (workspace_id,),
        ).fetchone()

        if comp_row is not None or spec_row is not None or draft_row is not None:
            return "done", []
        for _draft_info, document, _runs in _process_draft_records(workspace_id):
            objects = document.get("objects")
            if isinstance(objects, dict) and any(
                isinstance(item, dict) and item.get("kind") == "unit" for item in objects.values()
            ):
                return "done", []
        return "not_started", []

    elif step_name == "run":
        bench_runs: list[dict[str, Any]] = []
        try:
            runs_data = bench_runs_view.list_runs(workspace_id)
            bench_runs = runs_data.get("runs", [])
        except Exception:
            bench_runs = []

        has_usable_bench = any(
            r.get("verdict") in ("usable", "usable_with_warnings") for r in bench_runs
        )

        sim_rows = connection.execute(
            "SELECT status FROM simulation_runs WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall()
        sim_statuses = [r["status"] for r in sim_rows]

        has_completed_sim = any(s == "completed" for s in sim_statuses)
        draft_records = _process_draft_records(workspace_id)
        has_completed_draft = any(
            any(
                run.get("action") == "run"
                and run.get("status") == "completed"
                and (run.get("outcome") or {}).get("state") == "converged"
                for run in runs
            )
            for _draft_info, _document, runs in draft_records
        )

        if has_usable_bench or has_completed_sim or has_completed_draft:
            return "done", []

        if any(s in ("running", "queued") for s in sim_statuses):
            return "in_progress", []

        draft_blocked_items = [
            f'Diagramma "{process_draft.plain_text(draft_info.get("name") or draft_info.get("draft_id"))}": '
            f'{"non converge" if (runs[0].get("outcome") or {}).get("state") == "non_converged" else "fallito"}'
            for draft_info, _document, runs in draft_records
            if runs
            and (runs[0].get("outcome") or {}).get("state") in {"non_converged", "failed"}
        ]
        if bench_runs or sim_statuses or draft_blocked_items:
            return "blocked", draft_blocked_items or ["No runs produced a usable verdict"]

        return "not_started", []

    elif step_name == "explore":
        skip_reason = (
            (question.get("explore_skip_reason") or "").strip() if question else ""
        )
        if skip_reason:
            return "done", []

        template_row = connection.execute(
            """
            SELECT 1 FROM design_component_study_templates t
            JOIN design_components c ON t.component_id = c.id
            WHERE c.project_id = ? OR c.version_id IN (
                SELECT id FROM design_project_versions WHERE project_id = ? OR data_workspace_id = ?
            )
            LIMIT 1
            """,
            (workspace_id, workspace_id, workspace_id),
        ).fetchone()
        if template_row is not None:
            return "done", []

        versions_count_row = connection.execute(
            """
            SELECT COUNT(*) AS c FROM design_project_versions
            WHERE project_id = ? OR data_workspace_id = ?
            """,
            (workspace_id, workspace_id),
        ).fetchone()
        if versions_count_row and versions_count_row["c"] > 1:
            return "done", []

        return "not_started", []

    elif step_name == "compare":
        resolution_row = connection.execute(
            """
            SELECT 1 FROM simulation_result_resolutions r
            JOIN simulation_result_values v ON r.value_id = v.id
            JOIN simulation_result_batches b ON v.batch_id = b.id
            WHERE b.workspace_id = ?
            LIMIT 1
            """,
            (workspace_id,),
        ).fetchone()
        if resolution_row is not None:
            return "done", []

        has_green_readiness = False
        try:
            board = compute_readiness_board()
            for case in board.cases:
                if (
                    case.workspace_id == workspace_id
                    and case.steps.get("compare")
                    and case.steps["compare"].status == "green"
                ):
                    has_green_readiness = True
                    break
        except Exception:
            pass

        if has_green_readiness:
            return "done", []

        return "not_started", []

    elif step_name == "decide":
        linked_decision = (
            (question.get("linked_decision_id") or "").strip() if question else ""
        )
        if linked_decision:
            return "done", []

        dec_row = connection.execute(
            "SELECT 1 FROM decisions WHERE workspace_id = ? LIMIT 1",
            (workspace_id,),
        ).fetchone()
        if dec_row is not None:
            return "done", []

        return "not_started", []

    return "not_started", []


def evaluate_all_steps(
    connection: sqlite3.Connection,
    workspace_id: str,
    question: dict[str, Any] | None = None,
) -> list[GuidedStepEvaluation]:
    evaluations: list[GuidedStepEvaluation] = []
    for step_def in GUIDED_STEPS:
        state, blocked_items = evaluate_step(
            connection, workspace_id, step_def["step"], question
        )
        evaluations.append(
            GuidedStepEvaluation(
                step=step_def["step"],
                order=step_def["order"],
                label_it=step_def["label_it"],
                route=step_def["route"],
                description_it=step_def["description_it"],
                state=state,
                blocked_items=blocked_items,
            )
        )
    return evaluations


def compute_next_action(
    steps: list[GuidedStepEvaluation],
) -> NextAction | None:
    """Find the earliest non-done step and return the deterministic next action."""
    for step in sorted(steps, key=lambda s: s.order):
        if step.state == "done":
            continue

        if step.step == "frame":
            return NextAction(
                step=step.step,
                title_it="Inquadra la domanda e i requisiti",
                reason_it="Definisci il quesito tecnico e associa almeno un requisito al progetto.",
                action_label_it="Vai a Project Basis",
                route=step.route,
            )
        elif step.step == "sources":
            if step.state == "blocked":
                items_text = ", ".join(step.blocked_items[:3])
                suffix = f" ({items_text})" if items_text else ""
                return NextAction(
                    step=step.step,
                    title_it="Risolvi le anomalie nelle fonti",
                    reason_it=f"Ci sono voci di letteratura contrassegnate come non verificate{suffix}.",
                    action_label_it="Risolvi le fonti",
                    route=step.route,
                )
            return NextAction(
                step=step.step,
                title_it="Aggiungi e verifica le fonti",
                reason_it="Inserisci almeno una fonte di letteratura verificata per il progetto.",
                action_label_it="Vai a Letteratura",
                route=step.route,
            )
        elif step.step == "model":
            if step.state == "blocked":
                items_text = ", ".join(step.blocked_items[:3])
                suffix = f" ({items_text})" if items_text else ""
                return NextAction(
                    step=step.step,
                    title_it="Collega le fonti ai parametri",
                    reason_it=f"I seguenti parametri attivi non hanno una fonte associata{suffix}.",
                    action_label_it="Collega i parametri",
                    route=step.route,
                )
            if step.state == "not_started":
                return NextAction(
                    step=step.step,
                    title_it="Apri il diagramma di processo",
                    reason_it=(
                        "Costruisci il diagramma con le unità esistenti: pompa, separatore, "
                        "unità di coltura e scambiatore."
                    ),
                    action_label_it="Apri il diagramma di processo",
                    route="/design/process",
                )
            return NextAction(
                step=step.step,
                title_it="Definisci il modello di progetto",
                reason_it="Crea almeno un componente, specifica o bozza di modello nel workspace.",
                action_label_it="Modella il progetto",
                route=step.route,
            )
        elif step.step == "run":
            if step.state == "blocked":
                if any(item.startswith('Diagramma "') for item in step.blocked_items):
                    return NextAction(
                        step=step.step,
                        title_it="Risolvi i problemi nel diagramma di processo",
                        reason_it="L'ultimo calcolo del diagramma di processo non converge.",
                        action_label_it="Apri il diagramma di processo",
                        route="/design/process",
                    )
                return NextAction(
                    step=step.step,
                    title_it="Risolvi i problemi nei calcoli",
                    reason_it="Nessun calcolo o simulazione ha prodotto un verdetto utilizzabile.",
                    action_label_it="Verifica i calcoli",
                    route=step.route,
                )
            elif step.state == "in_progress":
                return NextAction(
                    step=step.step,
                    title_it="Monitora i calcoli in corso",
                    reason_it="Una simulazione è attualmente in esecuzione o in coda.",
                    action_label_it="Monitora i calcoli",
                    route=step.route,
                )
            return NextAction(
                step=step.step,
                title_it="Esegui i calcoli di benchmark",
                reason_it="Avvia una simulazione o un calcolo per validare il modello.",
                action_label_it="Vai ai calcoli",
                route=step.route,
            )
        elif step.step == "explore":
            return NextAction(
                step=step.step,
                title_it="Esplora le varianti di progetto",
                reason_it="Definisci uno studio parametrico, crea una variante di versione o inserisci una motivazione di esclusione.",
                action_label_it="Vai agli studi",
                route=step.route,
            )
        elif step.step == "compare":
            return NextAction(
                step=step.step,
                title_it="Confronta e verifica i risultati",
                reason_it="Risolvi le verifiche dei risultati di simulazione o raggiungi lo stato verde sui casi di test.",
                action_label_it="Vai alla prontezza",
                route=step.route,
            )
        elif step.step == "decide":
            return NextAction(
                step=step.step,
                title_it="Formalizza la decisione finale",
                reason_it="Registra la decisione ingegneristica per rispondere alla domanda di progetto.",
                action_label_it="Registra la decisione",
                route=step.route,
            )

    return None


def list_questions(
    connection: sqlite3.Connection, workspace_id: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, workspace_id, title, status, what_changes, sensitivity,
               explore_skip_reason, linked_decision_id, created_at, updated_at
        FROM design_questions
        WHERE workspace_id = ?
        ORDER BY created_at ASC, id ASC
        """,
        (workspace_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_question(
    connection: sqlite3.Connection, workspace_id: str, question_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT id, workspace_id, title, status, what_changes, sensitivity,
               explore_skip_reason, linked_decision_id, created_at, updated_at
        FROM design_questions
        WHERE workspace_id = ? AND id = ?
        """,
        (workspace_id, question_id),
    ).fetchone()
    return dict(row) if row else None


def create_question(
    connection: sqlite3.Connection,
    workspace_id: str,
    title: str,
    what_changes: str | None = None,
    sensitivity: str | None = None,
    explore_skip_reason: str | None = None,
    linked_decision_id: str | None = None,
) -> dict[str, Any]:
    cleaned_title = title.strip()
    if not cleaned_title:
        raise ValueError("Question title cannot be empty.")

    qid = str(uuid4())
    now = utc_now()
    connection.execute(
        """
        INSERT INTO design_questions (
            id, workspace_id, title, status, what_changes, sensitivity,
            explore_skip_reason, linked_decision_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
        """,
        (
            qid,
            workspace_id,
            cleaned_title,
            what_changes.strip() if what_changes else None,
            sensitivity.strip() if sensitivity else None,
            explore_skip_reason.strip() if explore_skip_reason else None,
            linked_decision_id.strip() if linked_decision_id else None,
            now,
            now,
        ),
    )
    connection.commit()
    return get_question(connection, workspace_id, qid)  # type: ignore[return-value]


def update_question(
    connection: sqlite3.Connection,
    workspace_id: str,
    question_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    existing = get_question(connection, workspace_id, question_id)
    if existing is None:
        raise LookupError("Question not found in workspace.")

    fields: list[str] = []
    params: list[Any] = []

    if "title" in updates and updates["title"] is not None:
        cleaned_title = updates["title"].strip()
        if not cleaned_title:
            raise ValueError("Question title cannot be empty.")
        fields.append("title = ?")
        params.append(cleaned_title)

    if "status" in updates and updates["status"] is not None:
        status = updates["status"]
        if status not in ("open", "closed"):
            raise ValueError("Status must be 'open' or 'closed'.")
        fields.append("status = ?")
        params.append(status)

    if "what_changes" in updates:
        val = updates["what_changes"]
        fields.append("what_changes = ?")
        params.append(val.strip() if isinstance(val, str) else None)

    if "sensitivity" in updates:
        val = updates["sensitivity"]
        fields.append("sensitivity = ?")
        params.append(val.strip() if isinstance(val, str) else None)

    if "explore_skip_reason" in updates:
        val = updates["explore_skip_reason"]
        fields.append("explore_skip_reason = ?")
        params.append(val.strip() if isinstance(val, str) else None)

    if "linked_decision_id" in updates:
        val = updates["linked_decision_id"]
        fields.append("linked_decision_id = ?")
        params.append(val.strip() if isinstance(val, str) else None)

    if not fields:
        return existing

    now = utc_now()
    fields.append("updated_at = ?")
    params.append(now)

    params.extend([workspace_id, question_id])
    query = f"UPDATE design_questions SET {', '.join(fields)} WHERE workspace_id = ? AND id = ?"
    connection.execute(query, params)
    connection.commit()
    return get_question(connection, workspace_id, question_id)  # type: ignore[return-value]
