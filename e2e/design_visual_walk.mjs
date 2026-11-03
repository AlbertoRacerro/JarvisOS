#!/usr/bin/env node
// Spec 230 acceptance 8: visual proof of the focused Workshop, Python and math rendering, the inspectable CFD field,
// the charts and the status language. A fresh browser profile opens "/" and reaches every surface through visible
// controls only, in Light and Dark at a wide (1920x1080) and a narrower (1440x900) desktop viewport. It measures the
// explanation/code pair, reads the computed colours of real Python tokens and the math font, inspects the field by
// pointer and keyboard, expands a chart and captures empty, error and keyboard-focus states. Screenshots are evidence
// for the coordinator to open and inspect; a DOM assertion here is not a layout proof by itself.
// The SOURCE data root (the 222 importer's C1 Example) is snapshotted into a fresh temporary directory and the backend
// runs against the copy; the source is opened read-only. No solver or simulation runs; only GETs and read-only previews.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/design_visual_walk.mjs <evidence-dir> <source-data-root>
// Live check (acceptance 9): JARVISOS_PROOF_LIVE_BASE=http://127.0.0.1:8000 node ... <evidence-dir> walks the running app.
// The one error state is produced by answering the field request of one run with HTTP 503 inside the browser
// (page.route); it is recorded as an intercepted presentation check, not as backend behaviour.
// Env: JARVISOS_PROOF_PORT (8230), PLAYWRIGHT_MODULE.
import { createHash } from "node:crypto";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { DESIGN_TARGETS } from "../frontend/src/app/designTargets.ts";

if (process.argv.includes("--help")) { console.log('node e2e/design_visual_walk.mjs <evidence-dir> <source-data-root>  (needs built frontend, Playwright)'); process.exit(0); }
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8230);
const live = process.env.JARVISOS_PROOF_LIVE_BASE?.replace(/\/+$/, "") || null;
const base = live ?? `http://127.0.0.1:${port}`;
const evidence = resolve(process.argv[2] ?? await mkdtemp(join(tmpdir(), "jarvisos-230-walk-")));
const sourceRoot = resolve(process.argv[3] ?? "");
const assert = (condition, message) => { if (!condition) throw new Error(message); };
assert(live || process.argv[3], "source data root required");
await mkdir(evidence, { recursive: true });

let dataRoot = null, snapshot = null, server = null;
if (!live) {
  dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-230-walk-data-"));
  await cp(sourceRoot, dataRoot, { recursive: true, filter: (path) => !/jarvisos\.db(-wal|-shm|-journal)?$/.test(path) });
  snapshot = spawnSync(python, ["-I", "-c", [
    "import sqlite3,sys",
    "src=sqlite3.connect('file:'+sys.argv[2]+'/jarvisos.db?mode=ro',uri=True); dst=sqlite3.connect(sys.argv[1]+'/jarvisos.db'); src.backup(dst)",
    "n=dst.execute(\"UPDATE artifacts SET stored_path=? || substr(stored_path, ?) WHERE stored_path LIKE ? || '%'\",(sys.argv[1],len(sys.argv[2])+1,sys.argv[2])).rowcount",
    "dst.commit(); print(n)"].join("\n"), dataRoot, sourceRoot], { encoding: "utf8" });
  assert(snapshot.status === 0, `data-root snapshot failed: ${snapshot.stderr}`);
  await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });
  const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host") };
  delete env.JARVISOS_BLUECAD_TOOL_REGISTRY;
  await new Promise((ok, fail) => {
    const probe = createServer().once("error", () => fail(new Error(`port ${port} is in use; refusing to use a foreign backend`)))
      .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1");
  });
  const log = openSync(join(evidence, "backend.log"), "w");
  server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], { cwd: backend, env, stdio: ["ignore", log, log] });
}
const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const dirty = spawnSync("git", ["status", "--porcelain"], { cwd: root, encoding: "utf8" }).stdout.trim();

const RUNS = [
  { name: "light-wide", colorScheme: "light", viewport: { width: 1920, height: 1080 } },
  { name: "dark-wide", colorScheme: "dark", viewport: { width: 1920, height: 1080 } },
  { name: "light-narrow", colorScheme: "light", viewport: { width: 1440, height: 900 } },
  { name: "dark-narrow", colorScheme: "dark", viewport: { width: 1440, height: 900 } }
];
// JARVISOS_PROOF_ONLY=light-wide,... limits the runs while iterating; a proof run uses all four (summary.passed requires it).
const ONLY = process.env.JARVISOS_PROOF_ONLY?.split(",").filter(Boolean) ?? null;
const PREVIEW_POSTS = [/^POST \/workspaces\/[^/]+\/actions\/brief$/, /^POST \/workspaces\/[^/]+\/model-implementations\/[^/]+\/binding-preview$/,
  /^POST \/workspaces\/[^/]+\/studio\/models\/[^/]+\/derive$/, /^POST \/workspaces\/[^/]+\/studio\/derive$/];
const summary = { spec: "230", acceptance: live ? "9 live check on the running app" : "8 visual proof", head, worktree_clean: !dirty, browser: null,
  started_at: new Date().toISOString(), ...(live ? { live_base: base, served: await fetch(`${base}/health`).then((r) => r.json()).catch(() => null) } : {
    source_data_root: sourceRoot, working_copy: "fresh temporary snapshot (files copied, SQLite via backup API); source opened read-only",
    artifact_paths_rewritten_in_copy: Number(snapshot.stdout.trim()) }), runs: {} };

// WCAG relative luminance contrast of two computed rgb() colours (alpha ignored; backgrounds here are opaque).
const rgb = (text) => (text.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
const lum = ([r, g, b]) => { const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
const contrast = (a, b) => { const [x, y] = [lum(rgb(a)), lum(rgb(b))].sort((m, n) => n - m); return (x + 0.05) / (y + 0.05); };

try {
  for (let i = 0; ; i++) {
    assert(!server || server.exitCode === null, "backend exited; see backend.log");
    try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* starting */ }
    assert(i < 120, "backend did not become healthy");
    await delay(500);
  }
  const workspaces = await (await fetch(`${base}/workspaces`)).json();
  const example = workspaces.find((item) => item.slug === "example-c1-smooth-tube");
  assert(example, "the C1 Example is not in the data root");
  summary.served_index = await fetch(`${base}/`).then((r) => r.text()).then((html) => html.match(/assets\/index-[\w-]+\.js/)?.[0] ?? null).catch(() => null);

  const browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  for (const run of RUNS.filter((item) => !ONLY || ONLY.includes(item.name))) {
    const record = { colorScheme: run.colorScheme, viewport: run.viewport, steps: [], screenshots: [], console_errors: [], page_errors: [],
      write_requests: [], preview_posts: [], error_responses: [], expected_refusals: [], intercepted: [], foreign_requests: [], targets_seen: {}, measurements: {} };
    summary.runs[run.name] = record;
    const context = await browser.newContext({ viewport: run.viewport, colorScheme: run.colorScheme });
    await context.addInitScript(() => { try { window.__proofInitialStorage = { local: localStorage.length, session: sessionStorage.length }; } catch { /* ignore */ } });
    const page = await context.newPage();
    page.setDefaultTimeout(45_000);
    page.on("console", (m) => { if (m.type() === "error") record.console_errors.push(m.text()); });
    page.on("pageerror", (e) => record.page_errors.push(String(e)));
    record.document_loads = 0;
    page.on("load", () => { record.document_loads += 1; });
    page.on("request", (r) => {
      const url = r.url();
      if (!url.startsWith(base) && !url.startsWith("data:") && !url.startsWith("blob:")) record.foreign_requests.push(url);
      if (["GET", "HEAD", "OPTIONS"].includes(r.method())) return;
      const call = `${r.method()} ${url.replace(base, "").split("?")[0]}`;
      (PREVIEW_POSTS.some((pattern) => pattern.test(call)) ? record.preview_posts : record.write_requests).push(call);
    });
    page.on("response", (response) => {
      if (response.status() < 400) return;
      const line = `${response.status()} ${response.request().method()} ${response.url().replace(base, "")}`;
      if (response.status() === 409 && /\/components\/[^/]+\/fluid-domain$/.test(response.url())) { record.expected_refusals.push(line); return; }
      (record.intercepted.some((item) => line.includes(item.path)) ? record.intercepted.find((item) => line.includes(item.path)).responses : record.error_responses).push(line);
    });
    const shot = async (name, locator = null) => {
      const file = join(evidence, `${run.name}-${String(record.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
      if (locator) await locator.screenshot({ path: file }); else await page.screenshot({ path: file, fullPage: false });
      record.screenshots.push({ file: file.slice(evidence.length + 1), sha256: createHash("sha256").update(await readFile(file)).digest("hex") });
      return file.slice(evidence.length + 1);
    };
    const seeTargets = async () => {
      for (const id of await page.locator("[data-jarvis-target]").evaluateAll((els) => els.map((el) => el.getAttribute("data-jarvis-target")))) {
        assert(DESIGN_TARGETS[id] || id?.startsWith("design.tree.component."), `unregistered target ${id}`);
        record.targets_seen[id] = (record.targets_seen[id] ?? 0) + 1;
      }
    };
    const step = async (name, work) => {
      try {
        const detail = (await work()) ?? {};
        await seeTargets();
        record.steps.push({ step: name, status: "pass", ...detail });
      } catch (error) {
        record.steps.push({ step: name, status: "fail", error: String(error).slice(0, 900), screenshot: await shot(`FAILED-${name}`).catch(() => null) });
      }
    };
    const box = (locator) => locator.evaluate((el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }; });
    const stages = page.getByRole("navigation", { name: "Design stages" });
    const tree = page.getByRole("tree", { name: "Component model tree" });

    await step("open_example", async () => {
      await page.goto(`${base}/`, { waitUntil: "networkidle" });
      const initial = await page.evaluate(() => window.__proofInitialStorage);
      assert(initial && initial.local === 0 && initial.session === 0, `profile was not clean: ${JSON.stringify(initial)}`);
      assert(await page.evaluate(() => document.documentElement.dataset.theme) === run.colorScheme, "system appearance not followed");
      const picker = page.getByRole("combobox", { name: "Project version", exact: true });
      const option = picker.locator('option[value^="case:"]').filter({ hasText: /C1 · smooth tube/ });
      await option.waitFor({ state: "attached" });
      const label = await option.innerText();
      await picker.selectOption(await option.getAttribute("value"));
      await page.getByRole("heading", { level: 1, name: /case study C1 smooth tube/ }).waitFor();
      return { via: `Project version picker: ${label}`, screenshot: await shot("design") };
    });

    await step("workshop_focused", async () => {
      await stages.getByRole("button", { name: "CFD", exact: true }).click();
      await page.getByText("Library", { exact: true }).click();
      await page.locator('.design-library__row[title*="c1_tube"]').first().click();
      await page.getByRole("heading", { level: 1, name: "Workshop" }).waitFor();
      const pair = page.locator(".studio-math-workspace");
      await pair.waitFor();
      await page.locator(".studio-code-editor .cm-content").waitFor();
      await page.waitForTimeout(500);
      const main = await box(page.locator("#app-main"));
      const measured = await box(pair);
      const usable = { width: main.width, height: Math.min(main.height, run.viewport.height) };
      const share = { width: +(measured.width / usable.width).toFixed(3), height: +(measured.height / usable.height).toFixed(3) };
      record.measurements.workshop = { pair: measured, usable_main: main, share };
      assert(share.width >= 0.85, `pair uses ${share.width} of the usable width`);
      assert(share.height >= 0.70, `pair uses ${share.height} of the usable height`);
      assert(await page.getByRole("button", { name: "Show library" }).isVisible(), "Library is not collapsed behind a visible control");
      assert(await page.getByRole("button", { name: "Open Jarvis" }).or(page.getByRole("button", { name: /Jarvis/ })).first().isVisible(), "Jarvis is not reachable");
      return { share, screenshot: await shot("workshop") };
    });

    await step("python_rendering", async () => {
      const editor = page.locator(".studio-code-editor");
      // Real tokens of the C1 calculation source, read where the browser painted them (VS Code Dark+ HighlightStyle spans + semantic marks).
      const reading = await editor.evaluate(async (host) => {
        // CodeMirror paints only the visible lines: scroll through the source and read tokens as they are painted.
        const pick = {};
        const scroller = host.querySelector(".cm-scroller"), content = host.querySelector(".cm-content");
        const plain = getComputedStyle(content).color;
        for (let top = 0; ; top += Math.max(100, scroller.clientHeight - 40)) {
          scroller.scrollTop = top;
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
          for (const el of host.querySelectorAll(".cm-line span")) {
            const text = el.textContent.trim();
            if (!text) continue;
            const color = getComputedStyle(el).color;
            if (color === plain && !el.className) continue;
            let kind = null;
            if (/^(def|for|if|in|raise|return|and|or|not)$/.test(text)) kind = "keyword";
            else if (text.startsWith("#")) kind = "comment";
            else if (/^['"]/.test(text)) kind = "string";
            else if (/^-?\d/.test(text)) kind = "number";
            else if (el.classList.contains("cm-py-constant") || /^[A-Z][A-Z0-9_]+$/.test(text)) kind = "constant";
            else if (/^evaluate$|^abs$|^repr$|^ValueError$/.test(text)) kind = "function";
            else if (/^[a-z_][a-z0-9_]*$/i.test(text) && color !== plain) kind = "variable";
            if (kind && !pick[kind]) pick[kind] = { text: text.slice(0, 40), color };
          }
          if (top >= scroller.scrollHeight) break;
        }
        scroller.scrollTop = 0;
        const bg = getComputedStyle(host).backgroundColor;
        return { tokens: pick, plain, background: bg, font: getComputedStyle(scroller).fontFamily, size: parseFloat(getComputedStyle(scroller).fontSize),
          gutters: !!host.querySelector(".cm-lineNumbers .cm-gutterElement"), guides: host.querySelectorAll(".cm-indent-guide").length };
      });
      const colours = {};
      for (const [kind, found] of Object.entries(reading.tokens)) {
        colours[kind] = { ...found, contrast: +contrast(found.color, reading.background).toFixed(2) };
      }
      for (const kind of ["keyword", "string", "number", "comment"]) assert(colours[kind], `no ${kind} token found in the source`);
      const distinct = new Set(Object.values(colours).map((item) => item.color).concat(reading.plain));
      assert(distinct.size === Object.keys(colours).length + 1 || distinct.size >= 6, `token colours are not distinct: ${[...distinct].join(", ")}`);
      for (const [kind, item] of Object.entries(colours)) assert(item.contrast >= 4.5, `${kind} contrast ${item.contrast} below 4.5`);
      assert(/(IBM Plex Mono|Consolas|Cascadia Code|Courier New|monospace)/i.test(reading.font) && reading.size >= 14 && reading.size <= 16, `editor face/size ${reading.font} ${reading.size}`);
      assert(reading.gutters, "line numbers missing");
      // Bracket matching and a visible selection: place the cursor after the first "(" and select a word.
      await editor.evaluate(async (host) => {
        const scroller = host.querySelector(".cm-scroller");
        for (let top = 0; top < scroller.scrollHeight; top += 200) {
          scroller.scrollTop = top;
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
          const found = [...host.querySelectorAll(".cm-line")].find((el) => el.textContent.includes("def evaluate("));
          if (found) { found.scrollIntoView({ block: "center" }); return; }
        }
      });
      const line = page.locator(".studio-code-editor .cm-line", { hasText: "def evaluate(" }).first();
      await line.click({ position: { x: 5, y: 5 } });
      await page.keyboard.press("End");
      await page.keyboard.press("Home");
      const paren = await line.evaluate((el) => el.textContent.indexOf("("));
      for (let i = 0; i <= paren; i += 1) await page.keyboard.press("ArrowRight");
      const matching = await page.locator(".studio-code-editor .cm-matchingBracket").count();
      await page.keyboard.press("Shift+End");
      const selection = await page.evaluate(() => window.getSelection()?.toString().length ?? 0);
      record.measurements.python = { ...reading, tokens: undefined, colours, distinct: distinct.size, matching_brackets: matching, selected_characters: selection };
      assert(selection > 0, "no visible selection");
      assert(matching >= 1, "bracket matching not shown");
      return { colours, font: reading.font, size_px: reading.size, matching, screenshot: await shot("python", page.locator(".studio-code-pane")) };
    });

    await step("math_rendering", async () => {
      const pane = page.locator(".studio-math-pane");
      const reading = await pane.evaluate(async (el) => {
        await document.fonts.ready;
        const math = el.querySelector("math");
        const text = el.innerText;
        const unrenderedEl = el.querySelector(".studio-unrendered");
        return { math_count: el.querySelectorAll("math").length, family: math && getComputedStyle(math).fontFamily, size: math && getComputedStyle(math).fontSize,
          loaded: document.fonts.check('20px "Latin Modern Math"'), body_family: getComputedStyle(el).fontFamily,
          raw_markup: /\\frac|\\mathrm|<math|<mrow|<mi>/.test(text), unrendered: el.querySelectorAll(".studio-unrendered").length,
          unrendered_reason: unrenderedEl?.querySelector("small")?.textContent ?? unrenderedEl?.getAttribute("title") ?? null };
      });
      record.measurements.math = reading;
      assert(reading.math_count > 0, "no typeset equation");
      assert(/^"?Latin Modern Math/.test(reading.family) && reading.loaded, `math face not loaded: ${reading.family} ${reading.loaded}`);
      assert(!/Mono/.test(reading.body_family), "explanation uses the code face");
      assert(!reading.raw_markup, "raw LaTeX or MathML is visible");
      assert(reading.unrendered === 0 || reading.unrendered_reason, "unrendered constructs lack their reason");
      return { ...reading, screenshot: await shot("math", pane) };
    });

    await step("equation_code_linking", async () => {
      const link = page.locator(".studio-equation-link").first();
      const lineLabel = (await link.locator("span").last().innerText()).trim();
      await link.click();
      await page.locator(".studio-code-editor .cm-linked-line").first().waitFor();
      const linkedText = await page.locator(".studio-code-editor .cm-linked-line").first().innerText();
      await page.waitForFunction((el) => el.getAttribute("aria-pressed") === "true", await link.elementHandle(), { timeout: 10_000 })
        .catch(() => { throw new Error("equation not marked linked"); });
      return { equation: lineLabel, linked_python_line: linkedText.slice(0, 80) };
    });

    await step("splitter_and_views", async () => {
      const splitter = page.getByRole("separator", { name: "Resize explanation and code" });
      const before = (await box(page.locator(".studio-math-pane"))).width;
      await splitter.focus();
      for (let i = 0; i < 6; i += 1) await page.keyboard.press("ArrowLeft");
      const after = (await box(page.locator(".studio-math-pane"))).width;
      assert(after < before - 40, `keyboard resize did not narrow the explanation (${before} -> ${after})`);
      const focusShot = await shot("keyboard-focus-splitter");
      await page.keyboard.press("Home");
      await page.getByRole("button", { name: "Python", exact: true }).click();
      assert(!await page.locator(".studio-math-pane").isVisible(), "Python view still shows the explanation");
      const codeOnly = await box(page.locator(".studio-code-pane"));
      await page.getByRole("button", { name: "Explanation", exact: true }).click();
      const explanationOnly = await box(page.locator(".studio-math-pane"));
      await page.getByRole("button", { name: "Side by side", exact: true }).click();
      await splitter.dblclick();
      record.measurements.views = { split_before: before, split_after_keyboard: after, code_only: codeOnly, explanation_only: explanationOnly };
      return { before, after, code_only_width: codeOnly.width, explanation_only_width: explanationOnly.width, focus_screenshot: focusShot };
    });

    await step("workshop_controls", async () => {
      await page.getByRole("button", { name: "Show library" }).click();
      await page.locator("#studio-library").waitFor();
      const libraryShot = await shot("workshop-library");
      await page.getByRole("button", { name: "Hide library" }).click();
      await page.getByRole("button", { name: "Contract & notes" }).click();
      await page.getByRole("region", { name: "Contract, units and notes" }).waitFor();
      await page.getByRole("button", { name: "Contract & notes" }).click();
      await page.getByRole("button", { name: "Exit focus" }).click();
      const unfocused = await shot("workshop-unfocused");
      await page.getByRole("button", { name: "Focus", exact: true }).click();
      return { library_screenshot: libraryShot, unfocused_screenshot: unfocused };
    });

    await step("cfd_field", async () => {
      await page.getByRole("button", { name: "Back to Design" }).click();
      await stages.getByRole("button", { name: "CFD", exact: true }).click();
      await tree.getByRole("treeitem", { name: /^C1-CFD$/ }).click();
      await tree.getByRole("treeitem", { name: /^Results\b/ }).click();
      const slice = page.getByRole("region", { name: "CFD field slice" });
      const image = slice.getByRole("img", { name: /on the axial section/ });
      await image.waitFor({ timeout: 60_000 });
      const svg = await box(image);
      const main = await box(page.locator("#app-main"));
      record.measurements.field = { svg, main, share_width: +(svg.width / main.width).toFixed(3) };
      assert(svg.y + svg.height <= run.viewport.height, `field figure below the fold (${svg.y}+${svg.height})`);
      const text = await slice.innerText();
      assert(/2D axial/.test(text), "2D section label missing");
      assert(/Display only: the vertical axis is stretched ×/.test(text), "display stretch not declared");
      const legend = await slice.getByLabel("Colour legend").innerText();
      assert(/Pa|m2\/s2/.test(legend), `pressure legend has no units: ${legend}`);
      const fieldShot = await shot("cfd-field");
      // Pointer readout.
      await page.mouse.move(svg.x + svg.width * 0.4, svg.y + svg.height * 0.5);
      const readout = slice.locator(".design-project__field-readout");
      await page.waitForFunction((el) => /at x =/.test(el.textContent), await readout.elementHandle());
      const pointer = await readout.innerText();
      // Zoom, keyboard inspection and |U|.
      await slice.getByRole("button", { name: "Zoom in" }).click();
      await slice.getByRole("button", { name: "Zoom in" }).click();
      const zoomText = await slice.getByRole("group", { name: "Field view controls" }).innerText();
      await image.focus();
      await page.keyboard.press("ArrowRight");
      await page.keyboard.press("ArrowRight");
      const keyboard = await readout.innerText();
      const keyboardShot = await shot("cfd-field-keyboard");
      await slice.getByRole("button", { name: "|U|" }).click();
      await slice.getByRole("img", { name: /Velocity magnitude on the axial section/ }).waitFor();
      const velocityLegend = await slice.getByLabel("Colour legend").innerText();
      assert(/m\/s/.test(velocityLegend), `velocity legend has no units: ${velocityLegend}`);
      await slice.getByRole("button", { name: "Reset view" }).click();
      const compare = page.getByRole("group", { name: "Compare runs" });
      const labels = await compare.locator("label").allInnerTexts();
      assert(labels.length >= 8 && labels.every((label) => !/^[0-9a-f]{8} ·/.test(label.trim())), `run labels are not human: ${labels[0]}`);
      return { pointer_readout: pointer, keyboard_readout: keyboard, zoom: zoomText.match(/\d+%/)?.[0], legend, velocity_legend: velocityLegend, runs: labels.length,
        screenshots: [fieldShot, keyboardShot, await shot("cfd-field-velocity", slice)] };
    });

    await step("empty_and_error_states", async () => {
      // Empty: the C1-horizontal component has no CFD run (real data).
      await tree.getByRole("treeitem", { name: /^C1-horizontal$/ }).click();
      await tree.getByRole("treeitem", { name: /^Results\b/ }).click();
      const empty = page.getByRole("region", { name: "CFD field slice" });
      await empty.locator('.design-project__viewport-empty[title*="No finished CFD run"]').waitFor();
      const emptyText = await empty.innerText();
      assert(/Empty/.test(emptyText), "empty state lacks its status word");
      const emptyShot = await shot("empty-state");
      // Error: the field request of the C1-CFD component is answered with 503 inside the browser (presentation check).
      const path = "/fields";
      const entry = { path, kind: "page.route 503 on CFD field fetch", responses: [] };
      record.intercepted.push(entry);
      await page.route(`**/cfd-runs/*/fields*`, (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Field service unavailable (proof interception)" }) }));
      await tree.getByRole("treeitem", { name: /^C1-CFD$/ }).click();
      await tree.getByRole("treeitem", { name: /^Results\b/ }).click();
      const slice = page.getByRole("region", { name: "CFD field slice" });
      await slice.getByRole("alert").waitFor();
      const errorText = await slice.getByRole("alert").innerText();
      assert(/Failed/.test(errorText), "error state lacks its status word");
      const errorShot = await shot("error-state");
      await page.unroute(`**/cfd-runs/*/fields*`);
      return { empty: emptyText.slice(0, 200), error: errorText, screenshots: [emptyShot, errorShot] };
    });

    await step("status_language", async () => {
      await tree.getByRole("treeitem", { name: /^Geometry\b/ }).click();
      const badges = await tree.locator(".design-tree__meta").evaluateAll((els) => els.map((el) => ({ text: el.textContent, tone: el.dataset.tone,
        glyph: getComputedStyle(el, "::before").content })));
      assert(badges.every((badge) => badge.tone && badge.glyph && badge.glyph !== "none"), "a node badge has no tone glyph");
      const inspector = page.getByRole("complementary", { name: "Inspector" });
      const visible = await page.locator("#app-main").evaluate((el) => {
        // Visible main-view text only (closed details are not rendered): long hex digests or UUIDs must not appear.
        return el.innerText;
      });
      const leaks = visible.match(/\b[0-9a-f]{12,}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/g) ?? [];
      record.measurements.status = { badges, digest_or_uuid_in_main_view: leaks };
      assert(!leaks.length, `digests/UUIDs visible in the main view: ${leaks.slice(0, 3).join(", ")}`);
      return { badges, screenshot: await shot("status-geometry"), inspector_excerpt: (await inspector.innerText().catch(() => "")).slice(0, 200) };
    });

    await step("charts", async () => {
      await stages.getByRole("button", { name: "Optics", exact: true }).click();
      const prediction = page.getByRole("region", { name: "Dynamic Studio prediction" });
      await prediction.getByLabel("Important plant-level KPIs").getByText(/12\.92 kg/).waitFor({ timeout: 60_000 });
      await prediction.getByText("Runs and results", { exact: true }).first().click();
      await prediction.getByRole("button", { name: "View results" }).first().click();
      const chart = page.locator(".dyn-chart").first();
      await chart.locator("canvas").first().waitFor({ timeout: 60_000 });
      const collapsed = await box(chart);
      await chart.getByRole("button", { name: "Expand chart" }).click();
      await page.waitForTimeout(600);
      const expanded = await box(chart);
      await chart.scrollIntoViewIfNeeded();
      const caption = await chart.locator("figcaption").innerText();
      record.measurements.chart = { collapsed, expanded };
      assert(expanded.height > collapsed.height + 100, "Expand chart did not enlarge the chart");
      const shotFile = await shot("chart-expanded");
      await chart.getByRole("button", { name: "Collapse chart" }).click();
      return { caption, collapsed, expanded, screenshot: shotFile };
    });

    await context.close();
  }
  await browser.close();
} finally {
  if (server) {
    server.kill("SIGTERM");
    await delay(1000);
    if (server.exitCode === null) server.kill("SIGKILL");
    await rm(dataRoot, { recursive: true, force: true }).catch(() => {});
  }
  summary.finished_at = new Date().toISOString();
  const runs = Object.values(summary.runs);
  summary.passed = runs.length === RUNS.length && runs.every((run) => run.document_loads === 1 && run.steps.length && run.steps.every((s) => s.status === "pass")
    && run.console_errors.length === run.expected_refusals.length + run.intercepted.reduce((n, item) => n + item.responses.length, 0) && !run.page_errors.length && !run.foreign_requests.length && !run.write_requests.length && !run.error_responses.length);
  await writeFile(join(evidence, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  console.log(JSON.stringify({ evidence, passed: summary.passed,
    steps: Object.fromEntries(Object.entries(summary.runs).map(([k, r]) => [k, r.steps.map((s) => `${s.status} ${s.step}${s.error ? " " + s.error.slice(0, 300) : ""}`)])) }, null, 2));
  process.exitCode = summary.passed ? 0 : 1;
}
