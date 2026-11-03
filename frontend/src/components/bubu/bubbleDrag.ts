// Pure geometry + storage for the draggable Bubu bubble. Self-contained so Node tests can import it.

export const BUBU_BUBBLE_POSITION_KEY = "jarvisos:bubu-bubble-position:v1";
export const DRAG_THRESHOLD = 4;
export const KEY_STEP = 16;
export const KEY_STEP_BIG = 48;

export type BubblePoint = { left: number; top: number };
export type BubbleSize = { width: number; height: number };
export type BubbleViewport = { width: number; height: number };
/** A position as a fraction of the viewport (0..1), so it survives window resizes and size changes of the bubble. */
export type NormalizedPosition = { x: number; y: number };

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(value, Math.max(min, max)));
const unit = (value: number) => (Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0);

/** Keeps the whole bubble inside the viewport, `margin` away from every edge (if it cannot fit, it sticks to the top-left margin). */
export function clampBubblePosition(point: BubblePoint, size: BubbleSize, viewport: BubbleViewport, margin = 12): BubblePoint {
  return {
    left: clamp(point.left, margin, viewport.width - size.width - margin),
    top: clamp(point.top, margin, viewport.height - size.height - margin),
  };
}

export function normalizeBubblePosition(point: BubblePoint, viewport: BubbleViewport): NormalizedPosition {
  return {
    x: viewport.width > 0 ? unit(point.left / viewport.width) : 0,
    y: viewport.height > 0 ? unit(point.top / viewport.height) : 0,
  };
}

export function denormalizeBubblePosition(position: NormalizedPosition, size: BubbleSize, viewport: BubbleViewport, margin = 12): BubblePoint {
  return clampBubblePosition({ left: Math.round(unit(position.x) * viewport.width), top: Math.round(unit(position.y) * viewport.height) }, size, viewport, margin);
}

/** True once a pointer moved far enough from where it went down to count as a drag rather than a click. */
export function exceedsDragThreshold(dx: number, dy: number, threshold = DRAG_THRESHOLD): boolean {
  return Math.hypot(dx, dy) >= threshold;
}

/** Arrow-key movement of the handle: 16px, 48px with Shift. Returns null for any other key. */
export function nudgeBubblePosition(point: BubblePoint, key: string, shift: boolean, size: BubbleSize, viewport: BubbleViewport, margin = 12): BubblePoint | null {
  const step = shift ? KEY_STEP_BIG : KEY_STEP;
  const delta = key === "ArrowLeft" ? [-step, 0] : key === "ArrowRight" ? [step, 0] : key === "ArrowUp" ? [0, -step] : key === "ArrowDown" ? [0, step] : null;
  return delta ? clampBubblePosition({ left: point.left + delta[0], top: point.top + delta[1] }, size, viewport, margin) : null;
}

export function parseStoredPosition(raw: string | null | undefined): NormalizedPosition | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as { x?: unknown; y?: unknown } | null;
    if (!value || typeof value.x !== "number" || typeof value.y !== "number" || !Number.isFinite(value.x) || !Number.isFinite(value.y)) return null;
    return { x: unit(value.x), y: unit(value.y) };
  } catch { return null; }
}

export const serializePosition = (position: NormalizedPosition): string =>
  JSON.stringify({ x: Math.round(unit(position.x) * 1e5) / 1e5, y: Math.round(unit(position.y) * 1e5) / 1e5 });

const defaultStorage = (): Pick<Storage, "getItem" | "setItem" | "removeItem"> | null => {
  try { return typeof localStorage === "undefined" ? null : localStorage; } catch { return null; }
};

/** Best effort: private windows or blocked storage just mean the bubble is not remembered. */
export function loadStoredPosition(storage = defaultStorage()): NormalizedPosition | null {
  try { return parseStoredPosition(storage?.getItem(BUBU_BUBBLE_POSITION_KEY)); } catch { return null; }
}

/** Pass null to forget the position (the bubble is attached to Bubu again). */
export function saveStoredPosition(position: NormalizedPosition | null, storage = defaultStorage()): void {
  try {
    if (!storage) return;
    if (position) storage.setItem(BUBU_BUBBLE_POSITION_KEY, serializePosition(position));
    else storage.removeItem(BUBU_BUBBLE_POSITION_KEY);
  } catch { /* not remembered */ }
}
