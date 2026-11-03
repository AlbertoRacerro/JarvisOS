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
