import { useEffect, useState, type ReactNode } from "react";
import { findMath, loadKatex, matchMathBlock, renderMathHtml, unescapeDollars } from "./mathText";

// Model output is untrusted text. This renders a small, readable subset of
// Markdown (paragraphs, lists, fenced code, `code`, **bold**) as plain React
// elements: no HTML parsing, no links, no execution. The one exception is LaTeX
// ($x$, $$x$$, \(x\), \[x\]): KaTeX output (trust:false) is the only HTML injected.

// KaTeX JS and CSS load on the first formula; until then (or if loading fails) the source shows as code.
let mathStyles: Promise<unknown> | null = null;
const loadMath = () => Promise.all([loadKatex(), (mathStyles ??= import("katex/dist/katex.min.css"))]);
let mathReady = false;

function useMathReady(): boolean {
  const [ready, setReady] = useState(mathReady);
  useEffect(() => {
    if (ready) return;
    let live = true;
    loadMath().then(() => { mathReady = true; if (live) setReady(true); }, () => { mathStyles = null; });
    return () => { live = false; };
  }, [ready]);
  return ready;
}

function MathSpan({ src, display, block }: { src: string; display: boolean; block?: boolean }) {
  const ready = useMathReady();
  const html = ready ? renderMathHtml(src, display) : null;
  if (html === null) return <code>{src}</code>;
  const Tag = block ? "div" : "span";
  return <Tag className={block ? "jarvis-math-block" : "jarvis-math"} dangerouslySetInnerHTML={{ __html: html }} />;
}

// Bubu (spec 231) wraps a pointed-at target's name in private-use marks: \uE000name\uE001 is emphasised and
// \uE002note\uE003 is a muted note. They are split out before Markdown/LaTeX so a name is always plain text.
const MARK = /\uE000([^\uE000-\uE003\n]*)\uE001|\uE002([^\uE000-\uE003\n]*)\uE003/g;
const stripMarks = (text: string) => text.replace(/[\uE000-\uE003]/g, "");

function inline(text: string, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = [];
  let last = 0;
  let index = 0;
  for (const match of text.matchAll(MARK)) {
    const at = match.index ?? 0;
    if (at > last) parts.push(...markdownInline(stripMarks(text.slice(last, at)), `${keyPrefix}-t${index}`));
    parts.push(match[1] !== undefined
      ? <strong key={`${keyPrefix}-m${index++}`} className="jarvis-message-text__target">{match[1]}</strong>
      : <span key={`${keyPrefix}-m${index++}`} className="jarvis-message-text__note">{match[2]}</span>);
    last = at + match[0].length;
  }
  if (last < text.length) parts.push(...markdownInline(stripMarks(text.slice(last)), `${keyPrefix}-t${index}`));
  return parts;
}

function markdownInline(text: string, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
  let last = 0;
  let index = 0;
  const plain = (to: number) => { if (to > last) parts.push(unescapeDollars(text.slice(last, to))); };
  while (last < text.length) {
    pattern.lastIndex = last;
    const match = pattern.exec(text);
    const math = findMath(text, last);
    if (math && (!match || math.start < match.index)) {
      plain(math.start);
      parts.push(<MathSpan key={`${keyPrefix}-${index++}`} src={math.src} display={math.display} />);
      last = math.end;
    } else if (match) {
      plain(match.index);
      const token = match[0];
      parts.push(token.startsWith("`")
        ? <code key={`${keyPrefix}-${index++}`}>{token.slice(1, -1)}</code>
        : <strong key={`${keyPrefix}-${index++}`}>{markdownInline(token.slice(2, -2), `${keyPrefix}-${index}`)}</strong>);
      last = match.index + token.length;
    } else break;
  }
  plain(text.length);
  return parts;
}

type Block =
  | { kind: "math"; src: string }
  | { kind: "p"; lines: string[] }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "code"; lines: string[] }
  | { kind: "h"; text: string };

function parse(text: string): Block[] {
  const blocks: Block[] = [];
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      const code: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i++]);
      i += 1;
      blocks.push({ kind: "code", lines: code });
      continue;
    }
    if (!line.trim()) { i += 1; continue; }
    const math = matchMathBlock(lines, i);
    if (math) { blocks.push({ kind: "math", src: math.src }); i = math.next; continue; }
    const heading = /^#{1,4}\s+(.*)$/.exec(line.trim());
    if (heading) { blocks.push({ kind: "h", text: heading[1] }); i += 1; continue; }
    const bullet = /^\s*[-*•]\s+(.*)$/;
    const numbered = /^\s*\d+[.)]\s+(.*)$/;
    const listPattern = bullet.test(line) ? bullet : numbered.test(line) ? numbered : null;
    if (listPattern) {
      const items: string[] = [];
      while (i < lines.length && listPattern.test(lines[i])) {
        items.push((listPattern.exec(lines[i]) as RegExpExecArray)[1]);
        i += 1;
      }
      blocks.push({ kind: listPattern === bullet ? "ul" : "ol", items });
      continue;
    }
    const paragraph: string[] = [];
    while (i < lines.length && lines[i].trim() && !lines[i].trim().startsWith("```") && !bullet.test(lines[i]) && !numbered.test(lines[i]) && !matchMathBlock(lines, i)) paragraph.push(lines[i++]);
    blocks.push({ kind: "p", lines: paragraph });
  }
  return blocks;
}

/** `marks` opts in to Bubu's target-name marks; any other caller gets them stripped as plain text. */
export default function JarvisMessageText({ text, marks = false }: { text: string; marks?: boolean }) {
  return <div className="jarvis-message-text">{parse(marks ? text : stripMarks(text)).map((block, index) => {
    const key = `b${index}`;
    if (block.kind === "math") return <MathSpan key={key} src={stripMarks(block.src)} display block />;
    if (block.kind === "code") return <pre key={key}><code>{stripMarks(block.lines.join("\n"))}</code></pre>;
    if (block.kind === "h") return <p key={key} className="jarvis-message-text__heading">{inline(block.text, key)}</p>;
    if (block.kind === "ul") return <ul key={key}>{block.items.map((item, n) => <li key={n}>{inline(item, `${key}-${n}`)}</li>)}</ul>;
    if (block.kind === "ol") return <ol key={key}>{block.items.map((item, n) => <li key={n}>{inline(item, `${key}-${n}`)}</li>)}</ol>;
    return <p key={key}>{block.lines.flatMap((line, n) => n ? [<br key={`br${n}`} />, ...inline(line, `${key}-${n}`)] : inline(line, `${key}-${n}`))}</p>;
  })}</div>;
}
