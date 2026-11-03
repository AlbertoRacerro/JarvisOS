#!/usr/bin/env node
// Spec 229 acceptance 7: operator discovery walk. A fresh browser profile (no localStorage, sessionStorage or URL
// pre-seeding) opens "/" and reaches the C1 Example's Plant -> CAD -> CFD runs and field -> reduced model and its
// Process pin -> Optics inputs -> C1 prediction using only visible controls addressed by role and accessible name,
// then follows one visible path to every row of the capability-parity inventory. Semantic targets are used only in
// assertions. No CFD or simulation is run.
// The SOURCE data root (one in which the 222 importer imported the C1 Example) is snapshotted into a fresh temporary
// directory and the backend runs against the copy; the source is opened read-only.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/design_discovery_walk.mjs <evidence-dir> <source-data-root>
// Live check (acceptance 8): JARVISOS_PROOF_LIVE_BASE=http://127.0.0.1:8000 node ... <evidence-dir> walks the already running app
// instead; no backend is started and no data root is copied. The walk only reads (GETs and the read-only preview POSTs).
// Env: JARVISOS_PROOF_PORT (8229), PLAYWRIGHT_MODULE.
import { createHash } from "node:crypto";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { DESIGN_STAGE_POSTCONDITIONS, DESIGN_TARGETS } from "../frontend/src/app/designTargets.ts";

if (process.argv.includes("--help")) { console.log('node e2e/design_discovery_walk.mjs <evidence-dir> <source-data-root>  (needs built frontend, Playwright)'); process.exit(0); }
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8229);
const live = process.env.JARVISOS_PROOF_LIVE_BASE?.replace(/\/+$/, "") || null;
const base = live ?? `http://127.0.0.1:${port}`;
const evidence = resolve(process.argv[2] ?? await mkdtemp(join(tmpdir(), "jarvisos-229-walk-")));
const sourceRoot = resolve(process.argv[3] ?? "");
const assert = (condition, message) => { if (!condition) throw new Error(message); };
assert(live || process.argv[3], "source data root required");
await mkdir(evidence, { recursive: true });

// Snapshot: files are copied, the SQLite database through the backup API so a live source stays consistent.
let dataRoot = null, snapshot = null, server = null;
if (!live) {
dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-229-walk-data-"));
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
  { name: "light-wide", colorScheme: "light", viewport: { width: 1600, height: 1000 }, entry: "picker", parity: true },
  { name: "dark-narrow", colorScheme: "dark", viewport: { width: 1024, height: 768 }, entry: "empty-project-offer", parity: false }
];
// POST routes that only compute a preview and store nothing (Jarvis surface brief, Studio binding preview).
const PREVIEW_POSTS = [/^POST \/workspaces\/[^/]+\/actions\/brief$/, /^POST \/workspaces\/[^/]+\/model-implementations\/[^/]+\/binding-preview$/];
const summary = { spec: "229", acceptance: live ? "8 live check on the running app" : "7 operator discovery walk", head, worktree_clean: !dirty, browser: null,
  started_at: new Date().toISOString(), ...(live ? { live_base: base, served: await fetch(`${base}/health`).then((r) => r.json()).catch(() => null) } : {
    source_data_root: sourceRoot, working_copy: "fresh temporary snapshot (files copied, SQLite via backup API); source opened read-only",
    artifact_paths_rewritten_in_copy: Number(snapshot.stdout.trim()) }), runs: {} };

try {
  for (let i = 0; ; i++) {
    assert(!server || server.exitCode === null, "backend exited; see backend.log");
    try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* starting */ }
    assert(i < 120, "backend did not become healthy");
    await delay(500);
  }
  const workspaces = await (await fetch(`${base}/workspaces`)).json();
  const example = workspaces.find((item) => item.slug === "example-c1-smooth-tube");
  assert(example, "the C1 Example is not in the source data root");
  // A project without Design components, to show the empty-state offer (acceptance 3). Read-only lookups.
  let emptyProject = null;
  for (const item of workspaces.filter((candidate) => candidate.id !== example.id)) {
    const versions = await fetch(`${base}/workspaces/${item.id}/design-versions`);
    if (!versions.ok) continue;
    const { active_version_id: active } = await versions.json();
    const components = await fetch(`${base}/workspaces/${item.id}/design-versions/${active}/components`);
    if (components.ok && !(await components.json()).length) { emptyProject = item; break; }
  }
  assert(emptyProject, "no project without Design components to exercise the Open Example · C1 offer");
  summary.workspaces = { count: workspaces.length, first_listed: workspaces[0]?.slug, example: { id: example.id, name: example.name },
    empty_project_for_offer: emptyProject.name };

  const browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  for (const run of RUNS) {
    const record = { colorScheme: run.colorScheme, viewport: run.viewport, steps: [], screenshots: [], console_errors: [], page_errors: [],
      navigations: [], write_requests: [], preview_posts: [], error_responses: [], foreign_requests: [], targets_seen: {}, postconditions: [] };
    summary.runs[run.name] = record;
    const context = await browser.newContext({ viewport: run.viewport, colorScheme: run.colorScheme });
    // Records only: proves the profile starts with empty browser storage before the app's own first write.
    await context.addInitScript(() => { try { window.__proofInitialStorage = { local: localStorage.length, session: sessionStorage.length }; } catch { /* ignore */ } });
    // Records only: the read-only Design state the page publishes for later guidance (acceptance 6).
    await context.addInitScript(() => { window.addEventListener("jarvis:design-surface", (event) => { window.__proofDesignSurface = event.detail; }); });
    const page = await context.newPage();
    page.setDefaultTimeout(30_000);
    page.on("console", (m) => { if (m.type() === "error") record.console_errors.push(m.text()); });
    page.on("pageerror", (e) => record.page_errors.push(String(e)));
    page.on("framenavigated", (frame) => { if (frame === page.mainFrame()) record.navigations.push(frame.url().replace(base, "")); });
    // Full document loads: only the initial "/" is loaded; every later route change is a click handled in the page.
    record.document_loads = 0;
    page.on("load", () => { record.document_loads += 1; });
    page.on("request", (r) => {
      const url = r.url();
      if (!url.startsWith(base) && !url.startsWith("data:") && !url.startsWith("blob:")) record.foreign_requests.push(url);
      if (["GET", "HEAD", "OPTIONS"].includes(r.method())) return;
      const call = `${r.method()} ${url.replace(base, "").split("?")[0]}`;
      (PREVIEW_POSTS.some((pattern) => pattern.test(call)) ? record.preview_posts : record.write_requests).push(call);
    });
    page.on("response", (response) => { if (response.status() >= 400) record.error_responses.push(`${response.status()} ${response.request().method()} ${response.url().replace(base, "")}`); });
    const shot = async (name) => {
      const file = join(evidence, `${run.name}-${String(record.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
      await page.screenshot({ path: file, fullPage: false });
      const entry = { file: file.slice(evidence.length + 1), sha256: createHash("sha256").update(await readFile(file)).digest("hex") };
      record.screenshots.push(entry);
      return entry.file;
    };
    // Every target rendered now: exactly one element, exposed with its registered role and accessible name.
    const checkTargets = async (where) => {
      const rendered = await page.locator("[data-jarvis-target]").evaluateAll((els) => els.map((el) => el.getAttribute("data-jarvis-target")));
      const counts = rendered.reduce((acc, id) => ({ ...acc, [id]: (acc[id] ?? 0) + 1 }), {});
      for (const [id, count] of Object.entries(counts)) {
        const target = DESIGN_TARGETS[id];
        assert(target, `${where}: unregistered target ${id}`);
        assert(count === 1, `${where}: target ${id} rendered ${count} times`);
        const element = page.locator(`[data-jarvis-target="${id}"]`);
        if (!await element.isVisible()) continue;
        const name = target.name.startsWith("^") ? new RegExp(target.name) : target.name;
        const owners = await page.getByRole(target.role, { name, exact: typeof name === "string" }).evaluateAll((els) => els.map((el) => el.getAttribute("data-jarvis-target")));
        assert(owners.includes(id), `${where}: ${id} is not exposed as ${target.role} "${target.name}"`);
        record.targets_seen[id] = (record.targets_seen[id] ?? 0) + 1;
      }
    };
    const holds = async (stage, only = null) => {
      for (const condition of DESIGN_STAGE_POSTCONDITIONS[stage].filter((item) => !only || only.includes(item.target))) {
        await page.locator(`[data-jarvis-target="${condition.target}"]`).waitFor({ state: "visible" });
        record.postconditions.push({ stage, target: condition.target, holds: condition.holds });
      }
    };
    const step = async (name, work) => {
      try {
        const detail = (await work()) ?? {};
        await checkTargets(name);
        record.steps.push({ step: name, status: "pass", screenshot: await shot(name), ...detail });
      } catch (error) {
        record.steps.push({ step: name, status: "fail", error: String(error).slice(0, 800), screenshot: await shot(`FAILED-${name}`).catch(() => null) });
      }
    };
    const stages = page.getByRole("navigation", { name: "Design stages" });
    const stageButton = (name) => stages.getByRole("button", { name, exact: true });
    const tree = page.getByRole("tree", { name: "Component model tree" });
    const node = (name) => tree.getByRole("treeitem", { name: new RegExp(`^${name}\\b`) });
    const nodeBadge = async (name) => (await node(name).locator(".design-tree__meta").innerText()).trim();

    await step("start_at_root", async () => {
      await page.goto(`${base}/`, { waitUntil: "networkidle" });
      const initial = await page.evaluate(() => window.__proofInitialStorage);
      assert(initial && initial.local === 0 && initial.session === 0, `profile was not clean: ${JSON.stringify(initial)}`);
      assert(new URL(page.url()).pathname === "/design/project", `"/" did not open New Design: ${page.url()}`);
      assert(await page.evaluate(() => document.documentElement.dataset.theme) === run.colorScheme, "system appearance not followed");
      const picker = page.getByRole("combobox", { name: "Project", exact: true });
      await picker.waitFor();
      const selected = await picker.evaluate((el) => el.options[el.selectedIndex]?.text);
      return { initial_storage: initial, landing: "/design/project", initially_selected_project: selected,
        example_offer_visible: await page.getByRole("button", { name: "Open Example · C1" }).isVisible() };
    });

    await step("open_example", async () => {
      const offer = page.getByRole("button", { name: "Open Example · C1" });
      const picker = page.getByRole("combobox", { name: "Project", exact: true });
      let via;
      if (run.entry === "empty-project-offer") {
        // An operator on a project with no Design content: the empty state offers the Example next to the create path.
        const label = (await picker.locator("option").allInnerTexts()).find((text) => text === emptyProject.name);
        assert(label, `the empty project ${emptyProject.name} is not in the Project picker`);
        await picker.selectOption({ label });
        await page.getByRole("heading", { level: 1, name: emptyProject.name }).waitFor();
        await offer.waitFor();
        await checkTargets("empty_project");
        await offer.click(); via = `Project picker: ${label}, then Open Example · C1`;
      } else {
        const label = (await picker.locator("option").allInnerTexts()).find((text) => /^Example · C1 smooth tube.*\(Example\)$/.test(text));
        assert(label, "the C1 Example is not offered in the Project picker");
        await picker.selectOption({ label }); via = `Project picker: ${label}`;
      }
      await page.getByRole("heading", { level: 1, name: /Example · C1 smooth tube/ }).waitFor();
      await page.getByRole("heading", { level: 1 }).getByText("Example", { exact: true }).waitFor();
      return { via };
    });

    await step("plant", async () => {
      await stageButton("Plant").click();
      await holds("Plant");
      await page.getByTestId("node-C1-CFD").waitFor();
      await page.getByTestId("node-C1-horizontal").waitFor();
      return { components: await tree.getByRole("treeitem").allInnerTexts() };
    });

    await step("cad_tube", async () => {
      await tree.getByRole("treeitem", { name: /^C1-CFD$/ }).click();
      await stageButton("CAD").click();
      await holds("CAD");
      await page.getByRole("region", { name: "Component viewport" }).locator("canvas").waitFor({ timeout: 60_000 });
      const inspector = await page.getByRole("complementary", { name: "Inspector" }).innerText();
      assert(/A validated BlueCad candidate is attached/.test(inspector), "geometry attachment not shown");
      assert(await nodeBadge("Geometry") === "attached", "Geometry badge disagrees with the panel");
      return { geometry_badge: await nodeBadge("Geometry") };
    });

    await step("cfd_runs", async () => {
      await stageButton("CFD").click();
      await page.getByRole("region", { name: "CFD runs", exact: true }).getByText("Loading CFD runs…").waitFor({ state: "detached" });
      await holds("CFD", ["design.panel.cfd-studies", "design.panel.cfd-runs"]);
      const list = page.getByRole("list", { name: "CFD run list" });
      const items = await list.getByRole("listitem").allInnerTexts();
      const finished = items.filter((text) => /finished/.test(text)).length;
      assert(finished >= 8, `expected the eight imported finished runs, saw ${finished}`);
      const badge = await nodeBadge("Studies");
      assert(badge.endsWith(`${items.length} runs`), `Studies badge "${badge}" disagrees with ${items.length} listed runs`);
      await list.getByRole("button").first().click();
      await page.getByRole("region", { name: "CFD run console" }).waitFor();
      return { listed_runs: items.length, finished, studies_badge: badge };
    });

    await step("cfd_field", async () => {
      await node("Results").click();
      const slice = page.getByRole("region", { name: "CFD field slice" });
      const image = slice.getByRole("img", { name: /on the axial section/ });
      await image.waitFor({ timeout: 60_000 });
      await holds("CFD", ["design.panel.cfd-field"]);
      const box = await image.locator("polygon").evaluateAll((els) => {
        const rects = els.map((el) => el.getBoundingClientRect());
        return { height: Math.max(...rects.map((r) => r.bottom)) - Math.min(...rects.map((r) => r.top)), width: Math.max(...rects.map((r) => r.right)) - Math.min(...rects.map((r) => r.left)) };
      });
      assert(box.height >= 60, `field slice drawn ${box.height.toFixed(1)} px tall; not legible`);
      const text = await slice.innerText();
      assert(/Display only: the vertical axis is stretched ×/.test(text), "display stretch not declared");
      assert(/Colour legend|m2\/s2|Pa/.test(text) || await slice.getByLabel("Colour legend").isVisible(), "legend missing");
      await slice.getByRole("button", { name: "|U|" }).click();
      await slice.getByRole("img", { name: /Velocity magnitude on the axial section/ }).waitFor();
      const legend = await slice.getByLabel("Colour legend").innerText();
      assert(/m\/s/.test(legend), `velocity legend has no units: ${legend}`);
      const results = await nodeBadge("Results");
      assert(/^\d+ finished$/.test(results), `Results badge "${results}" disagrees with the shown field`);
      return { drawn_px: box, legend, results_badge: results, stretch_note: text.match(/Display only:[^\n]*/)?.[0] };
    });

    await step("reduced_model_and_pin", async () => {
      await stageButton("Reduced model").click();
      await holds("Reduced model");
      const models = page.getByRole("region", { name: "CFD reduced models" });
      await models.getByText("Current", { exact: true }).first().waitFor();
      await models.getByText("Pinned on Process unit").first().waitFor();
      const text = await models.innerText();
      assert(/scientifically unqualified/.test(text), "fidelity label missing");
      const badge = await nodeBadge("Reduced model");
      assert(badge === "current · pinned", `Reduced model badge "${badge}" disagrees with the panel`);
      return { reduced_badge: badge, panel: text.slice(0, 300) };
    });

    await step("optics_inputs_and_prediction", async () => {
      await stageButton("Optics").click();
      await holds("Optics");
      const inputs = page.getByRole("region", { name: "Optics inputs" });
      await inputs.getByText(/C1-horizontal/).first().waitFor();
      const text = await inputs.innerText();
      assert(/not set/.test(text), "unset optics v2 inputs are not shown as not set");
      const prediction = page.getByRole("region", { name: "Dynamic Studio prediction" });
      const chosen = await prediction.getByRole("combobox", { name: "Draft with the prediction" }).evaluate((el) => el.options[el.selectedIndex]?.text);
      assert(/C1 horizontal central declared-assumption run \(215\)/.test(chosen), `prediction draft not chosen by default: ${chosen}`);
      await prediction.scrollIntoViewIfNeeded();
      await prediction.getByLabel("Important plant-level KPIs").getByText("Cumulative harvest").waitFor().catch(async (error) => {
        throw new Error(`${error.message.split(/\r?\n/)[0]} | region text: ${(await prediction.innerText()).slice(0, 1500)}`);
      });
      // The cards say "loading the run's results…" until the stored run is read; then the values must appear.
      await prediction.getByLabel("Important plant-level KPIs").getByText(/12\.92 kg/).waitFor({ timeout: 60_000 });
      const shown = await prediction.innerText();
      assert(/12\.92 kg/.test(shown), "cumulative harvest 12.92 kg not shown");
      assert(/0\.899796 kg\/m3\/d/.test(shown), "volumetric productivity not shown");
      assert(/unqualified/i.test(shown), "unqualified label missing");
      return { prediction_draft: chosen, optics_excerpt: text.slice(0, 500) };
    });

    await step("design_state_published", async () => {
      const state = await page.evaluate(() => window.__proofDesignSurface ?? null);
      assert(state && state.route_id === "design-project" && state.workspace_id === example.id && state.example === true, `Design state missing or wrong: ${JSON.stringify(state)}`);
      assert(state.stage === "Optics" && state.component_name === "C1-CFD" && state.version_label, "Design state does not reflect the screen");
      assert(state.panels.cfd_runs === "available" && state.panels.cfd_fields === "available" && state.panels.reduced_models === "available" && state.panels.geometry === "available",
        `panel availability disagrees with the walk: ${JSON.stringify(state.panels)}`);
      return { design_state: state };
    });

    if (run.parity) {
      const tools = page.getByRole("navigation", { name: "Design tools" });
      const backToDesign = async () => {
        await page.getByRole("link", { name: "Design", exact: true }).click();
        await page.getByRole("combobox", { name: "Project", exact: true }).waitFor();
      };
      const viaTools = async (label) => {
        if (!await tools.getByRole("link", { name: label, exact: true }).isVisible()) await tools.getByText("More Design tools").click();
        await tools.getByRole("link", { name: label, exact: true }).click();
      };
      const parity = [
        // The live solver console opens from the Plant toolbar of a draft that has a stored solve; the C1 Plant has none,
        // so this row uses the first listed project, chosen in the picker. It only opens the stored console.
        ["plant_solver_console", async () => {
          const picker = page.getByRole("combobox", { name: "Project", exact: true });
          await picker.selectOption({ label: workspaces[0].name });
          await stageButton("Plant").click();
          await page.getByRole("region", { name: "Plant" }).getByRole("button", { name: /Solver console/ }).click();
          await page.locator("#process-solver-console").waitFor();
          await picker.selectOption({ label: (await picker.locator("option").allInnerTexts()).find((text) => /^Example · C1 smooth tube/.test(text)) });
          await page.getByRole("heading", { level: 1, name: /Example · C1 smooth tube/ }).waitFor();
          return { path: `Design › Project ${workspaces[0].name} › Plant › Solver console`, clicks: 3 };
        }],
        // Dynamic Studio left the bottom of the Process editor for its own Design stage; the editor keeps a link to it.
        ["process_editor_dynamic_link", async () => { await viaTools("Process editor"); await page.getByRole("heading", { name: "Process workspace" }).waitFor();
          await page.getByRole("link", { name: "Design › Dynamic", exact: true }).waitFor(); return { path: "Design tools › Process editor", clicks: 2 }; }],
        ["process_editor_native_dwsim", async () => { await page.getByText("Advanced · Native DWSIM cases").click();
          await page.getByRole("tab", { name: "Native DWSIM cases (advanced)" }).click(); await page.getByRole("combobox", { name: "DWSIM case" }).or(page.getByText(/native DWSIM case/i).first()).first().waitFor();
          return { path: "Design tools › Process editor › Advanced › Native DWSIM cases", clicks: 4 }; }],
        ["design_dynamic_stage", async () => { await backToDesign(); await stageButton("Dynamic").click(); await holds("Dynamic");
          return { path: "Design › Dynamic", clicks: 1 }; }],
        ["environment_profiles", async () => { await backToDesign(); await stageButton("Plant").click(); await page.getByRole("button", { name: "Environment…" }).click();
          await page.getByRole("dialog", { name: "Environment profiles" }).waitFor(); await page.getByRole("button", { name: "Close Environment" }).click();
          return { path: "Design › Plant › Environment…", clicks: 2 }; }],
        ["process_editor_from_plant", async () => { await page.getByRole("link", { name: "Open full Process editor" }).click(); await page.getByRole("heading", { name: "Process workspace" }).waitFor();
          return { path: "Design › Plant › Open full Process editor", clicks: 2 }; }],
        ["engineering_studies", async () => { await viaTools("Engineering studies"); await page.getByRole("heading", { name: "Evaluator availability" }).waitFor(); return { path: "Design tools › Engineering studies", clicks: 2 }; }],
        ["bluecad_workbench", async () => { await viaTools("BlueCad workbench"); await page.getByRole("heading", { name: "Model workbench" }).waitFor(); return { path: "Design tools › BlueCad workbench", clicks: 2 }; }],
        ["bluecad_from_cad", async () => { await backToDesign(); await tree.getByRole("treeitem", { name: /^C1-CFD$/ }).click(); await stageButton("CAD").click();
          await page.getByRole("link", { name: "Open BlueCad workbench" }).click(); await page.getByRole("heading", { name: "Model workbench" }).waitFor();
          return { path: "Design › C1-CFD › CAD › Open BlueCad workbench", clicks: 4 }; }],
        ["studio", async () => { await viaTools("Studio"); await page.getByRole("heading", { name: "Library" }).first().waitFor(); return { path: "Design tools › Studio", clicks: 2 }; }],
        ["studies_from_cfd", async () => { await backToDesign(); await tree.getByRole("treeitem", { name: /^C1-CFD$/ }).click(); await stageButton("CFD").click();
          await page.getByRole("link", { name: "Open engineering Studies" }).click(); await page.getByRole("heading", { name: "Evaluator availability" }).waitFor();
          return { path: "Design › C1-CFD › CFD › Open engineering Studies", clicks: 4 }; }],
        ["studio_from_library", async () => { await backToDesign(); await page.getByText("Library", { exact: true }).click(); await checkTargets("library_open"); await page.getByRole("link", { name: "Open Studio" }).click();
          await page.getByRole("heading", { name: "Library" }).first().waitFor(); return { path: "Design › Library › Open Studio", clicks: 2 }; }],
        ["engineering_data", async () => { await viaTools("Engineering data"); await page.getByRole("heading", { name: "Engineering Data", level: 1 }).waitFor(); return { path: "Design tools › Engineering data", clicks: 2 }; }],
        ["runs", async () => { await viaTools("Runs"); await page.getByRole("heading", { name: "Runs", level: 1 }).waitFor(); return { path: "Design tools › Runs", clicks: 2 }; }],
        ["back_to_design", async () => { await backToDesign(); assert(new URL(page.url()).pathname === "/design/project", "Design rail item did not return to New Design"); return { path: "Design", clicks: 1 }; }]
      ];
      for (const [name, work] of parity) await step(`parity_${name}`, work);
    }

    record.initial_storage_checked = true;
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
  const unseen = Object.keys(DESIGN_TARGETS).filter((id) => !runs.some((run) => run.targets_seen[id]));
  summary.targets_never_rendered = unseen;
  summary.passed = runs.length === RUNS.length && runs.every((run) => run.document_loads === 1 && run.steps.every((s) => s.status === "pass") && !run.console_errors.length
    && !run.page_errors.length && !run.foreign_requests.length && !run.write_requests.length && !run.error_responses.length);
  await writeFile(join(evidence, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  console.log(JSON.stringify({ evidence, passed: summary.passed, targets_never_rendered: unseen,
    steps: Object.fromEntries(Object.entries(summary.runs).map(([k, r]) => [k, r.steps.map((s) => `${s.status} ${s.step}${s.error ? " " + s.error.slice(0, 220) : ""}`)])) }, null, 2));
  process.exitCode = summary.passed ? 0 : 1;
}
