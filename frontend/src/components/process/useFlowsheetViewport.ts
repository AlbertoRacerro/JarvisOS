import { useCallback, useEffect, useRef, useState, type MutableRefObject, type PointerEvent as ReactPointerEvent } from "react";

/** Flowsheet-local zoom and pan (spec 181): only the SVG viewBox changes; page zoom never does. */
export type Box = { x: number; y: number; w: number; h: number };
type View = { cx: number; cy: number; zoom: number };
type Gesture = { pointers: Map<number, { x: number; y: number }>; start: View; moved: boolean; distance: number | null };

export const ZOOM_MIN = 0.25;
export const ZOOM_MAX = 4;
const clampZoom = (zoom: number) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom));

export function useFlowsheetViewport(svgRef: MutableRefObject<SVGSVGElement | null>, content: Box) {
  const [size, setSize] = useState({ w: 800, h: 480 });
  // null follows the content ("Fit all"); an explicit view stays put while the flowsheet is edited.
  const [view, setView] = useState<View | null>(null);
  const gesture = useRef<Gesture | null>(null);
  // The SVG mounts only after the draft loads, so listeners follow the element, not the first render.
  const [element, setElement] = useState<SVGSVGElement | null>(null);
  const attach = useCallback((node: SVGSVGElement | null) => {
    svgRef.current = node;
    setElement(node);
  }, [svgRef]);

  useEffect(() => {
    const svg = element;
    if (!svg) return;
    const measure = () => {
      const rect = svg.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) setSize({ w: rect.width, h: rect.height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(svg);
    return () => observer.disconnect();
  }, [element]);

  const fitted: View = {
    cx: content.x + content.w / 2,
    cy: content.y + content.h / 2,
    zoom: clampZoom(Math.min(size.w / content.w, size.h / content.h)),
  };
  const current = view ?? fitted;
  // The viewBox keeps the element's aspect ratio, so one SVG unit is exactly `zoom` CSS pixels.
  const viewBox: Box = { x: current.cx - size.w / current.zoom / 2, y: current.cy - size.h / current.zoom / 2, w: size.w / current.zoom, h: size.h / current.zoom };

  const zoomAt = useCallback((factor: number, clientX?: number, clientY?: number) => {
    const svg = svgRef.current;
    const rect = svg?.getBoundingClientRect();
    const base = view ?? fitted;
    const zoom = clampZoom(base.zoom * factor);
    if (!rect || clientX === undefined || clientY === undefined) return setView({ ...base, zoom });
    // Keep the SVG point under the cursor fixed on screen.
    const offsetX = clientX - rect.left - rect.width / 2;
    const offsetY = clientY - rect.top - rect.height / 2;
    const pointX = base.cx + offsetX / base.zoom;
    const pointY = base.cy + offsetY / base.zoom;
    setView({ cx: pointX - offsetX / zoom, cy: pointY - offsetY / zoom, zoom });
  }, [fitted, svgRef, view]);

  const panBy = useCallback((dx: number, dy: number) => {
    const base = view ?? fitted;
    setView({ cx: base.cx + dx / base.zoom, cy: base.cy + dy / base.zoom, zoom: base.zoom });
  }, [fitted, view]);

  // Inside the flowsheet box the wheel scrolls the flowsheet (shift for sideways), like a long document;
  // ctrl+wheel and trackpad pinch zoom it. A passive listener could not stop the page from moving instead.
  const zoomRef = useRef(zoomAt);
  zoomRef.current = zoomAt;
  const panRef = useRef(panBy);
  panRef.current = panBy;
  useEffect(() => {
    const svg = element;
    if (!svg) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      if (event.ctrlKey) zoomRef.current(Math.exp(-event.deltaY * 0.01), event.clientX, event.clientY);
      else if (event.shiftKey && !event.deltaX) panRef.current(event.deltaY, 0);
      else panRef.current(event.deltaX, event.deltaY);
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [element]);

  const spread = (pointers: Gesture["pointers"]) => {
    const [a, b] = [...pointers.values()];
    return { distance: Math.hypot(a.x - b.x, a.y - b.y), x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  };

  /** Background pointer down: one pointer pans, two pinch-zoom at their midpoint. */
  const onBackgroundDown = (event: ReactPointerEvent) => {
    (event.currentTarget as Element).setPointerCapture?.(event.pointerId);
    const state = gesture.current ?? { pointers: new Map(), start: current, moved: false, distance: null };
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    state.start = current;
    state.distance = state.pointers.size === 2 ? spread(state.pointers).distance : null;
    gesture.current = state;
  };
  /** Returns true while a background gesture owns the pointer. */
  const onGestureMove = (event: ReactPointerEvent): boolean => {
    const state = gesture.current;
    const previous = state?.pointers.get(event.pointerId);
    if (!state || !previous) return false;
    if (state.pointers.size === 2 && state.distance) {
      state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      const now = spread(state.pointers);
      state.moved = true;
      zoomAt(now.distance / state.distance, now.x, now.y);
      state.distance = now.distance;
      return true;
    }
    const first = [...state.pointers.values()][0];
    const dx = event.clientX - first.x;
    const dy = event.clientY - first.y;
    if (!state.moved && Math.hypot(dx, dy) < 3) return true;
    state.moved = true;
    setView({ cx: state.start.cx - dx / state.start.zoom, cy: state.start.cy - dy / state.start.zoom, zoom: state.start.zoom });
    return true;
  };
  /** Returns whether the finished gesture was a plain click (no pan or pinch). */
  const onGestureUp = (event: ReactPointerEvent): boolean | null => {
    const state = gesture.current;
    if (!state?.pointers.has(event.pointerId)) return null;
    state.pointers.delete(event.pointerId);
    if (state.pointers.size === 0) gesture.current = null;
    else {
      state.start = current;
      state.distance = null;
      const [rest] = [...state.pointers.entries()];
      state.pointers.set(rest[0], rest[1]);
    }
    return !state.moved;
  };

  return {
    attach,
    viewBox,
    zoom: current.zoom,
    fitted: view === null,
    zoomIn: () => zoomAt(1.25),
    zoomOut: () => zoomAt(1 / 1.25),
    fitAll: () => setView(null),
    reset: () => setView({ cx: fitted.cx, cy: fitted.cy, zoom: 1 }),
    onBackgroundDown,
    onGestureMove,
    onGestureUp,
  };
}
