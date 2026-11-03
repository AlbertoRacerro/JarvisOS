import { useSyncExternalStore } from "react";

/** Structural twin of the page-context type sent to the backend (see api/threads.ts). */
export type BubuUiContext = {
  route: string;
  title: string;
  targets: { id: string; role: string; name: string; text?: string }[];
};
export type BubuExtras = { ui_context?: BubuUiContext; images?: string[] };
export type BubuTurn = { id: string; role: "user" | "jarvis"; text: string; pending?: boolean; failed?: boolean; note?: string };

export type BubuConversation = {
  /** A project is selected, so Jarvis can be asked something. */
  ready: boolean;
  /** The most recent turns of the current thread, oldest first. */
  turns: BubuTurn[];
  pending: boolean;
  error: string | null;
  /** Set when the latest submit did not go through, so the composer can put the text back. */
  failedPrompt: string | null;
  submit(prompt: string, extras?: BubuExtras): Promise<void>;
  /** Ends the current thread: the next submit starts a new one. */
  sleep(): void;
};

/** `askClaude` opens the owner's Relay approval for a finished turn (the operator reviews before anything is sent). */
export type BubuHandlers = Pick<BubuConversation, "submit" | "sleep"> & { askClaude?(turnId: string): void };
export type BubuConversationState = Pick<BubuConversation, "ready" | "turns" | "pending" | "error">;

export const MAX_BUBU_TURNS = 8;

let handlers: BubuHandlers | null = null;
let relayEnabled = false;
const listeners = new Set<() => void>();

// Stable delegates: consumers never see a changing function identity, and the
// registered handlers can be replaced (or absent) without touching the snapshot.
const submit: BubuConversation["submit"] = (prompt, extras) => handlers ? handlers.submit(prompt, extras) : Promise.resolve();
const sleep: BubuConversation["sleep"] = () => { handlers?.sleep(); };

const EMPTY: BubuConversation = { ready: false, turns: [], pending: false, error: null, failedPrompt: null, submit, sleep };
let snapshot: BubuConversation = EMPTY;

function sameTurns(a: BubuTurn[], b: BubuTurn[]): boolean {
  return a.length === b.length && a.every((turn, index) => {
    const other = b[index];
    return turn.id === other.id && turn.role === other.role && turn.text === other.text && turn.note === other.note && Boolean(turn.pending) === Boolean(other.pending) && Boolean(turn.failed) === Boolean(other.failed);
  });
}

function emit(next: BubuConversation) {
  snapshot = next;
  for (const listener of [...listeners]) listener();
}

export function getBubuConversation(): BubuConversation { return snapshot; }

export function subscribeBubuConversation(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** Called by the conversation owner (useJarvisSidecar). Notifies only on a real change. */
export function publishBubuConversation(state: BubuConversationState) {
  const turns = state.turns.slice(-MAX_BUBU_TURNS);
  if (snapshot.ready === state.ready && snapshot.pending === state.pending && snapshot.error === state.error && sameTurns(snapshot.turns, turns)) return;
  emit({ ...snapshot, ready: state.ready, turns, pending: state.pending, error: state.error });
}

export function publishBubuFailedPrompt(failedPrompt: string | null) {
  if (snapshot.failedPrompt === failedPrompt) return;
  emit({ ...snapshot, failedPrompt });
}

/** Relay is available on this machine, so a turn the model could not fully read can be sent to Claude. */
export function publishBubuRelayEnabled(enabled: boolean) {
  if (relayEnabled === enabled) return;
  relayEnabled = enabled;
  for (const listener of [...listeners]) listener();
}

/** Opens the owner's Relay approval for this finished turn. A no-op without an owner. */
export function askClaudeAboutTurn(turnId: string) { handlers?.askClaude?.(turnId); }

/** Registers the owner's submit/sleep. Returns an unregister that also resets the snapshot. */
export function registerBubuHandlers(next: BubuHandlers): () => void {
  handlers = next;
  return () => {
    if (handlers !== next) return;
    handlers = null;
    relayEnabled = false;
    emit(EMPTY);
  };
}

export function useBubuRelayEnabled(): boolean {
  return useSyncExternalStore(subscribeBubuConversation, () => relayEnabled, () => false);
}

export function useBubuConversation(): BubuConversation {
  return useSyncExternalStore(subscribeBubuConversation, getBubuConversation, getBubuConversation);
}
