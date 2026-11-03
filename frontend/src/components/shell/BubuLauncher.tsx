import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type RefObject } from "react";
import { createPortal } from "react-dom";
import { publishBubuRect } from "../bubu/bubuAnchor";
import { BUBU_POSES, bubuPose, bubuPoseSrc } from "../bubu/bubuPose";
import { useBubuConversation } from "../bubu/conversationStore";
import "./BubuLauncher.css";

type Point = Readonly<{ x: number; y: number }>;
const POSITION_KEY = "jarvisos:bubu-position:v1";
const SIZE = 96;
const MARGIN = 12;
const DEFAULT_POSITION: Point = { x: 1, y: 1 };

function readPosition(): Point {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(POSITION_KEY) ?? "null");
    if (value && typeof value === "object" && "x" in value && "y" in value &&
      typeof value.x === "number" && Number.isFinite(value.x) &&
      typeof value.y === "number" && Number.isFinite(value.y)) {
      return { x: Math.max(0, Math.min(1, value.x)), y: Math.max(0, Math.min(1, value.y)) };
    }
  } catch { /* Position is a best-effort visual preference, never conversation state. */ }
  return DEFAULT_POSITION;
}

function savePosition(position: Point) {
  try { window.localStorage.setItem(POSITION_KEY, JSON.stringify(position)); }
  catch { /* Dragging remains available when browser storage is disabled. */ }
}

function viewport() {
  return { width: window.innerWidth, height: window.innerHeight };
}

type Props = Readonly<{
  buttonRef: RefObject<HTMLButtonElement>;
  /** The shell Sidecar is open. */
  expanded: boolean;
  /** The speech bubble is open. */
  bubbleOpen?: boolean;
  /** After Sleep, until Bubu is clicked again. */
  asleep?: boolean;
  /** Dictation is active: listening pose with a subtle pulse. */
  listening?: boolean;
  onActivate(): void;
}>;

/** Bubu's mascot button. Its pose mirrors the real conversation state; it never controls the page. */
export default function BubuLauncher({ buttonRef, expanded, bubbleOpen = false, asleep = false, listening = false, onActivate }: Props) {
  const conversation = useBubuConversation();
  const lastTurn = conversation.turns[conversation.turns.length - 1];
  const pose = bubuPose({
    asleep, dictating: listening, pending: conversation.pending, error: conversation.error,
    latestReply: lastTurn && lastTurn.role === "jarvis" && !lastTurn.pending && !lastTurn.failed ? lastTurn.text : null,
  });
  useEffect(() => { for (const name of BUBU_POSES) { const image = new Image(); image.src = bubuPoseSrc(name); } }, []);
  const [position, setPosition] = useState(readPosition);
  const [bounds, setBounds] = useState(viewport);
  const [dragging, setDragging] = useState(false);
  const [panelLeft, setPanelLeft] = useState<number | null>(null);
  const drag = useRef<{ id: number; start: Point; origin: Point; moved: boolean } | null>(null);
  const suppressClick = useRef(false);
  const current = useRef(position);
  const size = Math.min(SIZE, bounds.width, bounds.height);
  const marginX = Math.min(MARGIN, Math.max(0, (bounds.width - size) / 2));
  const marginY = Math.min(MARGIN, Math.max(0, (bounds.height - size) / 2));
  // Keep the launcher beside the open desktop panel rather than over its composer.
  const availableWidth = panelLeft !== null && panelLeft >= size + MARGIN * 2 ? panelLeft : bounds.width;
  const rangeX = Math.max(0, availableWidth - size - marginX * 2);
  const rangeY = Math.max(0, bounds.height - size - marginY * 2);

  const move = (next: Point) => {
    current.current = next;
    setPosition(next);
  };

  useEffect(() => {
    const resize = () => setBounds(viewport());
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);

  useLayoutEffect(() => {
    const panel = expanded ? document.getElementById("shell-sidecar") : null;
    if (!panel) { setPanelLeft(null); return; }
    const measure = () => setPanelLeft(panel.getBoundingClientRect().left);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(panel);
    window.addEventListener("resize", measure);
    return () => { observer.disconnect(); window.removeEventListener("resize", measure); };
  }, [expanded]);

  const left = marginX + position.x * rangeX;
  const top = marginY + position.y * rangeY;
  // The bubble follows Bubu: publish the rendered rect (cheap external store, no shell re-render).
  useLayoutEffect(() => { publishBubuRect({ left, top, width: size, height: size }); }, [left, top, size]);
  useEffect(() => () => publishBubuRect(null), []);

  const startDrag = (event: PointerEvent<HTMLButtonElement>) => {
    if (!event.isPrimary || event.button !== 0) return;
    suppressClick.current = false;
    drag.current = { id: event.pointerId, start: { x: event.clientX, y: event.clientY }, origin: current.current, moved: false };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const updateDrag = (event: PointerEvent<HTMLButtonElement>) => {
    const pending = drag.current;
    if (!pending || pending.id !== event.pointerId) return;
    const dx = event.clientX - pending.start.x;
    const dy = event.clientY - pending.start.y;
    if (!pending.moved && Math.hypot(dx, dy) < 6) return;
    pending.moved = true;
    suppressClick.current = true;
    setDragging(true);
    move({
      x: rangeX ? Math.max(0, Math.min(1, pending.origin.x + dx / rangeX)) : 0,
      y: rangeY ? Math.max(0, Math.min(1, pending.origin.y + dy / rangeY)) : 0,
    });
  };

  const finishDrag = (event: PointerEvent<HTMLButtonElement>, cancel = false) => {
    const pending = drag.current;
    if (!pending || pending.id !== event.pointerId) return;
    drag.current = null;
    setDragging(false);
    if (cancel) move(pending.origin);
    else if (pending.moved) savePosition(current.current);
    else {
      // Handle a completed tap directly: touch devices need not synthesize a
      // mouse click after capture. Suppress the duplicate click when they do.
      suppressClick.current = true;
      onActivate();
    }
    // Normal pointerup releases capture automatically.
    if (cancel && event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "Escape" && drag.current) {
      event.preventDefault();
      const pending = drag.current;
      drag.current = null;
      move(pending.origin);
      setDragging(false);
      suppressClick.current = true;
      if (event.currentTarget.hasPointerCapture(pending.id)) event.currentTarget.releasePointerCapture(pending.id);
      return;
    }
    const directions: Record<string, Point> = {
      ArrowLeft: { x: -1, y: 0 }, ArrowRight: { x: 1, y: 0 },
      ArrowUp: { x: 0, y: -1 }, ArrowDown: { x: 0, y: 1 },
    };
    const direction = directions[event.key];
    if (!direction && event.key !== "Home") return;
    event.preventDefault();
    const step = event.shiftKey ? 48 : 16;
    const next = event.key === "Home" ? DEFAULT_POSITION : {
      x: rangeX ? Math.max(0, Math.min(1, current.current.x + direction.x * step / rangeX)) : 0,
      y: rangeY ? Math.max(0, Math.min(1, current.current.y + direction.y * step / rangeY)) : 0,
    };
    move(next);
    savePosition(next);
  };

  return createPortal(<button
    ref={buttonRef}
    type="button"
    id="bubu-launcher"
    className="bubu-launcher"
    data-bubu-ui="launcher"
    data-dragging={dragging}
    data-pose={pose}
    data-listening={listening || undefined}
    aria-label={expanded || bubbleOpen ? "Bubu — Close Jarvis" : asleep ? "Bubu — Wake Jarvis" : "Bubu — Open Jarvis"}
    aria-expanded={expanded || bubbleOpen}
    aria-controls={bubbleOpen ? "bubu-bubble" : undefined}
    aria-description="Drag to move, or use arrow keys. Home restores the corner. Enter opens Jarvis."
    title={expanded || bubbleOpen ? "Bubu · Close Jarvis" : asleep ? "Bubu · Wake up" : "Bubu · Open Jarvis · Drag to move"}
    style={{ left, top, width: size, height: size }}
    onPointerDown={startDrag}
    onPointerMove={updateDrag}
    onPointerUp={(event) => finishDrag(event)}
    onPointerCancel={(event) => finishDrag(event, true)}
    onLostPointerCapture={(event) => finishDrag(event, true)}
    onKeyDown={onKeyDown}
    onClick={(event) => {
      if (suppressClick.current && event.detail !== 0) { suppressClick.current = false; return; }
      suppressClick.current = false;
      onActivate();
    }}
  >
    <img src={bubuPoseSrc(pose)} alt="" width={512} height={512} draggable={false} />
  </button>, document.body);
}
