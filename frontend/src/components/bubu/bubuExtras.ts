// Pure helpers for what Bubu sends with a message and how a reply drives the pointer.
type Target = { id: string; role: string; name: string; text?: string };
type Context = { route: string; title: string; targets: Target[] };
type Turn = { id: string; role: "user" | "jarvis"; text: string; pending?: boolean; failed?: boolean };

export const MAX_IMAGES = 2;

/** Targets the user picked ("this one") always reach the model, first, even when the page list is full. */
export function withPicked(context: Context, picked: readonly Target[], cap = 150): Context {
  const ids = new Set(picked.map((target) => target.id));
  return { ...context, targets: [...picked, ...context.targets.filter((target) => !ids.has(target.id))].slice(0, cap) };
}

/** A one-off screenshot first, then the live vision frame; never more than the backend accepts. */
export function pickImages(oneOff: string | null, visionFrame: string | null): string[] | undefined {
  const images = [oneOff, visionFrame].filter((image): image is string => !!image).slice(0, MAX_IMAGES);
  return images.length ? images : undefined;
}

/** The newest finished Jarvis reply: its directives are the pointers on screen. */
export function latestReply(turns: readonly Turn[]): Turn | null {
  for (let index = turns.length - 1; index >= 0; index--) {
    const turn = turns[index];
    if (turn.role === "user") return null;
    if (!turn.pending && !turn.failed) return turn;
  }
  return null;
}

/** How a picked element is quoted in the draft. */
export const pickedMention = (target: Target): string => `“${target.name || target.id}”`;
