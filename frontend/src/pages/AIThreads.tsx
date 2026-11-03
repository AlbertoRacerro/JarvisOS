import { useEffect, useRef, useState } from "react";

import {
  createThread,
  getThread,
  listThreads,
  submitThreadInteraction,
  type ThreadDetail,
  type ThreadSummary
} from "../api/threads";
import {
  chooseThread,
  contextMatches,
  detailContext,
  initialThreadState,
  listContext,
  orderThreads,
  submitContext,
  withThread,
  withWorkspace,
  type ThreadState
} from "../components/threads/threadState";

type Props = Readonly<{ workspaceId: string | null }>;
type PendingSubmit = Readonly<{ threadId: string; prompt: string; requestId: string }>;

const makeRequestId = () => crypto.randomUUID();

export default function AIThreads({ workspaceId }: Props) {
  const stateRef = useRef<ThreadState>(initialThreadState());
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const pendingSubmitRef = useRef<PendingSubmit | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ThreadDetail | null>(null);
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    stateRef.current = withWorkspace(stateRef.current, workspaceId);
    pendingSubmitRef.current = null;
    setThreads([]);
    setSelectedId(null);
    setDetail(null);
    setSubmitting(false);
    setError(null);
    if (!workspaceId) return;

    const captured = listContext(stateRef.current);
    setLoading(true);
    void listThreads(workspaceId)
      .then((items) => {
        if (!contextMatches(listContext(stateRef.current), captured)) return;
        const ordered = orderThreads(items);
        setThreads(ordered);
        const next = chooseThread(ordered, null);
        stateRef.current = withThread(stateRef.current, next);
        setSelectedId(next);
      })
      .catch(() => {
        if (contextMatches(listContext(stateRef.current), captured)) setError("Thread list could not be loaded.");
      })
      .finally(() => {
        if (contextMatches(listContext(stateRef.current), captured)) setLoading(false);
      });
  }, [workspaceId]);

  useEffect(() => {
    if (!workspaceId || !selectedId) {
      setDetail(null);
      return;
    }
    if (stateRef.current.selectedThreadId !== selectedId) stateRef.current = withThread(stateRef.current, selectedId);
    const captured = detailContext(stateRef.current);
    setLoading(true);
    setError(null);
    void getThread(workspaceId, selectedId)
      .then((value) => {
        if (contextMatches(detailContext(stateRef.current), captured)) setDetail(value);
      })
      .catch(() => {
        if (contextMatches(detailContext(stateRef.current), captured)) {
          setDetail(null);
          setError("Selected thread is unavailable.");
        }
      })
      .finally(() => {
        if (contextMatches(detailContext(stateRef.current), captured)) setLoading(false);
      });
  }, [workspaceId, selectedId]);

  const selectThread = (threadId: string) => {
    if (stateRef.current.selectedThreadId === threadId) return;
    stateRef.current = withThread(stateRef.current, threadId);
    pendingSubmitRef.current = null;
    setSubmitting(false);
    setSelectedId(threadId);
    setDetail(null);
    setError(null);
  };

  const onCreate = async () => {
    if (!workspaceId || loading) return;
    const captured = listContext(stateRef.current);
    setLoading(true);
    setError(null);
    try {
      const created = await createThread(workspaceId);
      if (!contextMatches(listContext(stateRef.current), captured)) return;
      const nextThreads = orderThreads([created, ...threads.filter((thread) => thread.id !== created.id)]);
      setThreads(nextThreads);
      stateRef.current = withThread(stateRef.current, created.id);
      pendingSubmitRef.current = null;
      setSubmitting(false);
      setSelectedId(created.id);
      requestAnimationFrame(() => promptRef.current?.focus());
    } catch {
      if (contextMatches(listContext(stateRef.current), captured)) setError("Thread could not be created.");
    } finally {
      if (contextMatches(listContext(stateRef.current), captured)) setLoading(false);
    }
  };

  const onSubmit = async () => {
    if (!workspaceId || !selectedId || submitting || !prompt.trim()) return;
    const pending = pendingSubmitRef.current;
    const requestId = pending && pending.threadId === selectedId && pending.prompt === prompt
      ? pending.requestId
      : makeRequestId();
    pendingSubmitRef.current = { threadId: selectedId, prompt, requestId };
    const captured = submitContext(stateRef.current);
    const text = prompt;
    setSubmitting(true);
    setError(null);
    try {
      await submitThreadInteraction(workspaceId, selectedId, requestId, text);
      if (!contextMatches(submitContext(stateRef.current), captured)) return;
      const refreshed = await getThread(workspaceId, selectedId);
      if (!contextMatches(submitContext(stateRef.current), captured)) return;
      setDetail(refreshed);
      pendingSubmitRef.current = null;
      setPrompt("");
      requestAnimationFrame(() => promptRef.current?.focus());
    } catch {
      if (contextMatches(submitContext(stateRef.current), captured)) {
        setError("Interaction was not submitted or its durable result could not be confirmed. Retrying the unchanged prompt reuses the same request id.");
        requestAnimationFrame(() => promptRef.current?.focus());
      }
    } finally {
      if (contextMatches(submitContext(stateRef.current), captured)) setSubmitting(false);
    }
  };

  if (!workspaceId) {
    return <section className="ai-threads" aria-labelledby="ai-threads-title"><h1 id="ai-threads-title">AI Threads</h1><p>Select a workspace on an existing application surface before opening AI Threads.</p></section>;
  }

  return (
    <section className="ai-threads" aria-labelledby="ai-threads-title" aria-busy={loading || submitting}>
      <header className="ai-threads__header"><div><p className="eyebrow">Inspection surface</p><h1 id="ai-threads-title">AI Threads</h1></div><button type="button" onClick={() => void onCreate()} disabled={loading}>New thread</button></header>
      {error ? <p className="ai-threads__status" role="status">{error}</p> : null}
      <div className="ai-threads__workbench">
        <nav className="ai-threads__list" aria-label="AI threads">
          {loading && threads.length === 0 ? <p>Loading threads…</p> : null}
          {!loading && threads.length === 0 ? <p>No threads yet.</p> : null}
          {threads.map((thread) => <button key={thread.id} type="button" aria-pressed={selectedId === thread.id} onClick={() => selectThread(thread.id)}><strong>{thread.title || "Untitled thread"}</strong><span>{thread.id}</span></button>)}
        </nav>
        <main className="ai-threads__transcript">
          {!selectedId ? <p>Select or create a thread.</p> : null}
          {detail?.has_older ? <p className="ai-threads__status">Older interactions are not shown in this bounded view.</p> : null}
          {detail?.interactions.map((interaction) => (
            <article className="ai-threads__interaction" key={interaction.id}>
              <div><span className="ai-threads__role">You</span><p>{interaction.user_text}</p></div>
              <div><span className="ai-threads__role">Jarvis advisory</span><p>{interaction.assistant_text ?? "No durable assistant snapshot."}</p></div>
              <dl><div><dt>Flow</dt><dd>{interaction.flow_id}</dd></div><div><dt>Canonical state</dt><dd>{interaction.flow_state}</dd></div><div><dt>Persistence</dt><dd>{interaction.persistence_state}</dd></div><div><dt>Attempts</dt><dd>{interaction.attempt_count}</dd></div><div><dt>Proposals</dt><dd>{interaction.proposal_count}{interaction.proposals_truncated ? "+" : ""}</dd></div></dl>
              {interaction.persistence_error ? <p className="ai-threads__status">Persistence diagnostic: {interaction.persistence_error}</p> : null}
              {interaction.assistant_text_truncated ? <p className="ai-threads__status">Assistant snapshot truncated; canonical flow evidence remains authoritative.</p> : null}
            </article>
          ))}
          {selectedId ? <div className="ai-threads__composer"><label htmlFor="ai-thread-prompt">Prompt</label><textarea id="ai-thread-prompt" ref={promptRef} value={prompt} maxLength={12000} onChange={(event) => setPrompt(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void onSubmit(); } }} disabled={submitting} /><button type="button" disabled={submitting || !prompt.trim()} onClick={() => void onSubmit()}>{submitting ? "Submitting…" : "Submit"}</button><small>Enter submits. Shift+Enter adds a line.</small></div> : null}
        </main>
      </div>
    </section>
  );
}
