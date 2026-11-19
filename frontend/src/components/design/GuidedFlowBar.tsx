import { useCallback, useEffect, useState } from "react";
import {
  createGuidedQuestion,
  getGuidedQuestions,
  updateGuidedQuestion,
  type DesignQuestion,
  type GuidedQuestionsResponse,
  type GuidedStepDefinition,
  type GuidedStepEvaluation,
  type NextAction,
} from "../../api/guidedFlow";
import "./guidedFlowBar.css";

const MODE_STORAGE_KEY = "jarvisos:guided-flow-mode:v1";

export const DEFAULT_STEPS: GuidedStepDefinition[] = [
  { step: "frame", order: 1, label_it: "Inquadra", route: "/memory/project-basis", description_it: "Definisci il quesito tecnico e associa almeno un requisito al progetto." },
  { step: "sources", order: 2, label_it: "Fonti", route: "/memory/literature", description_it: "Raccogli e verifica le fonti bibliografiche e sperimentali." },
  { step: "model", order: 3, label_it: "Modello", route: "/design/project", description_it: "Crea componenti, specifiche o bozze e vincola i parametri alle fonti." },
  { step: "run", order: 4, label_it: "Calcolo", route: "/runs", description_it: "Esegui e verifica i calcoli o le simulazioni di benchmark." },
  { step: "explore", order: 5, label_it: "Esplora", route: "/design/studies", description_it: "Esplora varianti di progetto o definisci studi parametrici." },
  { step: "compare", order: 6, label_it: "Confronta", route: "/development/readiness", description_it: "Confronta i risultati di calcolo con i requisiti e i casi di riferimento." },
  { step: "decide", order: 7, label_it: "Decidi", route: "/memory/project-basis", description_it: "Formalizza la decisione ingegneristica motivando la scelta." },
];

const STATE_LABELS_IT: Record<string, string> = {
  done: "Completato",
  blocked: "Bloccato",
  in_progress: "In corso",
  not_started: "Da iniziare",
};

export default function GuidedFlowBar({
  workspaceId,
  navigate,
}: {
  workspaceId: string | null;
  navigate: (href: string) => void;
}) {
  const [mode, setMode] = useState<"guidato" | "libero">(() => {
    try {
      const stored = window.localStorage.getItem(MODE_STORAGE_KEY);
      return stored === "libero" ? "libero" : "guidato";
    } catch {
      return "guidato";
    }
  });

  const [data, setData] = useState<GuidedQuestionsResponse | null>(null);
  const [selectedQuestionId, setSelectedQuestionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form states
  const [isCreating, setIsCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newWhatChanges, setNewWhatChanges] = useState("");
  const [skipReasonInput, setSkipReasonInput] = useState("");

  const handleModeChange = (nextMode: "guidato" | "libero") => {
    setMode(nextMode);
    try {
      window.localStorage.setItem(MODE_STORAGE_KEY, nextMode);
    } catch {
      // storage unavailable
    }
  };

  const loadData = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await getGuidedQuestions(workspaceId);
      setData(res);
      setSelectedQuestionId((curr) => {
        if (curr && res.questions.some((q) => q.id === curr)) return curr;
        return res.active_question_id;
      });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Errore durante il caricamento del flusso guidato");
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const activeQuestion: DesignQuestion | undefined = data?.questions.find(
    (q) => q.id === selectedQuestionId,
  );

  useEffect(() => {
    if (activeQuestion) {
      setSkipReasonInput(activeQuestion.explore_skip_reason ?? "");
    }
  }, [activeQuestion]);

  const handleCreateQuestion = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!workspaceId || !newTitle.trim()) return;
    try {
      const created = await createGuidedQuestion(workspaceId, {
        title: newTitle.trim(),
        what_changes: newWhatChanges.trim() || null,
      });
      setNewTitle("");
      setNewWhatChanges("");
      setIsCreating(false);
      await loadData();
      setSelectedQuestionId(created.id);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Errore nella creazione della domanda");
    }
  };

  const handleToggleStatus = async (question: DesignQuestion) => {
    if (!workspaceId) return;
    const nextStatus = question.status === "open" ? "closed" : "open";
    try {
      await updateGuidedQuestion(workspaceId, question.id, { status: nextStatus });
      await loadData();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Errore nell'aggiornamento dello stato");
    }
  };

  const handleSaveSkipReason = async () => {
    if (!workspaceId || !activeQuestion) return;
    try {
      await updateGuidedQuestion(workspaceId, activeQuestion.id, {
        explore_skip_reason: skipReasonInput.trim() || null,
      });
      await loadData();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Errore nel salvataggio della motivazione");
    }
  };

  const effectiveSteps: GuidedStepEvaluation[] =
    activeQuestion?.steps ??
    data?.steps ??
    DEFAULT_STEPS.map((s) => ({ ...s, state: "not_started" as const, blocked_items: [] }));
  const effectiveNextAction: NextAction | null =
    activeQuestion ? activeQuestion.next_action : (data?.next_action ?? null);

  return (
    <aside className="guided-flow-bar" aria-label="Flusso guidato di progetto">
      <div className="guided-flow-bar__header">
        <div className="guided-flow-bar__mode-toggle" role="radiogroup" aria-label="Modalità di navigazione">
          <button
            type="button"
            role="radio"
            aria-checked={mode === "guidato"}
            className={`guided-flow-bar__mode-btn ${
              mode === "guidato" ? "guided-flow-bar__mode-btn--active" : ""
            }`}
            onClick={() => handleModeChange("guidato")}
          >
            Guidato
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={mode === "libero"}
            className={`guided-flow-bar__mode-btn ${
              mode === "libero" ? "guided-flow-bar__mode-btn--active" : ""
            }`}
            onClick={() => handleModeChange("libero")}
          >
            Libero
          </button>
        </div>

        {mode === "guidato" && (
          <span className="guided-flow-bar__questions-title">Domande</span>
        )}
      </div>

      {mode === "guidato" && (
        <>
          {error && <div className="guided-flow-bar__error" role="alert">{error}</div>}

          {/* Question management */}
          <div className="guided-flow-bar__questions-section">
            <div className="guided-flow-bar__questions-header">
              <button
                type="button"
                className="guided-flow-bar__new-btn"
                onClick={() => setIsCreating((curr) => !curr)}
              >
                {isCreating ? "Chiudi modulo" : "+ Nuova domanda"}
              </button>
            </div>

            {isCreating && (
              <form className="guided-flow-bar__create-form" onSubmit={handleCreateQuestion}>
                <input
                  type="text"
                  placeholder="Domanda ingegneristica (es. Come ridurre la contropressione?)"
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  autoFocus
                  required
                />
                <input
                  type="text"
                  placeholder="Cosa cambia (opzionale, es. Variazione diametro condotto)"
                  value={newWhatChanges}
                  onChange={(e) => setNewWhatChanges(e.target.value)}
                />
                <div className="guided-flow-bar__create-form-actions">
                  <button type="submit" disabled={!newTitle.trim()}>
                    Crea domanda
                  </button>
                  <button type="button" onClick={() => setIsCreating(false)}>
                    Annulla
                  </button>
                </div>
              </form>
            )}

            {data && data.questions.length > 0 && (
              <div className="guided-flow-bar__questions-list" role="tablist" aria-label="Elenco domande">
                {data.questions.map((q) => {
                  const isSelected = q.id === selectedQuestionId;
                  return (
                    <div
                      key={q.id}
                      className={`guided-flow-bar__question-card ${
                        isSelected ? "guided-flow-bar__question-card--selected" : ""
                      } ${q.status === "closed" ? "guided-flow-bar__question-card--closed" : ""}`}
                      onClick={() => setSelectedQuestionId(q.id)}
                      role="tab"
                      aria-selected={isSelected}
                      tabIndex={0}
                    >
                      <span className="guided-flow-bar__question-title">{q.title}</span>
                      {q.what_changes && (
                        <span className="guided-flow-bar__question-change">
                          {q.what_changes}
                        </span>
                      )}
                      <span
                        className={`guided-flow-bar__status-badge guided-flow-bar__status-badge--${q.status}`}
                      >
                        {q.status === "open" ? "Aperta" : "Chiusa"}
                      </span>
                      <button
                        type="button"
                        className="guided-flow-bar__question-toggle-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          void handleToggleStatus(q);
                        }}
                      >
                        {q.status === "open" ? "Chiudi" : "Riapri"}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}

            {activeQuestion && (
              <div className="guided-flow-bar__skip-reason-box">
                <label htmlFor="skip-explore-input">Esclusione Esplora (opzionale):</label>
                <input
                  id="skip-explore-input"
                  type="text"
                  placeholder="Motivazione per saltare l'esplorazione (es. Variante già validata)"
                  value={skipReasonInput}
                  onChange={(e) => setSkipReasonInput(e.target.value)}
                />
                <button
                  type="button"
                  onClick={handleSaveSkipReason}
                  disabled={skipReasonInput === (activeQuestion.explore_skip_reason ?? "")}
                >
                  Salva motivazione
                </button>
              </div>
            )}
          </div>

          {/* 7-step path bar */}
          <nav className="guided-flow-bar__path-bar" aria-label="Percorso in 7 passi">
            {effectiveSteps.map((step) => {
              const stateClass = `guided-flow-bar__state-badge--${step.state}`;
              return (
                <button
                  key={step.step}
                  type="button"
                  className="guided-flow-bar__step-node"
                  onClick={() => navigate(step.route)}
                  data-step={step.step}
                  data-state={step.state}
                  title={`${step.label_it}: ${step.description_it}`}
                >
                  <div className="guided-flow-bar__step-top">
                    <span className="guided-flow-bar__step-label">{step.label_it}</span>
                    <span className={`guided-flow-bar__state-badge ${stateClass}`}>
                      {STATE_LABELS_IT[step.state] ?? step.state}
                    </span>
                  </div>
                  {step.state === "blocked" && step.blocked_items.length > 0 && (
                    <span className="guided-flow-bar__step-blocked-hint" title={step.blocked_items.join(", ")}>
                      {step.blocked_items[0]}
                    </span>
                  )}
                </button>
              );
            })}
          </nav>

          {/* Prossimo passo card */}
          <div className="guided-flow-bar__next-card" role="region" aria-label="Prossimo passo">
            <div className="guided-flow-bar__next-info">
              <span className="guided-flow-bar__next-header">Prossimo passo</span>
              {effectiveNextAction ? (
                <>
                  <span className="guided-flow-bar__next-title">
                    {effectiveNextAction.title_it}
                  </span>
                  <p className="guided-flow-bar__next-reason">
                    {effectiveNextAction.reason_it}
                  </p>
                </>
              ) : (
                <span className="guided-flow-bar__next-title">
                  Tutti i passi completati per questa domanda.
                </span>
              )}
            </div>
            {effectiveNextAction && (
              <button
                type="button"
                className="guided-flow-bar__next-btn"
                onClick={() => navigate(effectiveNextAction.route)}
              >
                {effectiveNextAction.action_label_it}
              </button>
            )}
          </div>
        </>
      )}
    </aside>
  );
}
