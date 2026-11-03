// BUBU-3 (latex): formulas in Jarvis replies render through KaTeX. Parser rules + safety assertions.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(join(root, path), "utf8");
const { findMath, katexLoaded, loadKatex, matchMathBlock, renderMathHtml, unescapeDollars } = await import("../src/components/ai/mathText.ts");

const spans = (text) => { const out = []; let from = 0; for (let m = findMath(text, from); m; m = findMath(text, from)) { out.push(m); from = m.end; } return out; };
const srcs = (text) => spans(text).map((m) => m.src);

// ---- inline: $...$ and \(...\) ----------------------------------------------------------------------------
{
  assert.deepEqual(srcs("The rate is $r = k C^2$ here."), ["r = k C^2"]);
  assert.deepEqual(spans("a $x$ b")[0], { start: 2, end: 5, src: "x", display: false });
  assert.deepEqual(srcs("\\(E = mc^2\\) and $a$ and $b$"), ["E = mc^2", "a", "b"]);
  assert.equal(spans("\\(x\\)")[0].display, false);
  assert.equal(spans("so $$x^2$$ inline")[0].display, true);
  assert.equal(spans("so \\[x^2\\] inline")[0].display, true);
  assert.deepEqual(srcs("$\\frac{a}{b}$"), ["\\frac{a}{b}"]);
}

// ---- currency and other lone dollars are NOT math -----------------------------------------------------------
{
  for (const text of ["It costs $5 and $10.", "Between $5-$10 per kg", "pay $5, then $ 10", "a lone $ sign", "$ x $", "price: $5"]) {
    assert.deepEqual(spans(text), [], text);
  }
  assert.deepEqual(srcs("costs $5 and $x$ ok"), ["x"], "a formula never contains a bare dollar");
  assert.deepEqual(spans("empty $$ pair and \\(\\)"), []);
}

// ---- escapes and code ---------------------------------------------------------------------------------------
{
  assert.deepEqual(spans("a \\$x\\$ b"), []);
  assert.equal(unescapeDollars("a \\$x\\$ b"), "a $x$ b");
  assert.deepEqual(srcs("inside \\$ and $a\\$b$"), ["a\\$b"], "\\$ inside math stays LaTeX");
  assert.deepEqual(spans("use `$x$` literally"), []);
  assert.deepEqual(srcs("`$a$` but $b$"), ["b"]);
  assert.deepEqual(spans("`a \\(x\\) b`"), []);
}

// ---- block math -----------------------------------------------------------------------------------------------
{
  assert.deepEqual(matchMathBlock(["$$", "x^2 + y^2", "= z^2", "$$", "after"], 0), { src: "x^2 + y^2\n= z^2", next: 4 });
  assert.deepEqual(matchMathBlock(["  $$ E = mc^2 $$  "], 0), { src: "E = mc^2", next: 1 });
  assert.deepEqual(matchMathBlock(["$$x = 1$$."], 0), { src: "x = 1", next: 1 }, "trailing punctuation allowed");
  assert.deepEqual(matchMathBlock(["\\[", "\\int_0^1 f", "\\]"], 0), { src: "\\int_0^1 f", next: 3 });
  assert.deepEqual(matchMathBlock(["\\[ a = b \\]"], 0), { src: "a = b", next: 1 });
  assert.deepEqual(matchMathBlock(["$$", "x = 1 $$"], 0), { src: "x = 1", next: 2 });
  assert.equal(matchMathBlock(["$$x$$ and more text"], 0), null, "inline path handles it");
  assert.equal(matchMathBlock(["$$", "never closed", "", "next paragraph"], 0), null, "blank line aborts, nothing swallowed");
  assert.equal(matchMathBlock(["$$", "never closed"], 0), null);
  assert.equal(matchMathBlock(["$$ $$"], 0), null);
  assert.equal(matchMathBlock(["plain line"], 0), null);
}

// ---- rendering: KaTeX output, safe options, error fallback -------------------------------------------------------
{
  assert.equal(katexLoaded(), false, "KaTeX is not loaded until the first formula asks for it");
  assert.equal(renderMathHtml("x^2", false), null, "before loading, the caller shows the source");
  await Promise.all([loadKatex(), loadKatex()]);
  assert.equal(katexLoaded(), true);
  const html = renderMathHtml("x^2", false);
  assert.ok(html && html.includes('class="katex"') && !html.includes("katex-display"));
  assert.ok(renderMathHtml("x^2", true).includes("katex-display"));
  const seen = [];
  const fakeOk = (src, options) => { seen.push(options); return "<span>ok</span>"; };
  assert.equal(renderMathHtml("a", true, fakeOk), "<span>ok</span>");
  assert.deepEqual(seen[0], { throwOnError: false, trust: false, displayMode: true, strict: "ignore", maxSize: 20, maxExpand: 200 });
  assert.equal(renderMathHtml("a", false, () => { throw new Error("boom"); }), null, "exceptions fall back to null (caller shows the source)");
  // invalid LaTeX must never throw or inject a link
  assert.ok(typeof renderMathHtml("\\frac{", false) === "string");
  const link = renderMathHtml("\\href{javascript:alert(1)}{x}", false);
  assert.ok(!/<a[\s>]/i.test(link) && !/javascript:/i.test(link.replace(/<annotation[^>]*>.*?<\/annotation>/g, "")), "trust:false blocks \\href");
  assert.ok(!/<script/i.test(renderMathHtml("\\text{<script>alert(1)</script>}", false)), "raw tags in a formula are escaped");
}

// ---- source assertions ------------------------------------------------------------------------------------------
{
  const view = read("src/components/ai/JarvisMessageText.tsx");
  const math = read("src/components/ai/mathText.ts");
  const bubu = read("src/components/bubu/useBubuIntegration.tsx");
  assert.match(math, /trust: false/);
  assert.doesNotMatch(math, /trust: true|trust:true/);
  assert.match(math, /throwOnError: false/);
  assert.equal((view.match(/dangerouslySetInnerHTML/g) ?? []).length, 1, "only the KaTeX output is injected");
  assert.match(view, /dangerouslySetInnerHTML=\{\{ __html: html \}\}/);
  assert.match(view, /html === null\) return <code>\{src\}<\/code>/);
  assert.match(view, /import\("katex\/dist\/katex\.min\.css"\)/, "KaTeX CSS is lazy");
  assert.match(math, /import\("katex"\)/, "KaTeX JS is lazy");
  assert.doesNotMatch(view + math, /^import [^\n]*["']katex/m, "no static KaTeX import keeps it out of the main chunk");
  assert.doesNotMatch(view + math, /https?:\/\/[^"'\s]*katex/i, "fonts/CSS are bundled, no CDN");
  assert.match(bubu, /<JarvisMessageText text=\{parsePointDirectives\(text\)\.text\} \/>/);
  const pkg = JSON.parse(read("package.json"));
  assert.match(pkg.dependencies.katex, /^0\.16\.\d+$/, "katex is pinned exactly");
}

console.log("jarvis-math: ok");
