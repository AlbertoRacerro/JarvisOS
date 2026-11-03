import { useCallback, useEffect, useRef, useState } from "react";
import { describeElement, isBubuUi, isRenderable, pickableElement, type UiTarget } from "./pageContext";

// "This one": while active, the next click anywhere on the page is swallowed (the page action does NOT run) and
// reported as a UiTarget instead. Hover is shown with one overlay box; Esc cancels. Bubu's own UI is left alone.

const BLOCKED = ["pointerdown", "pointerup", "mousedown", "mouseup", "auxclick", "dblclick", "contextmenu"] as const;
const CURSOR_CSS = "html, html *:not([data-bubu-ui], [data-bubu-ui] *, #bubu-launcher) { cursor: crosshair !important; }";

function pageElement(e: Event): Element | null {
  const t = e.target;
  if (!(t instanceof Element) || isBubuUi(t)) return null;
  return t === document.documentElement || t === document.body ? null : pickableElement(t);
}

export function useUserPointer(onPick: (target: UiTarget) => void): { active: boolean; start(): void; cancel(): void } {
  const [active, setActive] = useState(false);
  const pickRef = useRef(onPick);
  pickRef.current = onPick;
  const start = useCallback(() => setActive(true), []);
  const cancel = useCallback(() => setActive(false), []);

  useEffect(() => {
    if (!active) return;
    const style = document.createElement("style");
    style.setAttribute("data-bubu-ui", "user-pointer-cursor");
    style.textContent = CURSOR_CSS;
    document.head.appendChild(style);
    const box = document.createElement("div");
    box.setAttribute("data-bubu-ui", "user-pointer");
    box.setAttribute("aria-hidden", "true");
    Object.assign(box.style, { position: "fixed", zIndex: "44", pointerEvents: "none", display: "none", boxSizing: "border-box", border: "2px solid var(--accent-seed, #8db943)", borderRadius: "6px", background: "color-mix(in srgb, var(--accent-seed, #8db943) 14%, transparent)", boxShadow: "0 0 0 1px rgb(0 0 0 / 35%), 0 0 8px var(--accent-seed, #8db943)" });
    document.body.appendChild(box);

    const hide = () => { box.style.display = "none"; };
    const onMove = (e: Event) => {
      const el = pageElement(e);
      if (!el || !isRenderable(el)) return hide();
      const r = el.getBoundingClientRect();
      Object.assign(box.style, { display: "block", left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
    };
    const swallow = (e: Event) => { if (pageElement(e)) { e.preventDefault(); e.stopPropagation(); } };
    const onClick = (e: Event) => {
      const el = pageElement(e);
      if (!el) return;
      e.preventDefault();
      e.stopPropagation();
      const target = describeElement(el);
      setActive(false);
      pickRef.current(target);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      setActive(false);
    };

    document.addEventListener("pointermove", onMove, true);
    document.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("blur", hide);
    for (const type of BLOCKED) document.addEventListener(type, swallow, true);
    return () => {
      document.removeEventListener("pointermove", onMove, true);
      document.removeEventListener("click", onClick, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("blur", hide);
      for (const type of BLOCKED) document.removeEventListener(type, swallow, true);
      box.remove();
      style.remove();
    };
  }, [active]);

  return { active, start, cancel };
}
