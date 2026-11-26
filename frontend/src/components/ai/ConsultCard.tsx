import { useEffect, useState } from "react";
import type { ConsultBlock } from "../../api/threads";
import {
  approveConsult,
  editConsult,
  rejectConsult,
  fetchConsultAnswer,
  submitConsultFeedback,
  fetchConsultFeedback,
} from "../../api/consult";

export type ConsultCardProps = {
  block: ConsultBlock;
  onAction?: () => void;
};

export default function ConsultCard({ block, onAction }: ConsultCardProps) {
  const [editing, setEditing] = useState(false);
  const [question, setQuestion] = useState("");
  const [context, setContext] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [verifiedAnswer, setVerifiedAnswer] = useState<string | null>(block.answer_text);
  const [verdict, setVerdict] = useState<"accepted" | "rejected" | null>(block.feedback_verdict || null);
  const [feedbackSaving, setFeedbackSaving] = useState(false);

  // If byte_identical is false and answer_text exists, fetch stored answer by id
  useEffect(() => {
    if (block.state === "sent" && !block.byte_identical && block.consult_id) {
      fetchConsultAnswer(block.consult_id)
        .then((res) => {
          if (res.text) {
            setVerifiedAnswer(res.text);
          }
        })
        .catch(() => {
          // Keep current answer_text if fetch fails
        });
    } else {
      setVerifiedAnswer(block.answer_text);
    }
  }, [block.consult_id, block.state, block.byte_identical, block.answer_text]);

  useEffect(() => {
    if (block.feedback_verdict) {
      setVerdict(block.feedback_verdict);
    } else if (block.state === "sent" && block.consult_id) {
      fetchConsultFeedback(block.consult_id)
        .then((res) => {
          if (res.verdict) {
            setVerdict(res.verdict);
          }
        })
        .catch(() => {});
    }
  }, [block.consult_id, block.state, block.feedback_verdict]);

  const handleFeedback = async (v: "accepted" | "rejected") => {
    setFeedbackSaving(true);
    try {
      setVerdict(v);
      await submitConsultFeedback(block.consult_id, v);
      onAction?.();
    } catch {
      // Keep optimistic verdict or handle silently
    } finally {
      setFeedbackSaving(false);
    }
  };

  const handleApprove = async () => {
    setSubmitting(true);
    setActionError(null);
    try {
      const res = await approveConsult(block.consult_id, block.packet_sha256);
      if (res.status === "refused") {
        setActionError(res.reason || res.code || "Approvazione rifiutata");
      } else {
        onAction?.();
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Errore durante l'approvazione");
    } finally {
      setSubmitting(false);
    }
  };

  const handleReject = async () => {
    setSubmitting(true);
    setActionError(null);
    try {
      const res = await rejectConsult(block.consult_id);
      if (res.status === "refused") {
        setActionError(res.reason || res.code || "Rifiuto non riuscito");
      } else {
        onAction?.();
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Errore durante il rifiuto");
    } finally {
      setSubmitting(false);
    }
  };

  const handleSaveEdit = async () => {
    setSubmitting(true);
    setActionError(null);
    try {
      const res = await editConsult(block.consult_id, question, context);
      if (res.status === "refused" || res.status === "rewrite_required") {
        setActionError(res.reason || res.code || "Modifica non valida");
      } else {
        setEditing(false);
        onAction?.();
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Errore durante il salvataggio");
    } finally {
      setSubmitting(false);
    }
  };

  const startEditing = () => {
    let q = "";
    let ctx = "";
    if (block.packet_text) {
      const qIdx = block.packet_text.indexOf("Question:\n");
      const cIdx = block.packet_text.indexOf("Context:\n");
      if (qIdx !== -1) {
        q = cIdx !== -1
          ? block.packet_text.slice(qIdx + 10, cIdx).trim()
          : block.packet_text.slice(qIdx + 10).trim();
      }
      if (cIdx !== -1) {
        ctx = block.packet_text.slice(cIdx + 9).trim();
      }
    }
    setQuestion(q || block.packet_text || "");
    setContext(ctx);
    setEditing(true);
    setActionError(null);
  };

  // 1. Pending (awaiting_approval)
  if (block.state === "awaiting_approval") {
    const briefVer = block.brief_sha256 ? block.brief_sha256.slice(0, 12) : "—";
    return (
      <div className="consult-card consult-card--pending" data-consult-card={block.consult_id} data-consult-state="awaiting_approval">
        <div className="consult-card__header">
          <span className="consult-card__title">Consulente · In attesa di approvazione</span>
          <span className="consult-card__tag">{block.kind}</span>
        </div>
        <div className="consult-card__meta">
          <span>Brief: <code>{briefVer}</code></span>
          {block.modules.length > 0 && <span>Moduli: {block.modules.join(", ")}</span>}
        </div>
        {editing ? (
          <div className="consult-card__edit-form">
            <label>
              <span>Domanda</span>
              <textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                rows={4}
                disabled={submitting}
              />
            </label>
            <label>
              <span>Contesto (opzionale)</span>
              <textarea
                value={context}
                onChange={(e) => setContext(e.target.value)}
                rows={4}
                disabled={submitting}
              />
            </label>
            {actionError && <p className="consult-card__error">{actionError}</p>}
            <div className="consult-card__actions">
              <button
                type="button"
                className="consult-btn consult-btn--save"
                onClick={handleSaveEdit}
                disabled={submitting || !question.trim()}
              >
                Salva modifiche
              </button>
              <button
                type="button"
                className="consult-btn consult-btn--cancel"
                onClick={() => setEditing(false)}
                disabled={submitting}
              >
                Annulla
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="consult-card__packet">
              <pre>{block.packet_text}</pre>
            </div>
            {actionError && <p className="consult-card__error">{actionError}</p>}
            <div className="consult-card__actions">
              <button
                type="button"
                className="consult-btn consult-btn--approve"
                onClick={handleApprove}
                disabled={submitting}
              >
                Approva
              </button>
              <button
                type="button"
                className="consult-btn consult-btn--edit"
                onClick={startEditing}
                disabled={submitting}
              >
                Modifica
              </button>
              <button
                type="button"
                className="consult-btn consult-btn--reject"
                onClick={handleReject}
                disabled={submitting}
              >
                Rifiuta
              </button>
            </div>
          </>
        )}
      </div>
    );
  }

  // 2. Answered (sent)
  if (block.state === "sent") {
    const rawTime = block.answered_at || block.created_at;
    let timeStr = "";
    if (rawTime) {
      try {
        const d = new Date(rawTime);
        timeStr = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      } catch {
        timeStr = "";
      }
    }
    const modelName = block.model_id || "Consulente";
    const headerLabel = `${modelName} dice · ${timeStr}`;
    const swapTimeStr = block.swap_timing && block.swap_timing.total_swap_s != null
      ? `tempo di swap: ${block.swap_timing.total_swap_s.toFixed(2)}s`
      : "";

    return (
      <div className="consult-card consult-card--answered" data-consult-card={block.consult_id} data-consult-state="sent">
        <div className="consult-card__header">
          <span className="consult-card__author">{headerLabel}</span>
          {swapTimeStr ? <span className="consult-card__swap" data-testid="consult-swap-time">{swapTimeStr}</span> : null}
        </div>
        <div className="consult-card__answer">
          <pre>{verifiedAnswer || "Nessuna risposta disponibile."}</pre>
        </div>
        <details className="consult-card__details">
          <summary>domanda inviata</summary>
          <pre>{block.packet_text}</pre>
        </details>
        <div className="consult-card__feedback" data-testid="consult-feedback">
          <button
            type="button"
            className={`consult-btn consult-btn--feedback consult-btn--useful ${verdict === "accepted" ? "consult-btn--active" : ""}`}
            data-testid="consult-feedback-useful"
            data-feedback-verdict="accepted"
            onClick={() => handleFeedback("accepted")}
            disabled={feedbackSaving}
          >
            Utile
          </button>
          <button
            type="button"
            className={`consult-btn consult-btn--feedback consult-btn--not-useful ${verdict === "rejected" ? "consult-btn--active" : ""}`}
            data-testid="consult-feedback-not-useful"
            data-feedback-verdict="rejected"
            onClick={() => handleFeedback("rejected")}
            disabled={feedbackSaving}
          >
            Non utile
          </button>
          {verdict && (
            <span className="consult-card__feedback-recorded" data-testid="consult-recorded-verdict">
              {verdict === "accepted" ? "registrato: Utile" : "registrato: Non utile"}
            </span>
          )}
        </div>
      </div>
    );
  }

  // 3. Rejected
  if (block.state === "rejected") {
    return (
      <div className="consult-card consult-card--rejected" data-consult-card={block.consult_id} data-consult-state="rejected">
        <div className="consult-card__header">
          <span className="consult-card__title">Consulente · Rifiutata</span>
        </div>
        {block.packet_text && (
          <details className="consult-card__details">
            <summary>domanda inviata</summary>
            <pre>{block.packet_text}</pre>
          </details>
        )}
      </div>
    );
  }

  // 4. Refused / Failed / Other
  return (
    <div className="consult-card consult-card--refused" data-consult-card={block.consult_id} data-consult-state={block.state}>
      <div className="consult-card__header">
        <span className="consult-card__title">Consulente · {block.state}</span>
      </div>
      {block.packet_text && (
        <details className="consult-card__details">
          <summary>domanda inviata</summary>
          <pre>{block.packet_text}</pre>
        </details>
      )}
    </div>
  );
}
