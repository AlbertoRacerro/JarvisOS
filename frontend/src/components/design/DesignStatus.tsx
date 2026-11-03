import type { NodeState } from "./designNodeStatus";

// One status vocabulary for every Design panel: a dot and one word. Specifics go in the tooltip, not in sentences.
const LOOK: Record<NodeState, readonly [tone: string, glyph: string, word: string]> = {
  empty: ["missing", "○", "Empty"], ready: ["available", "●", "Ready"], running: ["running", "◐", "Running"],
  checking: ["running", "◐", "Checking"], stale: ["stale", "▲", "Stale"], failed: ["error", "✕", "Failed"], unknown: ["missing", "?", "Unknown"]
};

export function DesignStatus({ state, title }: Readonly<{ state: NodeState; title?: string }>) {
  const [tone, glyph, word] = LOOK[state];
  return <span className={`status-word status-word--${tone}`} title={title}><span aria-hidden="true">{glyph}</span> {word}</span>;
}

/** The value of a field that has nothing in it yet. */
export function Empty({ title }: Readonly<{ title?: string }>) {
  return <span className="design-empty" title={title}>empty</span>;
}
