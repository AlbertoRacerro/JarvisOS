import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { X } from "@phosphor-icons/react";
import "./BubuPointer.css";
import { isRenderable, resolveTarget } from "./pageContext";
import type { PointDirective } from "./pointerDirectives";

// "Show me where": a pointer-events-none overlay that rings the referenced elements and draws an arrow from Bubu.
// Pass an empty array to clear. It scrolls to an off-screen target but never clicks anything.

type Shape = { id: string; label?: string; x: number; y: number; w: number; h: number; arrow: string; head: string; below: boolean };
type Pt = { x: number; y: number };

const PAD = 4;
const DISMISS = 18;
const GAP_MS = 3000;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const r1 = (n: number) => Math.round(n * 2) / 2;
const reducedMotion = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

function bubuCenter(): Pt {
  const r = document.getElementById("bubu-launcher")?.getBoundingClientRect();
  return r && r.width > 0 ? { x: r.left + r.width / 2, y: r.top + r.height / 2 } : { x: window.innerWidth - 48, y: window.innerHeight - 48 };
}

function shapeFor(p: PointDirective, rect: DOMRect, from: Pt): Shape {
  const x = rect.left - PAD, y = rect.top - PAD, w = rect.width + 2 * PAD, h = rect.height + 2 * PAD;
  const inside = from.x >= x && from.x <= x + w && from.y >= y && from.y <= y + h;
  const end: Pt = inside ? { x: x + w / 2, y: y + h / 2 } : { x: clamp(from.x, x, x + w), y: clamp(from.y, y, y + h) };
  const dx = end.x - from.x, dy = end.y - from.y, len = Math.hypot(dx, dy);
  let arrow = "", head = "";
  if (!inside && len > 64) {
    const ux = dx / len, uy = dy / len;
    const s: Pt = { x: from.x + ux * 30, y: from.y + uy * 30 };
    const e: Pt = { x: end.x - ux * 7, y: end.y - uy * 7 };
    const bow = Math.min(len * 0.22, 90);
    const nx = -uy, ny = ux, sign = ny <= 0 ? 1 : -1; // bow upward
    const c: Pt = { x: (s.x + e.x) / 2 + nx * bow * sign, y: (s.y + e.y) / 2 + ny * bow * sign };
    arrow = `M${r1(s.x)} ${r1(s.y)} Q${r1(c.x)} ${r1(c.y)} ${r1(e.x)} ${r1(e.y)}`;
    const a = Math.atan2(e.y - c.y, e.x - c.x), wing = (d: number) => `${r1(e.x - 12 * Math.cos(a + d))} ${r1(e.y - 12 * Math.sin(a + d))}`;
    head = `M${r1(e.x)} ${r1(e.y)} L${wing(0.45)} L${wing(-0.45)} Z`;
  }
  return { id: p.id, label: p.label, x: r1(x), y: r1(y), w: r1(w), h: r1(h), arrow, head, below: y < 36 };
}

// The × sits to the right of the ring. At the right edge it moves to the ring's left,
// vertically beside the target so it stays clear of the label above or below it.
function dismissSpot(s: Shape): Pt {
  let left = s.x + s.w + 6, top = s.y - 6;
  if (left + DISMISS > window.innerWidth - 4) { left = s.x - DISMISS - 6; top = s.y + (s.h - DISMISS) / 2; }
  return { x: clamp(left, 4, Math.max(4, window.innerWidth - 4 - DISMISS)), y: clamp(top, 4, Math.max(4, window.innerHeight - 4 - DISMISS)) };
}

/** Rings and arrows are drawn by a pass-through overlay; with `onDismiss` each ring gets a small × (the only clickable part). */
export function BubuPointer({ points, onDone, onDismiss }: { points: PointDirective[]; onDone?: () => void; onDismiss?: (id: string) => void }) {
  const [shapes, setShapes] = useState<Shape[]>([]);
  const pointsRef = useRef(points);
  pointsRef.current = points;
  const doneRef = useRef(onDone);
  doneRef.current = onDone;
  const key = points.map((p) => `${p.id}|${p.label ?? ""}`).join("\n");

  useEffect(() => {
    if (!key) { setShapes([]); return; }
    const startedAt = performance.now();
    let scrolled = false, seen = false, finished = false, last = "", raf = 0;
    const measure = () => {
      const from = bubuCenter();
      const next: Shape[] = [];
      for (const p of pointsRef.current) {
        const el = resolveTarget(p.id);
        if (!el || !isRenderable(el)) continue;
        let rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
        if (!scrolled && (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight)) {
          scrolled = true;
          el.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" });
          rect = el.getBoundingClientRect();
        }
        if (rect.bottom < 0 || rect.right < 0 || rect.top > window.innerHeight || rect.left > window.innerWidth) continue; // still off-screen (scrolling)
        next.push(shapeFor(p, rect, from));
      }
      const sig = JSON.stringify(next);
      if (sig !== last) { last = sig; setShapes(next); }
      if (next.length) seen = true;
      if (!finished && ((seen && !next.length) || (!seen && performance.now() - startedAt > GAP_MS))) { finished = true; doneRef.current?.(); }
    };
    const loop = () => { measure(); raf = requestAnimationFrame(loop); };
    loop();
    window.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    return () => { cancelAnimationFrame(raf); window.removeEventListener("scroll", measure, true); window.removeEventListener("resize", measure); };
  }, [key]);

  if (!shapes.length || typeof document === "undefined") return null;
  const dismiss = (id: string, viaKeyboard = false) => {
    onDismiss?.(id);
    if (viaKeyboard) window.requestAnimationFrame(() => document.querySelector<HTMLElement>(".bubu-pointer__dismiss")?.focus({ preventScroll: true }));
  };
  const onDismissKey = (event: KeyboardEvent<HTMLButtonElement>, id: string) => {
    if (event.key === "Escape" && !event.nativeEvent.isComposing) { event.preventDefault(); event.stopPropagation(); dismiss(id, true); }
  };
  const first = shapes.length > 1 && onDismiss ? dismissSpot(shapes[0]) : null;
  return createPortal(<div className="bubu-pointer" data-bubu-ui="pointer">
    <svg className="bubu-pointer__svg" width="100%" height="100%" aria-hidden="true">
      {shapes.map((s) => <g key={s.id}>
        <rect className="bubu-pointer__halo" x={s.x} y={s.y} width={s.w} height={s.h} rx="9" />
        <rect className="bubu-pointer__ring" x={s.x} y={s.y} width={s.w} height={s.h} rx="9" />
        {s.arrow && <path className="bubu-pointer__arrow" d={s.arrow} pathLength={1} />}
        {s.head && <path className="bubu-pointer__head" d={s.head} />}
      </g>)}
    </svg>
    {shapes.filter((s) => s.label).map((s) => <span key={s.id} className={`bubu-pointer__tag${s.below ? " is-below" : ""}`} aria-hidden="true" style={{ left: clamp(s.x, 8, Math.max(8, window.innerWidth - 8 - Math.min(260, window.innerWidth - 16))), top: s.below ? s.y + s.h + 8 : s.y - 8 }}>{s.label}</span>)}
    {onDismiss ? shapes.map((s) => { const at = dismissSpot(s); return <button key={s.id} type="button" className="bubu-pointer__dismiss" data-bubu-ui="pointer-dismiss" aria-label="Dismiss" title={s.label ? `Dismiss: ${s.label}` : "Dismiss"} style={{ left: at.x, top: at.y }} onClick={() => dismiss(s.id)} onKeyDown={(event) => onDismissKey(event, s.id)}><X size={11} weight="bold" aria-hidden="true" /></button>; }) : null}
    {first ? <button type="button" className="bubu-pointer__clear" data-bubu-ui="pointer-dismiss" title="Dismiss all" style={first.x + DISMISS + 4 + 78 <= window.innerWidth ? { left: first.x + DISMISS + 4, top: first.y } : { left: clamp(first.x - 60, 4, Math.max(4, window.innerWidth - 82)), top: clamp(first.y + DISMISS + 4, 4, Math.max(4, window.innerHeight - 24)) }} onClick={() => shapes.forEach((s) => onDismiss?.(s.id))}>Clear all</button> : null}
  </div>, document.body);
}
