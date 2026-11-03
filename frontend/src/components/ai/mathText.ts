// LaTeX in Jarvis replies. Model text is untrusted: the only HTML ever injected is KaTeX output rendered with
// trust:false (no \href, \url, \includegraphics, \htmlClass...). Self-contained on purpose so node tests can import it.
// KaTeX itself (~260 kB) is loaded on demand by `loadKatex`, so it stays out of the main chunk.

export type MathMatch = { start: number; end: number; src: string; display: boolean };
export type MathBlock = { src: string; next: number };

const CODE_SPAN = /`[^`\n]+`/y;
const isSpace = (ch: string | undefined) => ch === undefined || /\s/.test(ch);
const isDigit = (ch: string | undefined) => ch !== undefined && ch >= "0" && ch <= "9";

// Index of the first unescaped `needle` at or after `from`, skipping `\x` pairs; -1 when absent.
function indexOfUnescaped(text: string, needle: string, from: number): number {
  for (let i = from; i < text.length; i += 1) {
    if (text[i] === "\\") { i += 1; continue; }
    if (text.startsWith(needle, i)) return i;
  }
  return -1;
}

// Closing `$` of an inline formula: Pandoc rules (no space before it, not followed by a digit), so "costs $5 and $10" stays text.
function closingDollar(text: string, from: number): number {
  for (let j = from; j < text.length; j += 1) {
    if (text[j] === "\\") { j += 1; continue; }
    if (text[j] !== "$") continue;
    return !isSpace(text[j - 1]) && !isDigit(text[j + 1]) ? j : -1; // a formula never contains a bare `$`
  }
  return -1;
}

/** Next math span at or after `from` in one line of text; never inside `code`; `\$` is a literal dollar. */
export function findMath(text: string, from = 0): MathMatch | null {
  for (let i = from; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "`") {
      CODE_SPAN.lastIndex = i;
      const code = CODE_SPAN.exec(text);
      if (code) i += code[0].length - 1;
      continue;
    }
    if (ch === "\\") {
      const next = text[i + 1];
      if (next === "(" || next === "[") {
        const close = text.indexOf(next === "(" ? "\\)" : "\\]", i + 2);
        if (close > i + 2 && text.slice(i + 2, close).trim()) return { start: i, end: close + 2, src: text.slice(i + 2, close), display: next === "[" };
      }
      i += 1;
      continue;
    }
    if (ch !== "$") continue;
    if (text[i + 1] === "$") {
      const close = indexOfUnescaped(text, "$$", i + 2);
      if (close > i + 2 && text.slice(i + 2, close).trim()) return { start: i, end: close + 2, src: text.slice(i + 2, close), display: true };
      i += 1;
      continue;
    }
    if (isSpace(text[i + 1])) continue;
    const close = closingDollar(text, i + 1);
    if (close > i + 1) return { start: i, end: close + 1, src: text.slice(i + 1, close), display: false };
  }
  return null;
}

/** Plain-text pieces show `\$` as `$`. */
export const unescapeDollars = (text: string) => text.replace(/\\\$/g, "$");

/** A display formula that owns its lines: `$$ ... $$` or `\[ ... \]`, possibly over several lines (no blank line inside). */
export function matchMathBlock(lines: string[], i: number): MathBlock | null {
  const first = lines[i].trim();
  for (const [open, close] of [["$$", "$$"], ["\\[", "\\]"]] as const) {
    if (!first.startsWith(open)) continue;
    const rest = first.slice(open.length);
    const body: string[] = [];
    let tail = "";
    let next = i + 1;
    const same = rest.indexOf(close);
    if (same >= 0) {
      body.push(rest.slice(0, same));
      tail = rest.slice(same + close.length);
    } else {
      body.push(rest);
      let found = -1;
      for (let j = i + 1; j < lines.length; j += 1) {
        if (!lines[j].trim()) return null;
        const k = lines[j].indexOf(close);
        if (k >= 0) { body.push(lines[j].slice(0, k)); tail = lines[j].slice(k + close.length); found = j; break; }
        body.push(lines[j]);
      }
      if (found < 0) return null;
      next = found + 1;
    }
    const src = body.join("\n").trim();
    if (!src || tail.trim().replace(/[.,;:]/g, "")) return null;
    return { src, next };
  }
  return null;
}

type KatexRender = (src: string, options: Record<string, unknown>) => string;

let katexRender: KatexRender | null = null;
let katexLoading: Promise<void> | null = null;

/** Loads KaTeX once; later calls reuse the same promise. A failed load may be retried. */
export function loadKatex(): Promise<void> {
  katexLoading ??= import("katex").then(
    (mod) => { katexRender = mod.default.renderToString; },
    (error) => { katexLoading = null; throw error; },
  );
  return katexLoading;
}

export const katexLoaded = () => katexRender !== null;

const cache = new Map<string, string | null>();

/** KaTeX HTML for a formula, or null when rendering threw or KaTeX is not loaded yet (the caller then shows the source as code). */
export function renderMathHtml(src: string, display: boolean, render?: KatexRender): string | null {
  const fn = render ?? katexRender;
  if (!fn) return null;
  const key = `${display ? "d" : "i"}:${src}`;
  if (!render && cache.has(key)) return cache.get(key) as string | null;
  let html: string | null;
  try {
    html = fn(src, { throwOnError: false, trust: false, displayMode: display, strict: "ignore", maxSize: 20, maxExpand: 200 });
  } catch {
    html = null;
  }
  if (!render) {
    if (cache.size >= 300) cache.clear();
    cache.set(key, html);
  }
  return html;
}
