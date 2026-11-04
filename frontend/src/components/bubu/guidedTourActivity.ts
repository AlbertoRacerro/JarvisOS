import { useSyncExternalStore } from "react";

let active = false;
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};
const snapshot = () => active;
const serverSnapshot = () => false;

export function setGuidedTourActive(next: boolean): void {
  if (active === next) return;
  active = next;
  for (const listener of listeners) listener();
}

export function useGuidedTourActive(): boolean {
  return useSyncExternalStore(subscribe, snapshot, serverSnapshot);
}
