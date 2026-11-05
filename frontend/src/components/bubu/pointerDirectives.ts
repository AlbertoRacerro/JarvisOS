// BUBU-2: "[[point:<id>]]" / "[[point:<id>|<label>]]" directives in a Jarvis reply. Self-contained (no local imports).

export type PointDirective = { id: string; label?: string };

export const MAX_POINTS = 5;

// Surrounding horizontal whitespace is captured so removal never leaves a double space (and never touches other spacing).
const DIRECTIVE = /([ \t]*)\[\[\s*point\s*:\s*([\w.:-]+)\s*(?:\|([^\]\n]*))?\]\]([ \t]*)/gi;
const JOIN = "\u0001";

export function parsePointDirectives(text: string): { text: string; points: PointDirective[] } {
  const points: PointDirective[] = [];
  const seen = new Set<string>();
  let removed = false;
  const out = text.replace(DIRECTIVE, (all: string, before: string, id: string, label: string | undefined, after: string, offset: number) => {
    removed = true;
    if (!seen.has(id) && points.length < MAX_POINTS) {
      seen.add(id);
      const l = label?.trim();
      points.push(l ? { id, label: l } : { id });
    }
    if (!before && !after) return "";
    const prev = offset > 0 ? text[offset - 1] : "\n";
    const next = text[offset + all.length];
    // Leading edge of a line, or followed by end/newline/closing punctuation: drop the gap; otherwise keep one space.
    return prev === "\n" || next === undefined || /[\n.,;:!?)\]}]/.test(next) ? "" : JOIN;
  });
  return { text: removed ? out.replace(/\u0001+/g, " ").trimEnd() : text, points };
}

// Spec 231: a directive reads as the name of what it points at. Private-use marks wrap the name so the message renderer
// can emphasise it as plain text; marks already in the model's text are dropped so a reply cannot forge one.
export const NAME_OPEN = "";
export const NAME_CLOSE = "";
export const NOTE_OPEN = "";
export const NOTE_CLOSE = "";
const MARKS = /[-]/g;
const MAX_NAME = 60;
const SHORT_NAME = 48;

const tidy = (value: string | null | undefined) => (value ?? "").replace(MARKS, "").replace(/\s+/g, " ").trim();
const cap = (value: string) => (value.length > MAX_NAME ? `${value.slice(0, MAX_NAME - 1).trimEnd()}…` : value);

/**
 * `nameOf(id)` is the visible target's current name: null when the target is not on the page, "" when it has no name.
 * The visible name wins when it is short; the directive label is the fallback. Nothing is invented for a missing target.
 */
export function readablePointText(text: string, nameOf: (id: string) => string | null): string {
  const source = text.replace(MARKS, "");
  const out = source.replace(DIRECTIVE, (_all: string, before: string, id: string, label: string | undefined, after: string) => {
    const name = nameOf(id);
    const given = tidy(label);
    let shown: string;
    if (name === null) {
      shown = given ? `${NAME_OPEN}${cap(given)}${NAME_CLOSE}${NOTE_OPEN} (not shown here)${NOTE_CLOSE}` : `${NOTE_OPEN}an item that isn't shown here${NOTE_CLOSE}`;
    } else {
      const visible = tidy(name);
      const pick = visible && (visible.length <= SHORT_NAME || !given) ? visible : given || "the highlighted item";
      shown = `${NAME_OPEN}${cap(pick)}${NAME_CLOSE}`;
    }
    return `${before}${shown}${after}`;
  });
  // "**[[point:x]]**" or "`[[point:x]]`" would otherwise leave stray markers around the emphasised name.
  return out.replace(/(\*\*|`)([^-]*(?:[^-]*)?)\1/g, "$2");
}
