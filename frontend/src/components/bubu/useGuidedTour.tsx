import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import type { Navigate } from "../../app/AppLink";
import { setGuidedTourActive } from "./guidedTourActivity";
import { TOUR_CLICKABLE, TOUR_COUNT, TOUR_STOPS, notAvailableLine, tourPosition, type TourStop } from "./guidedTour";
import { isRenderable, resolveTarget } from "./pageContext";
import type { PointDirective } from "./pointerDirectives";

// Spec 242: the deterministic tour. It navigates with the app router, selects rows of the Components tree the way a
// user would, and points with the existing pointer. It sends no request of its own and clicks nothing that changes data.

type Phase = "preparing" | "pointing" | "absent";
type TourState = Readonly<{ index: number; phase: Phase; note: string }>;
type Token = { cancelled: boolean };

const TARGET_ATTR = "data-jarvis-target";
const WAIT_MS = 5000;
const here = () => `${location.pathname}${location.search}`;
const stageOf = (href: string) => new URL(href, location.origin).searchParams.get("stage") ?? "plant";
const sameView = (href: string) => new URL(href, location.origin).pathname === location.pathname && stageOf(href) === stageOf(here());
const queryTargets = (prefix: string) => Array.from(document.querySelectorAll<HTMLElement>(`[${TARGET_ATTR}^="${prefix}"]`)).filter(isRenderable);
const inCaseStudy = () => !!document.querySelector(".shell-project__back");

async function until<T>(read: () => T | null | undefined | false, token: Token, ms = WAIT_MS): Promise<T | null> {
  const end = performance.now() + ms;
  for (;;) {
    if (token.cancelled) return null;
    const value = read();
    if (value) return value;
    if (performance.now() >= end) return null;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

const pointable = (id: string): Element | null => { const el = resolveTarget(id); return el && isRenderable(el) ? el : null; };

/** Selecting a tree row is navigation; anything else is refused here, so the tour cannot reach a mutating control. */
function selectRow(el: Element | null): boolean {
  const id = el?.getAttribute(TARGET_ATTR) ?? "";
  if (!el || !TOUR_CLICKABLE.test(id)) return false;
  if (el.getAttribute("aria-selected") !== "true") (el as HTMLElement).click();
  return true;
}

/** Opens the C1 case study through the control a user has: "Open case study C1", else the version picker's Case studies. */
async function openCaseStudy(token: Token): Promise<"in" | "switched" | "none"> {
  if (inCaseStudy()) return "in";
  const control = await until(() => {
    const offer = pointable("design.example-open");
    if (offer) return offer;
    const picker = pointable("design.version-picker") as HTMLSelectElement | null;
    return picker && !picker.disabled && picker.options.length ? picker : null;
  }, token, 3000);
  if (!control) return "none";
  if (control.tagName === "SELECT") {
    const picker = control as HTMLSelectElement;
    const option = Array.from(picker.options).find((item) => item.value.startsWith("case:") && /^C1\b/.test(item.text));
    if (!option) return "none";
    picker.value = option.value;
    picker.dispatchEvent(new Event("change", { bubbles: true }));
  } else (control as HTMLElement).click();
  return (await until(inCaseStudy, token)) ? "switched" : "none";
}

/** Brings the page to the stop and reports whether its target is on screen. */
async function settle(stop: TourStop, token: Token, navigate: Navigate, switched: { current: boolean }): Promise<Pick<TourState, "phase" | "note">> {
  const goTo = async () => {
    if (here() !== stop.route) navigate(stop.route);
    await until(() => sameView(stop.route), token);
    // The address changes before the page re-renders for it; a selection made on the old render would keep the old stage.
    await new Promise((resolve) => setTimeout(resolve, 250));
  };
  await goTo();
  if (stop.needs) {
    if (!inCaseStudy()) {
      const opened = await openCaseStudy(token);
      if (opened === "switched") { switched.current = true; await goTo(); }
    }
    if (stop.needs === "component") {
      const row = await until(() => queryTargets("design.tree.component.")[0], token);
      if (row) {
        selectRow(row);
        if (stop.node) selectRow(await until(() => pointable(stop.node!), token));
      }
    }
  }
  if (token.cancelled) return { phase: "preparing", note: "" };
  return (await until(() => pointable(stop.target), token))
    ? { phase: "pointing", note: "" }
    : { phase: "absent", note: notAvailableLine(stop.absent) };
}

export type GuidedTour = Readonly<{
  start(): void;
  /** Drives the bubble's tour body; undefined while no tour runs. */
  bubble: Readonly<{ content: ReactNode; onExit(): void }> | undefined;
  /** The pointer directive for the current stop. */
  points: PointDirective[];
  exit(): void;
}>;

export function useGuidedTour(navigate: Navigate): GuidedTour {
  const [state, setState] = useState<TourState | null>(null);
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  // The tour leaves the project as it found it: a case study it opened is closed with the visible Back button.
  const switched = useRef(false);
  const index = state?.index ?? null;

  const exit = useCallback(() => {
    setGuidedTourActive(false);
    setState(null);
    if (switched.current) {
      switched.current = false;
      (document.querySelector(".shell-project__back") as HTMLElement | null)?.click();
    }
  }, []);
  const go = (next: number) => setState({ index: Math.min(TOUR_COUNT - 1, Math.max(0, next)), phase: "preparing", note: "" });

  useEffect(() => {
    if (index === null) return;
    const token: Token = { cancelled: false };
    void settle(TOUR_STOPS[index], token, navigateRef.current, switched).then((result) => {
      if (token.cancelled) return;
      setState((current) => (current && current.index === index ? { index, ...result } : current));
      window.requestAnimationFrame(() => document.getElementById("bubu-tour-next")?.focus({ preventScroll: true }));
    });
    return () => { token.cancelled = true; };
  }, [index]);

  useEffect(() => {
    if (index === null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing) return;
      event.preventDefault(); event.stopPropagation(); exit();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [index, exit]);

  const stop = index === null ? null : TOUR_STOPS[index];
  const bubble = state && stop ? {
    onExit: exit,
    content: <div className="bubu-tour" data-tour-state={state.phase} data-tour-route={stop.route} data-tour-target={stop.target}>
      <p className="bubu-tour__count" aria-live="polite">{tourPosition(state.index)}</p>
      <h3 className="bubu-tour__title">{stop.title}</h3>
      <p className="bubu-tour__route">Page: <code>{stop.route}</code></p>
      <p>{stop.text}</p>
      <p className="bubu-tour__note" role="status">{state.phase === "preparing" ? "Opening…" : state.note}</p>
      <div className="bubu-tour__actions">
        <button type="button" className="bubu-tour__btn" onClick={() => go(state.index - 1)} disabled={state.index === 0}>Back</button>
        <button type="button" id="bubu-tour-next" className="bubu-tour__btn bubu-tour__btn--primary" onClick={() => go(state.index + 1)} disabled={state.index === TOUR_COUNT - 1}>Next</button>
        <button type="button" className="bubu-tour__btn" onClick={exit}>Exit</button>
      </div>
    </div>
  } : undefined;
  const points: PointDirective[] = state && stop && state.phase === "pointing" ? [{ id: stop.target, label: stop.title }] : [];
  return { start: () => { setGuidedTourActive(true); go(0); }, bubble, points, exit };
}
