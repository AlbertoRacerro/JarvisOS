import { type Ref, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  CodingRequestError,
  controlRelaySupervisor,
  answerRelayQuestion,
  type RelayControlAction,
  readRelayMonitor,
  readRelayMonitorTimeline,
  type RelayMonitorItem,
  type RelayMonitorSnapshot,
} from "../../api/coding";

// The server bounds and checks the owner snapshot, timeline and maintainer actions.
const POLL_MS = 3000;
const PAGES_PER_POLL = 5;
const ITEMS_KEPT = 600;

const STATE_LABELS: Record<string, string> = {
  running: "Running",
  queued: "Queued",
  checkpointed: "Checkpointed",
  verified_complete: "Verified complete",
  held: "Held",
  paused: "Paused",
  waiting: "Waiting",
  idle: "Idle",
  stopped: "Stopped",
  unknown: "Unknown",
};

// The terminal marks (⏺, ⎿) are missing from common UI fonts; the browser shows the same kinds with plain glyphs.
const KIND_MARKS: Record<RelayMonitorItem["kind"], string> = {
  message: "●", tool: "▸", output: "↳", edit: "▸", error: "✗", done: "✓", turn: "──", mission: "──", gap: "…",
};

const KIND_LABELS: Partial<Record<RelayMonitorItem["kind"], string>> = {
  done: "Turn finished:",
  error: "Error:",
};

function words(code: string): string {
  return code.replace(/[_|]/g, " ");
}

export function RelayMonitorView({ snapshot, items, error, connected, timelineRef, busyAction, actionFeedback, onControl, onAnswerQuestion, otherDrafts, onOtherDraftChange }: Readonly<{
  snapshot: RelayMonitorSnapshot | null;
  items: readonly RelayMonitorItem[];
  error: string | null;
  connected: boolean;
  timelineRef?: Ref<HTMLOListElement>;
  busyAction?: string | null;
  actionFeedback?: string | null;
  onControl?: (action: RelayControlAction) => void;
  onAnswerQuestion?: (questionId: string, answer: { option_index?: number; custom_text?: string }) => void;
  otherDrafts?: Readonly<Record<string, string>>;
  onOtherDraftChange?: (questionId: string, text: string) => void;
}>) {
  const state = snapshot?.state ?? "unknown";
  const mission = snapshot?.mission ?? null;
  const turn = snapshot?.turn ?? {};
  const alerts = [...(snapshot?.incidents ?? []), ...(snapshot?.problems ?? [])];
  return <section className="coding-control-section coding-relay-monitor" aria-label="Relay owner">
    <header>
      <h3>Relay owner</h3>
      <span className="coding-state" data-state={state}>{STATE_LABELS[state] ?? words(state)}</span>
      {snapshot?.reason ? <span>{words(snapshot.reason)}</span> : null}
      <span>{snapshot?.owner === true ? "owner process alive" : snapshot?.owner === false ? "owner process not running" : "owner liveness unknown"}{connected ? "" : " · reconnecting"}</span>
    </header>
    {onControl ? <div className="coding-relay-controls" role="group" aria-label="Supervisor controls">
      <span>{snapshot?.stop ? "Graceful stop requested" : snapshot?.roadmap?.paused ? `Paused (${words(snapshot.roadmap.paused)})` : snapshot?.roadmap?.enabled ? `Roadmap cycle ${snapshot.roadmap.cycle ?? "—"} · active` : "Roadmap unavailable or disabled"}</span>
      <button type="button" disabled={busyAction != null || !snapshot?.roadmap?.enabled || snapshot.roadmap.paused === "maintainer_stop"} onClick={() => onControl("pause")}>Pause supervisor</button>
      <button type="button" disabled={busyAction != null || (!snapshot?.roadmap?.paused && !snapshot?.stop)} onClick={() => onControl("resume")}>Resume supervisor</button>
      <button type="button" disabled={busyAction != null || !snapshot || snapshot.stop === true} onClick={() => onControl("graceful_restart")}>Graceful restart</button>
      {actionFeedback ? <span role="status">{actionFeedback}</span> : null}
    </div> : null}
    {snapshot?.open_questions?.length ? <div className="coding-relay-questions" role="region" aria-label="Open maintainer questions">{snapshot.open_questions.map((question) => <div key={question.id}>
      <p>{question.prompt}</p>
      {question.options.map((option) => <div key={option.index}>{onAnswerQuestion ? <button type="button" disabled={busyAction != null} onClick={() => onAnswerQuestion(question.id, { option_index: option.index })}>{option.label}{option.recommended ? " (consigliata)" : ""}</button> : <span>{option.label}</span>}{option.consequence ? <span> · {option.consequence}</span> : null}</div>)}
      {onAnswerQuestion ? <div><label>Altro <input maxLength={400} value={otherDrafts?.[question.id] ?? ""} onChange={(event) => onOtherDraftChange?.(question.id, event.target.value)} /></label><button type="button" disabled={busyAction != null || !(otherDrafts?.[question.id] ?? "").trim()} onClick={() => onAnswerQuestion(question.id, { custom_text: otherDrafts?.[question.id] })}>Invia</button></div> : null}
    </div>)}</div> : null}
    {snapshot === null && !error ? <p className="coding-control-note">Reading owner state…</p> : null}
    {error ? <p className="coding-control-empty" role="status">Owner state unavailable: {words(error)}</p> : null}
    {snapshot && state === "unknown" ? <p className="coding-control-empty" role="status">The owner state cannot be determined{snapshot.reason ? ` (${words(snapshot.reason)})` : ""}. Nothing is shown as running.</p> : null}
    {mission ? <dl className="coding-relay-summary">
      <dt>Mission</dt><dd><code>{mission.id}</code>{mission.task ? ` · task ${mission.task}` : ""}{mission.agent ? ` · ${mission.agent}` : ""}</dd>
      {mission.label ? <><dt>Task</dt><dd>{mission.label}</dd></> : null}
      {turn.turn ? <><dt>Turn</dt><dd>{turn.turn}{turn.status ? ` · ${words(turn.status)}` : turn.running ? " · in progress" : ""}</dd></> : null}
      {turn.next_step ? <><dt>Next step</dt><dd>{turn.next_step}</dd></> : null}
      {snapshot?.question ? <><dt>Open question</dt><dd>{snapshot.question}</dd></> : null}
    </dl> : snapshot && state !== "unknown" ? <p className="coding-control-note">No mission is active{snapshot.roadmap?.enabled ? " · roadmap mode on" : ""}.</p> : null}
    {alerts.length ? <ul className="coding-relay-alerts">{alerts.map((code) => <li key={code}>{words(code)}</li>)}</ul> : null}
    {items.length ? <ol className="coding-relay-timeline" aria-label="Owner timeline" ref={timelineRef}>{items.map((item) => {
      const line = <><span className="coding-relay-mark" aria-hidden="true">{item.failed && item.kind === "output" ? "✗" : KIND_MARKS[item.kind] ?? item.mark}</span> {KIND_LABELS[item.kind] ? `${KIND_LABELS[item.kind]} ` : ""}{item.text}</>;
      return <li key={`${item.session}/${item.turn}/${item.offset}/${item.kind}/${item.text.slice(0, 40)}`} data-kind={item.kind} data-failed={item.failed ? "true" : undefined}>
        {item.detail ? <details><summary>{line}</summary><pre>{item.detail}</pre></details> : line}
      </li>;
    })}</ol> : snapshot && !error ? <p className="coding-control-note">No timeline events yet.</p> : null}
  </section>;
}

export default function RelayMonitorPanel() {
  const [snapshot, setSnapshot] = useState<RelayMonitorSnapshot | null>(null);
  const [items, setItems] = useState<readonly RelayMonitorItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [actionFeedback, setActionFeedback] = useState<string | null>(null);
  const [otherDrafts, setOtherDrafts] = useState<Record<string, string>>({});
  const runAction = async (name: string, call: () => ReturnType<typeof controlRelaySupervisor>) => {
    setBusyAction(name); setActionFeedback(null);
    try {
      const result = await call();
      if (result.snapshot) setSnapshot(result.snapshot);
      setActionFeedback(result.state === "ok" ? "Done" : `Refused: ${words(result.reason ?? "unknown")}`);
    } catch (cause) {
      setActionFeedback(`Failed: ${cause instanceof CodingRequestError ? words(cause.code) : "unavailable"}`);
    } finally { setBusyAction(null); }
  };
  const cursor = useRef<string | null>(null);
  const timeline = useRef<HTMLOListElement>(null);
  const pinned = useRef(true);

  // Terminal behaviour: stay on the newest line unless the reader has scrolled up.
  useLayoutEffect(() => {
    const list = timeline.current;
    if (list && pinned.current) list.scrollTop = list.scrollHeight;
  }, [items]);
  useEffect(() => {
    const list = timeline.current;
    if (!list) return undefined;
    const onScroll = () => { pinned.current = list.scrollHeight - list.scrollTop - list.clientHeight < 24; };
    list.addEventListener("scroll", onScroll);
    return () => list.removeEventListener("scroll", onScroll);
  }, [items.length > 0]);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const next = await readRelayMonitor();
        if (!active) return;
        setSnapshot(next); setError(next.state === "refused" ? next.reason ?? "refused" : null);
        for (let page = 0; page < PAGES_PER_POLL; page += 1) {
          const result = await readRelayMonitorTimeline(cursor.current);
          if (!active) return;
          if (result.state !== "ok") {
            setError(result.reason ?? result.state);
            break;
          }
          cursor.current = result.cursor ?? cursor.current;
          const fresh = result.items ?? [];
          if (fresh.length) setItems((kept) => [...kept, ...fresh].slice(-ITEMS_KEPT));
          if (!result.more) break;
        }
        setConnected(true);
      } catch (cause) {
        if (!active) return;
        setConnected(false);
        const code = cause instanceof CodingRequestError ? cause.code : "unavailable";
        if (code === "cursor_invalid") cursor.current = null;
        setError(code);
      }
      if (active) timer = setTimeout(() => void tick(), POLL_MS);
    };
    void tick();
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, []);

  return <RelayMonitorView snapshot={snapshot} items={items} error={error} connected={connected} timelineRef={timeline} busyAction={busyAction} actionFeedback={actionFeedback}
    onControl={(action) => void runAction(action, () => controlRelaySupervisor(action))}
    onAnswerQuestion={(id, answer) => void runAction(id, () => answerRelayQuestion(id, answer))}
    otherDrafts={otherDrafts} onOtherDraftChange={(id, text) => setOtherDrafts((drafts) => ({ ...drafts, [id]: text }))} />;
}
