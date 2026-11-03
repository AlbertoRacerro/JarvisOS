import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { ArrowUp, ArrowsOutSimple, DotsSix, Moon, PushPin, X } from "@phosphor-icons/react";

import { clampBubblePosition, denormalizeBubblePosition, exceedsDragThreshold, loadStoredPosition, normalizeBubblePosition, nudgeBubblePosition, saveStoredPosition, type BubblePoint, type NormalizedPosition } from "./bubbleDrag";
import { placeBubble } from "./bubblePlacement";
import { useBubuRect } from "./bubuAnchor";
import { askClaudeAboutTurn, useBubuConversation, useBubuRelayEnabled, type BubuExtras } from "./conversationStore";
import "./BubuBubble.css";

const WIDTH = 360;
const MARGIN = 12;

export type BubuBubbleProps = Readonly<{
  open: boolean;
  /** Hide the bubble only; the conversation stays. */
  onClose(): void;
  /** Open the full Jarvis panel (the owner also closes the bubble). */
  onExpand(): void;
  /** Called after the conversation was ended with Sleep (the owner closes the bubble and shows the sleeping pose). */
  onSleep(): void;
  /** Where focus goes when the bubble closes (Bubu's button). */
  returnFocusRef?: RefObject<HTMLElement | null>;
  /** Extra toolbar controls (dictation, screenshot toggle, ...). */
  extraTools?: ReactNode;
  /** Called on submit; its result is passed through to the conversation (page description, screenshots). */
  getExtras?: () => Promise<BubuExtras>;
  /** How a Jarvis reply is drawn. Default: plain text with line breaks. */
  renderReply?: (text: string) => ReactNode;
  /** Text to append to the draft (dictation, a picked element); a new nonce appends again. */
  draftInsert?: Readonly<{ text: string; nonce: number }>;
  /** One short status line above the toolbar (interim dictation, attachments, media errors). */
  status?: ReactNode;
}>;

/** What the app can add around the bubble: the integrator wires dictation, page description and reply rendering here. */
export type BubuIntegration = Readonly<{
  extraTools?: ReactNode;
  getExtras?: () => Promise<BubuExtras>;
  renderReply?: (text: string) => ReactNode;
  draftInsert?: Readonly<{ text: string; nonce: number }>;
  status?: ReactNode;
  /** Dictation is active: Bubu shows the listening pose with a subtle pulse. */
  listening?: boolean;
  /** The conversation was ended with Sleep: vision, pointers and attachments reset. */
  onSleep?(): void;
}>;

const plainReply =(text: string): ReactNode => <p>{text}</p>;
const viewport = () => ({ width: window.innerWidth, height: window.innerHeight });

/** Bubu's speech bubble: the last few turns, a composer and a small toolbar, anchored next to Bubu. */
export default function BubuBubble({ open, onClose, onExpand, onSleep, returnFocusRef, extraTools, getExtras, renderReply = plainReply, draftInsert, status }: BubuBubbleProps) {
  const conversation = useBubuConversation();
  const relayEnabled = useBubuRelayEnabled();
  const anchor = useBubuRect();
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [height, setHeight] = useState(280);
  const [screen, setScreen] = useState(viewport);
  const rootRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const listRef = useRef<HTMLOListElement>(null);
  const gripRef = useRef<HTMLDivElement>(null);
  // Once the user moves the bubble it is "detached": it keeps its own spot (a fraction of the viewport) and stops following Bubu.
  const [detached, setDetached] = useState<NormalizedPosition | null>(() => loadStoredPosition());
  const [dragPoint, setDragPoint] = useState<BubblePoint | null>(null);
  const dragRef = useRef<{ id: number; startX: number; startY: number; origin: BubblePoint; last: BubblePoint; active: boolean } | null>(null);
  const width = Math.max(0, Math.min(WIDTH, screen.width - MARGIN * 2));
  const { ready, turns, pending, error, failedPrompt } = conversation;

  const shown = open && anchor !== null;
  useEffect(() => {
    if (!shown) return;
    const resize = () => setScreen(viewport());
    resize();
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, [shown]);

  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!shown || !root) return;
    const measure = () => setHeight(root.offsetHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    return () => observer.disconnect();
  }, [shown]);

  useEffect(() => {
    if (!shown) return;
    const frame = window.requestAnimationFrame(() => (textareaRef.current && !textareaRef.current.disabled ? textareaRef.current : rootRef.current)?.focus({ preventScroll: true }));
    return () => window.cancelAnimationFrame(frame);
  }, [shown, ready]);

  useEffect(() => {
    const list = listRef.current;
    if (shown && list) list.scrollTop = list.scrollHeight;
  }, [shown, turns, pending]);

  // A submit that did not go through hands the text back so nothing typed is lost.
  useEffect(() => { if (failedPrompt) setDraft((current) => current || failedPrompt); }, [failedPrompt]);
  useEffect(() => {
    if (!draftInsert?.text) return;
    setDraft((current) => (current && !/\s$/.test(current) ? `${current} ` : current) + draftInsert.text);
    textareaRef.current?.focus({ preventScroll: true });
  }, [draftInsert?.nonce]);

  if (!shown || !anchor) return null;

  const size = { width, height };
  const layout = placeBubble(anchor, size, screen, { margin: MARGIN });
  const isDetached = detached !== null || dragPoint !== null;
  const position: BubblePoint = dragPoint ?? (detached ? denormalizeBubblePosition(detached, size, screen, MARGIN) : { left: layout.left, top: layout.top });
  const focusBubu = () => returnFocusRef?.current?.focus({ preventScroll: true });
  const close = () => { onClose(); focusBubu(); };
  const canSend = ready && !pending && !sending && draft.trim().length > 0;

  const send = async () => {
    const text = draft.trim();
    if (!canSend || !text) return;
    setDraft("");
    setSending(true);
    try {
      let extras: BubuExtras | undefined;
      try { extras = await getExtras?.(); } catch { extras = undefined; }
      await conversation.submit(text, extras);
    } finally { setSending(false); }
  };

  const commitPosition = (point: BubblePoint) => {
    const next = normalizeBubblePosition(clampBubblePosition(point, size, screen, MARGIN), screen);
    setDetached(next);
    saveStoredPosition(next);
  };
  const attach = () => { dragRef.current = null; setDragPoint(null); setDetached(null); saveStoredPosition(null); gripRef.current?.focus({ preventScroll: true }); };
  const onBarDown = (event: PointerEvent<HTMLDivElement>) => {
    if ((event.pointerType === "mouse" && event.button !== 0) || (event.target as HTMLElement).closest("button")) return;
    dragRef.current = { id: event.pointerId, startX: event.clientX, startY: event.clientY, origin: position, last: position, active: false };
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch { /* the drag still works while the pointer stays over the bar */ }
  };
  const onBarMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.id !== event.pointerId) return;
    const dx = event.clientX - drag.startX, dy = event.clientY - drag.startY;
    if (!drag.active && !exceedsDragThreshold(dx, dy)) return;
    drag.active = true;
    drag.last = clampBubblePosition({ left: drag.origin.left + dx, top: drag.origin.top + dy }, size, screen, MARGIN);
    setDragPoint(drag.last);
  };
  const onBarUp = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.id !== event.pointerId) return;
    dragRef.current = null;
    try { event.currentTarget.releasePointerCapture(event.pointerId); } catch { /* already released */ }
    setDragPoint(null);
    if (drag.active) commitPosition(drag.last);
  };
  const onGripKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Home") { event.preventDefault(); event.stopPropagation(); attach(); return; }
    const next = nudgeBubblePosition(position, event.key, event.shiftKey, size, screen, MARGIN);
    if (next) { event.preventDefault(); event.stopPropagation(); commitPosition(next); }
  };

  const sleep = () => { conversation.sleep(); setDraft(""); onSleep(); focusBubu(); };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape" && !event.nativeEvent.isComposing) { event.preventDefault(); event.stopPropagation(); close(); }
  };

  return createPortal(<div
    ref={rootRef}
    id="bubu-bubble"
    className="bubu-bubble"
    role="dialog"
    aria-label="Jarvis"
    data-bubu-ui="bubble"
    data-placement={isDetached ? undefined : layout.placement}
    data-detached={isDetached ? "true" : undefined}
    tabIndex={-1}
    style={{ left: position.left, top: position.top, width, ["--bubu-tail" as string]: `${layout.tail}px` }}
    onKeyDown={onKeyDown}
  >
    <div className={`bubu-bubble__bar${dragPoint ? " is-dragging" : ""}`} onPointerDown={onBarDown} onPointerMove={onBarMove} onPointerUp={onBarUp} onPointerCancel={onBarUp} onDoubleClick={(event) => { if (!(event.target as HTMLElement).closest("button")) attach(); }}>
      <div ref={gripRef} className="bubu-bubble__grip" role="button" tabIndex={0} aria-label="Move Jarvis" aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight Home" title="Drag to move · arrow keys nudge · Home or double-click attaches to Bubu" onKeyDown={onGripKeyDown}>
        <DotsSix size={16} weight="bold" aria-hidden="true" /><span className="bubu-bubble__title">Jarvis</span>
      </div>
      {detached ? <button type="button" className="bubu-bubble__attach" onClick={attach} aria-label="Attach to Bubu" title="Attach to Bubu"><PushPin size={14} aria-hidden="true" /></button> : null}
    </div>
    {turns.length ? <ol className="bubu-bubble__turns" ref={listRef} aria-label="Conversation" aria-live="polite">
      {turns.map((turn) => <li key={turn.id} className={`bubu-turn bubu-turn--${turn.role}${turn.failed ? " is-failed" : ""}`}>
        {turn.pending
          ? <span className="bubu-dots" role="status" aria-label="Jarvis is working"><i /><i /><i /></span>
          : turn.role === "jarvis" ? <>{renderReply(turn.text)}{turn.note ? <small className="bubu-turn__note">{turn.note}</small> : null}{turn.note && relayEnabled ? <button type="button" className="bubu-turn__ask" onClick={() => { askClaudeAboutTurn(turn.id); onExpand(); }} title="Send this question and your screenshot to Claude through your subscription. You review it first.">Ask Claude</button> : null}</> : <p>{turn.text}</p>}
      </li>)}
    </ol> : null}
    {error ? <p className="bubu-bubble__error" role="alert" title={error}>{error}</p> : null}
    <textarea
      ref={textareaRef}
      id="bubu-prompt"
      className="bubu-bubble__input"
      aria-label="Message to Jarvis"
      value={draft}
      rows={Math.min(5, Math.max(2, draft.split("\n").length))}
      maxLength={12000}
      disabled={!ready}
      placeholder={ready ? "Ask Jarvis…" : "Select a project"}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }}
    />
    {status ? <div className="bubu-bubble__status" role="status">{status}</div> : null}
    <div className="bubu-bubble__toolbar">
      <div className="bubu-bubble__tools">{extraTools}</div>
      <button type="button" className="bubu-bubble__btn bubu-bubble__btn--primary" onClick={() => void send()} disabled={!canSend} aria-label="Send" title="Send (Enter)"><ArrowUp size={16} weight="bold" aria-hidden="true" /></button>
      <button type="button" className="bubu-bubble__btn" onClick={onExpand} aria-label="Expand" title="Open the full Jarvis panel"><ArrowsOutSimple size={16} aria-hidden="true" /></button>
      <button type="button" className="bubu-bubble__btn" onClick={sleep} aria-label="Sleep" title="End this conversation"><Moon size={16} aria-hidden="true" /></button>
      <button type="button" className="bubu-bubble__btn" onClick={close} aria-label="Close" title="Hide (Esc)"><X size={16} aria-hidden="true" /></button>
    </div>
  </div>, document.body);
}
