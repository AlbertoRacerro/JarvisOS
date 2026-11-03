import { useSyncExternalStore } from "react";
import type { BubuRect } from "./bubblePlacement";

// Bubu's current on-screen rect, shared without re-rendering the shell on every drag step.
let rect: BubuRect | null = null;
const listeners = new Set<() => void>();

export function publishBubuRect(next: BubuRect | null) {
  if (rect === next || (rect && next && rect.left === next.left && rect.top === next.top && rect.width === next.width && rect.height === next.height)) return;
  rect = next;
  for (const listener of [...listeners]) listener();
}

export const getBubuRect = () => rect;
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export function useBubuRect(): BubuRect | null { return useSyncExternalStore(subscribe, getBubuRect, getBubuRect); }
