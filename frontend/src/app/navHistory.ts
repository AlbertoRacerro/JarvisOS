import { useEffect, useState } from "react";

// The shell's Back/Forward arrows need to know whether there is an app page behind or ahead of the current one.
// Every entry the app pushes carries its position in history.state; how many entries the tab holds is kept in
// sessionStorage so a reload keeps Forward right. Without storage the arrows still work; after a reload Forward
// only knows the entries it has seen again.
const INDEX_FIELD = "jarvisNavIndex";
const COUNT_KEY = "jarvisos:nav-count:v1";
const CHANGE_EVENT = "jarvisos:navigation";

type HistoryState = Record<string, unknown>;

let lastIndex: number | null = null;
let entryCount: number | null = null;

const historyStateObject = (value: unknown): HistoryState => typeof value === "object" && value !== null ? { ...(value as HistoryState) } : {};

function stampedIndex(value: unknown): number | null {
  const index = historyStateObject(value)[INDEX_FIELD];
  return typeof index === "number" && Number.isInteger(index) && index >= 0 ? index : null;
}

function readCount(): number {
  if (entryCount === null) {
    entryCount = 1;
    try {
      const stored = Number(window.sessionStorage.getItem(COUNT_KEY));
      if (Number.isInteger(stored) && stored > 0) entryCount = stored;
    } catch { /* storage unavailable: count from this page load */ }
  }
  return entryCount;
}

function writeCount(count: number) {
  entryCount = count;
  try { window.sessionStorage.setItem(COUNT_KEY, String(count)); } catch { /* best-effort */ }
}

/** The current entry's position; an entry the app did not push (the first load, a fragment link) is stamped here. */
function currentIndex(): number {
  const stamped = stampedIndex(window.history.state);
  if (stamped !== null) {
    // An entry seen here exists, so the tab holds at least this many (this restores Forward after a storage-less reload).
    if (readCount() <= stamped) writeCount(stamped + 1);
    lastIndex = stamped;
    return stamped;
  }
  const index = lastIndex === null ? 0 : lastIndex + 1;
  window.history.replaceState({ ...historyStateObject(window.history.state), [INDEX_FIELD]: index }, "", window.location.href);
  writeCount(index + 1);
  lastIndex = index;
  return index;
}

/** Push a new entry; it drops whatever was ahead, as the browser does. */
export function pushHistoryEntry(state: HistoryState, url: string): void {
  const index = currentIndex() + 1;
  window.history.pushState({ ...state, [INDEX_FIELD]: index }, "", url);
  lastIndex = index;
  writeCount(index + 1);
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

/** Replace the current entry, keeping its position. */
export function replaceHistoryEntry(state: HistoryState, url: string): void {
  window.history.replaceState({ ...state, [INDEX_FIELD]: currentIndex() }, "", url);
}

export type HistoryPosition = Readonly<{ canGoBack: boolean; canGoForward: boolean }>;

export function readHistoryPosition(): HistoryPosition {
  const index = currentIndex();
  return { canGoBack: index > 0, canGoForward: index < readCount() - 1 };
}

export function useHistoryPosition(): HistoryPosition {
  // Rendered disabled until mounted: the server render and the first paint never read window.
  const [position, setPosition] = useState<HistoryPosition>({ canGoBack: false, canGoForward: false });
  useEffect(() => {
    const update = () => {
      const { canGoBack, canGoForward } = readHistoryPosition();
      setPosition((current) => current.canGoBack === canGoBack && current.canGoForward === canGoForward ? current : { canGoBack, canGoForward });
    };
    update();
    window.addEventListener("popstate", update);
    window.addEventListener(CHANGE_EVENT, update);
    return () => { window.removeEventListener("popstate", update); window.removeEventListener(CHANGE_EVENT, update); };
  }, []);
  return position;
}
