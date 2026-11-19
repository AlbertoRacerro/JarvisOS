import { useEffect, useState } from "react";
import type { Navigate } from "../app/AppLink";
import AppLink from "../app/AppLink";
import {
  getReadiness,
  createCaseVerdict,
  createDecision,
  listCaseVerdicts,
  updateCaseVerdict,
  type CaseResult,
  type CaseVerdictRead,
  type ReadinessBoardRead,
} from "../api/readinessBoard";

export const READINESS_TITLE = "Prontezza";
export const DECISION_NEEDS_VERDICT = "Prima registra e accetta il verdetto nel passo Confronto.";
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
export const REASON_NOT_DECLARED = "Il caso non dichiara un valore pubblicato da confrontare";
export const REASON_UNIT_MISMATCH = "Le unità del risultato e del valore pubblicato non sono confrontabili";
export const REASON_OUT_OF_TOLERANCE = "Il risultato è fuori dalla tolleranza del caso";

export const MISSING_RECORD_SENTENCES: Record<StepId, string> = {
  paper: "Manca la fonte di letteratura o il file allegato:",
  facts: "Mancano dati accettati e verificati per:",
  bound: "Mancano parametri legati ai dati accettati della fonte:",
  run: "Manca un risultato del banco di prova con esito utilizzabile nel workspace del caso.",
  compare: "Nessun risultato utilizzabile riporta la grandezza confrontata:",
  decide: "Manca una decisione accettata collegata al verdetto di questo caso.",
  answer: "Manca una misura della qualità della ricerca con esito utilizzabile nel workspace del caso."
};

// Missing items are shown only where they are the engineer's own names (source title, quantities, parameters, the
// workspace, and the compared quantity); for the other steps the board names a record kind, which the sentence already says in words.
const STEPS_WITH_NAMED_ITEMS: ReadonlySet<StepId> = new Set(["paper", "facts", "bound", "compare"]);

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
  compare: { path: "/runs", label: "Apri Runs" },
  answer: { path: "/memory/search", label: "Apri Ricerca" }
};

export function getReasonText(step: StepId, reason: string | null): string {
  if (reason === "workspace_missing") return REASON_WORKSPACE_MISSING;
  if (reason === "not_declared") return REASON_NOT_DECLARED;
  if (reason === "unit_mismatch") return REASON_UNIT_MISMATCH;
  if (reason === "out_of_tolerance") return REASON_OUT_OF_TOLERANCE;
  if (reason === "missing_record") {
    return MISSING_RECORD_SENTENCES[step] ?? "Elemento mancante per questo passaggio:";
  }
  return "";
}

// Deviations below one part in a million are floating-point noise from the unit conversion, shown as zero.
export function formatPercentSigFigs(value: number, sigFigs: number = 2): string {
  const pct = value * 100;
  if (!Number.isFinite(pct)) return "—";
  if (Math.abs(value) < 1e-6) return "0%";
  const text = pct.toPrecision(sigFigs);
  return `${text.includes("e") ? Number(text) : text}%`;
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
  const [workspaceVerdicts, setWorkspaceVerdicts] = useState<Record<string, CaseVerdictRead[]>>({});
  const [rationaleInput, setRationaleInput] = useState<string>("");
  const [verdictActionLoading, setVerdictActionLoading] = useState<boolean>(false);
  const [verdictActionError, setVerdictActionError] = useState<string | null>(null);
  const [decisionTitle, setDecisionTitle] = useState<string>("");
  const [decisionText, setDecisionText] = useState<string>("");
  const [decisionRationale, setDecisionRationale] = useState<string>("");
  const [decisionLoading, setDecisionLoading] = useState<boolean>(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);

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

  const currentCase =
    board && board.cases.length > 0
      ? (board.cases.find((c) => c.id === selectedCell?.caseId) ?? board.cases[0])
      : null;

  useEffect(() => {
    const wsId = currentCase?.workspace_id;
    if (!wsId) return;
    let alive = true;
    listCaseVerdicts(wsId)
      .then((verdicts) => {
        if (!alive) return;
        setWorkspaceVerdicts((prev) => ({ ...prev, [wsId]: verdicts }));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [currentCase?.workspace_id]);

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

  const wsVerdicts = selectedCase?.workspace_id ? (workspaceVerdicts[selectedCase.workspace_id] ?? []) : [];
  const matchedStoredVerdict =
    wsVerdicts.find(
      (v) =>
        v.case_id === selectedCase?.id &&
        v.run_id === stepResult?.comparison?.run_id &&
        v.status !== "superseded",
    ) ??
    (stepResult?.comparison &&
    stepResult.stored_verdict?.run_id === stepResult.comparison.run_id &&
    stepResult.stored_verdict.status !== "superseded"
      ? stepResult.stored_verdict
      : null);

  const acceptedCaseVerdict =
    wsVerdicts.find((v) => v.case_id === selectedCase?.id && v.status === "accepted") ?? null;

  const isCompareLinked =
    selectedStepId === "compare" &&
    stepResult?.status === "red" &&
    (stepResult.reason === "missing_record" ||
      stepResult.reason === "out_of_tolerance" ||
      stepResult.reason === "unit_mismatch");

  const isOtherLinked =
    selectedStepId !== "compare" &&
    stepResult?.status === "red" &&
    stepResult.reason !== "workspace_missing";

  const targetLink = isCompareLinked || isOtherLinked ? STEP_LINKS[selectedStepId] : null;

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
          {stepResult.comparison && (
            <div className="readiness-board__comparison">
              <table className="readiness-board__comparison-table">
                <tbody>
                  <tr>
                    <th>Calcolato</th>
                    <td>
                      {stepResult.comparison.computed_in_published_unit !== null &&
                      stepResult.comparison.computed.unit !== stepResult.comparison.published.unit
                        ? `${stepResult.comparison.computed.value} ${stepResult.comparison.computed.unit} (${Number(stepResult.comparison.computed_in_published_unit.toPrecision(6))} ${stepResult.comparison.published.unit})`
                        : `${stepResult.comparison.computed.value} ${stepResult.comparison.computed.unit}`}
                    </td>
                  </tr>
                  <tr>
                    <th>Pubblicato</th>
                    <td>{`${stepResult.comparison.published.value} ${stepResult.comparison.published.unit}`}</td>
                  </tr>
                  <tr>
                    <th>Scarto</th>
                    <td>
                      {stepResult.comparison.deviation !== null
                        ? formatPercentSigFigs(stepResult.comparison.deviation, 2)
                        : "—"}
                    </td>
                  </tr>
                  <tr>
                    <th>Tolleranza</th>
                    <td>{`${Number((stepResult.comparison.rel_tol * 100).toPrecision(4))}%`}</td>
                  </tr>
                  <tr>
                    <th>Run</th>
                    <td>{stepResult.comparison.run_id}</td>
                  </tr>
                </tbody>
              </table>

              <div className="readiness-board__verdict-panel">
                {matchedStoredVerdict ? (
                  <>
                    <div className="readiness-board__verdict-row">
                      <span
                        className="readiness-board__verdict-badge"
                        data-verdict={matchedStoredVerdict.verdict}
                      >
                        {matchedStoredVerdict.verdict === "pass" ? "Entro tolleranza" : "Fuori tolleranza"}
                      </span>
                      <span
                        className="readiness-board__verdict-status"
                        data-status={matchedStoredVerdict.status}
                      >
                        {matchedStoredVerdict.status === "accepted" ? "Accettato" : "Registrato"}
                      </span>
                    </div>

                    {matchedStoredVerdict.status === "recorded" ? (
                      <div className="readiness-board__verdict-row">
                        <input
                          type="text"
                          className="readiness-board__rationale-input"
                          placeholder="Motivazione (opzionale)"
                          value={rationaleInput}
                          onChange={(e) => setRationaleInput(e.target.value)}
                        />
                        <button
                          type="button"
                          className="readiness-board__accept-verdict-button"
                          disabled={verdictActionLoading}
                          onClick={async () => {
                            if (!selectedCase?.workspace_id) return;
                            setVerdictActionLoading(true);
                            setVerdictActionError(null);
                            try {
                              const updated = await updateCaseVerdict(
                                selectedCase.workspace_id,
                                matchedStoredVerdict.id,
                                {
                                  status: "accepted",
                                  rationale: rationaleInput.trim() || undefined,
                                },
                              );
                              setWorkspaceVerdicts((prev) => {
                                const current = prev[selectedCase.workspace_id!] ?? [];
                                const filtered = current.filter((v) => v.id !== updated.id);
                                return {
                                  ...prev,
                                  [selectedCase.workspace_id!]: [updated, ...filtered],
                                };
                              });
                            } catch {
                              setVerdictActionError("Impossibile accettare il verdetto.");
                            } finally {
                              setVerdictActionLoading(false);
                            }
                          }}
                        >
                          Accetta verdetto
                        </button>
                      </div>
                    ) : matchedStoredVerdict.rationale ? (
                      <div className="readiness-board__verdict-rationale-display">
                        Motivazione: {matchedStoredVerdict.rationale}
                      </div>
                    ) : null}
                  </>
                ) : (
                  <button
                    type="button"
                    className="readiness-board__record-verdict-button"
                    disabled={verdictActionLoading}
                    onClick={async () => {
                      if (!selectedCase?.workspace_id || !stepResult?.comparison) return;
                      setVerdictActionLoading(true);
                      setVerdictActionError(null);
                      try {
                        const created = await createCaseVerdict(selectedCase.workspace_id, {
                          case_id: selectedCase.id,
                          run_id: stepResult.comparison.run_id,
                          quantity: stepResult.comparison.quantity ?? "",
                          published_value: stepResult.comparison.published.value,
                          published_unit: stepResult.comparison.published.unit,
                          rel_tol: stepResult.comparison.rel_tol,
                        });
                        setWorkspaceVerdicts((prev) => {
                          const current = prev[selectedCase.workspace_id!] ?? [];
                          return {
                            ...prev,
                            [selectedCase.workspace_id!]: [created, ...current],
                          };
                        });
                        setRationaleInput("");
                      } catch {
                        setVerdictActionError("Impossibile registrare il verdetto.");
                      } finally {
                        setVerdictActionLoading(false);
                      }
                    }}
                  >
                    Registra verdetto
                  </button>
                )}
                {verdictActionError && (
                  <p className="readiness-board__verdict-error">{verdictActionError}</p>
                )}
              </div>
            </div>
          )}
          {selectedStepId === "decide" && stepResult.status === "red" && stepResult.reason !== "workspace_missing" && (
            <div className="readiness-board__decision-panel">
              {acceptedCaseVerdict ? (
                <form
                  className="readiness-board__decision-form"
                  onSubmit={async (event) => {
                    event.preventDefault();
                    if (!selectedCase?.workspace_id || !decisionTitle.trim() || !decisionText.trim()) return;
                    setDecisionLoading(true);
                    setDecisionError(null);
                    try {
                      await createDecision(selectedCase.workspace_id, {
                        title: decisionTitle.trim(),
                        decision_text: decisionText.trim(),
                        rationale: decisionRationale.trim() || null,
                        status: "accepted",
                        case_verdict_id: acceptedCaseVerdict.id,
                      });
                      setBoard(await getReadiness());
                      setDecisionTitle("");
                      setDecisionText("");
                      setDecisionRationale("");
                    } catch {
                      setDecisionError("Impossibile registrare la decisione.");
                    } finally {
                      setDecisionLoading(false);
                    }
                  }}
                >
                  <label>
                    Titolo
                    <input type="text" value={decisionTitle} onChange={(e) => setDecisionTitle(e.target.value)} />
                  </label>
                  <label>
                    Decisione
                    <textarea value={decisionText} onChange={(e) => setDecisionText(e.target.value)} />
                  </label>
                  <label>
                    Motivazione (opzionale)
                    <textarea value={decisionRationale} onChange={(e) => setDecisionRationale(e.target.value)} />
                  </label>
                  <button
                    type="submit"
                    className="readiness-board__record-decision-button"
                    disabled={decisionLoading || !decisionTitle.trim() || !decisionText.trim()}
                  >
                    Registra decisione
                  </button>
                  {decisionError && <p className="readiness-board__verdict-error">{decisionError}</p>}
                </form>
              ) : (
                <p className="readiness-board__decision-hint">{DECISION_NEEDS_VERDICT}</p>
              )}
            </div>
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
