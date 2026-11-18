import { useEffect, useState } from "react";
import type { Navigate } from "../app/AppLink";
import AppLink from "../app/AppLink";
import { getReadiness, type CaseResult, type ReadinessBoardRead } from "../api/readinessBoard";

export const READINESS_TITLE = "Prontezza";
export const READINESS_EMPTY_STATE =
  "Nessun caso di letteratura installato: i casi arrivano dal pacchetto di progetto";

export const STEP_ORDER = [
  "paper",
  "facts",
  "bound",
  "run",
  "compare",
  "decide",
  "answer"
] as const;
export type StepId = (typeof STEP_ORDER)[number];

export const STEP_LABELS: Record<StepId, string> = {
  paper: "Articolo",
  facts: "Dati",
  bound: "Ingressi legati",
  run: "Calcolo",
  compare: "Confronto",
  decide: "Decisione",
  answer: "Risposta"
};

export const REASON_WORKSPACE_MISSING = "Il workspace del caso non esiste";
export const REASON_CAPABILITY_MISSING = "Funzione non ancora disponibile nel prodotto";

export const MISSING_RECORD_SENTENCES: Record<StepId, string> = {
  paper: "Manca la fonte di letteratura o il file allegato:",
  facts: "Mancano dati accettati e verificati per:",
  bound: "Mancano parametri legati ai dati accettati della fonte:",
  run: "Manca un risultato del banco di prova con esito utilizzabile nel workspace del caso.",
  compare: "Funzione non ancora disponibile nel prodotto",
  decide: "Manca una decisione accettata collegata a un calcolo.",
  answer: "Manca una misura della qualità della ricerca con esito utilizzabile nel workspace del caso."
};

// Missing items are shown only where they are the engineer's own names (source title, quantities, parameters, the
// workspace); for the other steps the board names a record kind, which the sentence already says in words.
const STEPS_WITH_NAMED_ITEMS: ReadonlySet<StepId> = new Set(["paper", "facts", "bound"]);

export function firstRedCell(cases: readonly CaseResult[]): { caseItem: CaseResult; stepId: StepId } | null {
  for (const caseItem of cases) {
    for (const stepId of STEP_ORDER) {
      if (caseItem.steps[stepId]?.status === "red") return { caseItem, stepId };
    }
  }
  return null;
}

export const STEP_LINKS: Partial<Record<StepId, { path: string; label: string }>> = {
  paper: { path: "/memory/literature", label: "Apri Letteratura" },
  facts: { path: "/memory/literature", label: "Apri Letteratura" },
  bound: { path: "/memory/literature", label: "Apri Letteratura" },
  run: { path: "/runs", label: "Apri Runs" },
  decide: { path: "/memory/project-basis", label: "Apri Project Basis" },
  answer: { path: "/memory/search", label: "Apri Ricerca" }
};

export function getReasonText(step: StepId, reason: string | null): string {
  if (reason === "workspace_missing") return REASON_WORKSPACE_MISSING;
  if (reason === "capability_missing") return REASON_CAPABILITY_MISSING;
  if (reason === "missing_record") {
    return MISSING_RECORD_SENTENCES[step] ?? "Elemento mancante per questo passaggio:";
  }
  return "";
}

type Props = Readonly<{
  navigate: Navigate;
  onWorkspaceChange?: (workspaceId: string) => void;
}>;

export default function ReadinessBoard({ navigate, onWorkspaceChange }: Props) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [board, setBoard] = useState<ReadinessBoardRead | null>(null);
  const [selectedCell, setSelectedCell] = useState<{ caseId: string; stepId: StepId } | null>(null);

  useEffect(() => {
    let alive = true;
    getReadiness()
      .then((data) => {
        if (!alive) return;
        setBoard(data);
        if (data.cases.length > 0) {
          const firstRed = firstRedCell(data.cases);
          setSelectedCell(firstRed ? { caseId: firstRed.caseItem.id, stepId: firstRed.stepId } : { caseId: data.cases[0].id, stepId: "paper" });
        }
      })
      .catch((err) => {
        if (!alive) return;
        setError("Non è stato possibile leggere il tabellone di prontezza.");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="readiness-board" aria-live="polite">
        <p>Caricamento prontezza…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="readiness-board">
        <p className="readiness-board__errors">{error}</p>
      </div>
    );
  }

  if (!board || board.cases.length === 0) {
    return (
      <div className="readiness-board">
        <p className="readiness-board__empty">{READINESS_EMPTY_STATE}</p>
        {board && board.errors.length > 0 && (
          <div className="readiness-board__errors">
            <h3>Errori</h3>
            <ul>
              {board.errors.map((err, idx) => (
                <li key={idx}>
                  {err.case_id ? <strong>{err.case_id}: </strong> : null}
                  <span>{err.message}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    );
  }

  const firstRed = firstRedCell(board.cases);

  const selectedCase =
    board.cases.find((c) => c.id === selectedCell?.caseId) ?? board.cases[0];
  const selectedStepId = selectedCell?.stepId ?? "paper";
  const stepResult = selectedCase ? selectedCase.steps[selectedStepId] : null;

  const targetLink =
    stepResult?.status === "red" &&
    stepResult.reason !== "workspace_missing" &&
    selectedStepId !== "compare"
      ? STEP_LINKS[selectedStepId]
      : null;

  return (
    <div className="readiness-board">
      <div className="readiness-board__summary">
        <span className="readiness-board__summary-count">
          {board.summary.green_cells}/{board.summary.total_cells}
        </span>
        {firstRed && (
          <span className="readiness-board__summary-first-red">
            Primo passo mancante: {firstRed.caseItem.title} · {STEP_LABELS[firstRed.stepId]}
          </span>
        )}
      </div>

      <div className="readiness-board__table-container">
        <table className="readiness-board__table">
          <thead>
            <tr>
              <th>Caso</th>
              <th>Workspace</th>
              {STEP_ORDER.map((stepId) => (
                <th key={stepId}>{STEP_LABELS[stepId]}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {board.cases.map((caseItem) => (
              <tr key={caseItem.id}>
                <td className="readiness-board__case-title">{caseItem.title}</td>
                <td className="readiness-board__case-workspace">{caseItem.workspace}</td>
                {STEP_ORDER.map((stepId) => {
                  const res = caseItem.steps[stepId];
                  const isSelected =
                    selectedCell?.caseId === caseItem.id && selectedCell?.stepId === stepId;
                  return (
                    <td key={stepId}>
                      <button
                        type="button"
                        className="readiness-board__cell-button"
                        data-selected={isSelected ? "true" : undefined}
                        data-status={res?.status}
                        aria-label={`${caseItem.title} - ${STEP_LABELS[stepId]}: ${res?.status === "green" ? "Verde" : "Rosso"}`}
                        onClick={() => setSelectedCell({ caseId: caseItem.id, stepId })}
                      >
                        <span
                          className="readiness-board__light"
                          data-status={res?.status}
                          aria-hidden="true"
                        />
                        <span className="readiness-board__cell-label">{STEP_LABELS[stepId]}</span>
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selectedCase && stepResult && (
        <div className="readiness-board__detail" data-testid="cell-detail">
          <div className="readiness-board__detail-title">
            {selectedCase.title} · {STEP_LABELS[selectedStepId]}
          </div>
          <div className="readiness-board__detail-reason">
            {stepResult.status === "green" ? (
              <p>Passaggio completato.</p>
            ) : (
              <p>{getReasonText(selectedStepId, stepResult.reason)}</p>
            )}
          </div>
          {stepResult.status === "red" && stepResult.missing.length > 0 &&
            (stepResult.reason === "workspace_missing" || STEPS_WITH_NAMED_ITEMS.has(selectedStepId)) && (
            <ul className="readiness-board__missing-list">
              {stepResult.missing.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
          {targetLink && (
            <AppLink
              href={targetLink.path}
              navigate={navigate}
              className="readiness-board__link"
              onClick={() => {
                if (selectedCase.workspace_id) {
                  onWorkspaceChange?.(selectedCase.workspace_id);
                }
              }}
            >
              {targetLink.label}
            </AppLink>
          )}
        </div>
      )}

      {board.errors.length > 0 && (
        <div className="readiness-board__errors">
          <h3>Errori</h3>
          <ul>
            {board.errors.map((err, idx) => (
              <li key={idx}>
                {err.case_id ? <strong>{err.case_id}: </strong> : null}
                <span>{err.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
