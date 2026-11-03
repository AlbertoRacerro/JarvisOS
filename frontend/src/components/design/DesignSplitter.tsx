import { useRef, type KeyboardEvent, type PointerEvent } from "react";
import { designTarget, type DesignTargetId } from "../../app/designTargets";

// Spec 214 / #920 item 5: a WAI-ARIA window splitter (focusable separator) for the Design component layout,
// following the spec 213 shell PanelSplitter pattern. Widths are visual-only browser preferences.
export type DesignPanelBounds = Readonly<{ min: number; max: number; fallback: number }>;

export function clampDesignWidth(bounds: DesignPanelBounds, width: number): number {
  if (!Number.isFinite(width)) return bounds.fallback;
  return Math.round(Math.min(bounds.max, Math.max(bounds.min, width)));
}

const STEP = 16;
const LARGE_STEP = 64;

type Props = Readonly<{
  label: string;
  controls: string;
  bounds: DesignPanelBounds;
  /** "start": the controlled pane is left of the splitter; "end": it is right of it. */
  side: "start" | "end";
  width: number;
  onResize(width: number): void;
  onCommit(width: number): void;
  onReset(): void;
  /** Spec 230: optional 229 semantic target for guidance. */
  target?: DesignTargetId;
}>;

function DesignSplitter({ label, controls, bounds, side, width, onResize, onCommit, onReset, target }: Props) {
  const drag = useRef<{ startX: number; startWidth: number; last: number } | null>(null);
  const direction = side === "start" ? 1 : -1;
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { startX: event.clientX, startWidth: width, last: width };
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return;
    const next = clampDesignWidth(bounds, drag.current.startWidth + (event.clientX - drag.current.startX) * direction);
    drag.current.last = next;
    onResize(next);
  };
  const endDrag = () => {
    if (!drag.current) return;
    onCommit(drag.current.last);
    drag.current = null;
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? LARGE_STEP : STEP;
    const grow = side === "start" ? "ArrowRight" : "ArrowLeft";
    const shrink = side === "start" ? "ArrowLeft" : "ArrowRight";
    let next: number | null = null;
    if (event.key === grow) next = clampDesignWidth(bounds, width + step);
    else if (event.key === shrink) next = clampDesignWidth(bounds, width - step);
    else if (event.key === "Home") next = bounds.min;
    else if (event.key === "End") next = bounds.max;
    if (next === null) return;
    event.preventDefault();
    onResize(next);
    onCommit(next);
  };
  return <div className="design-splitter" role="separator" aria-orientation="vertical" aria-label={label} aria-controls={controls}
    aria-valuemin={bounds.min} aria-valuemax={bounds.max} aria-valuenow={Math.round(width)}
    title={`${label} (drag, arrow keys; double-click to reset)`} tabIndex={0}
    onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={endDrag} onPointerCancel={endDrag}
    onKeyDown={onKeyDown} onDoubleClick={onReset} {...(target ? designTarget(target) : {})} />;
}

export default DesignSplitter;
