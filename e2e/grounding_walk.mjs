#!/usr/bin/env node
// REF-068 research tranches 2-7, not product guidance or a 224 readiness gate: the grounding candidates of
// grounding_bakeoff.mjs run against the REAL Jarvis Design screens. The C1 Example source data root is snapshotted into a
// fresh temporary directory exactly as the spec 230 walk does (files copied, SQLite via the backup API, source opened
// read-only) and a private backend serves the built frontend on it. No solver, model, provider or action runs: the
// harness only navigates through visible controls to set up each case, then asks every candidate for a pointer.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/grounding_walk.mjs <evidence-dir> <source-data-root>
// Owner state is the page's published Design surface (workspaceActionSurface.ts), captured by an init script. Each case
// records it at observation and again just before resolution; the expected decision is the case oracle, and a case
// whose setup did not produce the owner state it claims is reported as fixture_invalid instead of being scored.
// Env: JARVISOS_PROOF_PORT (8224), PLAYWRIGHT_MODULE.
import { createHash } from "node:crypto";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { DESIGN_TARGETS } from "../frontend/src/app/designTargets.ts";

if (process.argv.includes("--help")) { console.log('node e2e/grounding_walk.mjs <evidence-dir> <source-data-root>  (needs built frontend, Playwright)'); process.exit(0); }
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8224);
const base = `http://127.0.0.1:${port}`;
const assert = (condition, message) => { if (!condition) throw new Error(message); };
assert(process.argv[2] && process.argv[3], "usage: grounding_real_app.mjs <evidence-dir> <source-data-root>");
const evidence = resolve(process.argv[2]);
const sourceRoot = resolve(process.argv[3]);
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-068-grounding-data-"));
await cp(sourceRoot, dataRoot, { recursive: true, filter: (path) => !/jarvisos\.db(-wal|-shm|-journal)?$/.test(path) });
const snapshot = spawnSync(python, ["-I", "-c", [
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
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)],
  { cwd: backend, env, stdio: ["ignore", openSync(join(evidence, "backend.log"), "w"), openSync(join(evidence, "backend.log"), "a")] });
const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const dirty = spawnSync("git", ["status", "--porcelain"], { cwd: root, encoding: "utf8" }).stdout.trim();

// Owner fields that bind a pointer to the state it was computed in. A pointer observed in one of these states and
// resolved in another is stale even when an identically named control is still on screen.
const BINDING = ["route_id", "workspace_id", "version_id", "stage", "component_id", "node"];
// Read-only previews the Design page issues on load (the same allow-list as the spec 230 walk); anything else is a write.
const PREVIEW_POSTS = [/^POST \/workspaces\/[^/]+\/actions\/brief$/, /^POST \/workspaces\/[^/]+\/model-implementations\/[^/]+\/binding-preview$/];
// Development filter for a subset of cases; a scored run must not set it.
const ONLY = process.env.JARVISOS_068_ONLY ? new RegExp(`^(${process.env.JARVISOS_068_ONLY})$`) : null;
const sameBinding = (a, b) => !!a && !!b && BINDING.every((key) => a[key] === b[key]);

// Setup helpers act only through visible controls, as an operator would.
const stages = (page) => page.getByRole("navigation", { name: "Design stages" });
const tree = (page) => page.getByRole("tree", { name: "Component model tree" });
const openExample = async (page) => {
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const picker = page.getByRole("combobox", { name: "Project version", exact: true });
  const option = picker.locator('option[value^="case:"]').filter({ hasText: /C1 · smooth tube/ });
  await option.waitFor({ state: "attached" });
  await picker.selectOption(await option.getAttribute("value"));
  await page.getByRole("heading", { level: 1, name: /case study C1 smooth tube/ }).waitFor();
};
const otherProject = async (page) => {
  await page.getByRole("button", { name: /^Back to / }).click();
  await page.waitForFunction(() => window.__designSurface?.example === false);
};
const stage = async (page, name) => { await stages(page).getByRole("button", { name, exact: true }).click(); await page.waitForTimeout(300); };
const component = async (page, name) => { await tree(page).getByRole("treeitem", { name: new RegExp(`^${name}$`) }).click(); await page.waitForTimeout(300); };
const node = async (page, name) => { await tree(page).getByRole("treeitem", { name: new RegExp(`^${name}\\b`) }).click(); await page.waitForTimeout(300); };
const settle = (page) => page.waitForFunction(() => window.__designSurface && !Object.values(window.__designSurface.panels).includes("loading"), null, { timeout: 60_000 });
const canvas = (page) => page.getByRole("img", { name: "Process flowsheet draft" });
const canvasUnits = (page) => canvas(page).locator('g[role="button"]');
const plant = async (page) => { await openExample(page); await stage(page, "Plant"); await canvasUnits(page).first().waitFor({ timeout: 60_000 }); };
// Keyboard focus is how an operator selects a canvas unit without dragging it; the editor publishes the selection.
const selectUnit = async (page, index) => { await canvasUnits(page).nth(index).focus(); await page.waitForTimeout(300); };
const unitLabels = (page) => canvasUnits(page).evaluateAll((nodes) => nodes.map((n) => n.getAttribute("aria-label")));
const unitType = (label) => label.split(" ").slice(0, -1).join(" ");
// Tranche 4: hold a GET until the case has been resolved, so the screen is observed and resolved in its loading state.
const hold = async (page, pattern) => { await page.route(pattern, (route) => route.request().method() === "GET" ? new Promise(() => {}) : route.fallback()); };
// Fixture writes go to the private backend on the temporary snapshot only, never through the browser, and are recorded.
const fixtureWrites = [];
const api = async (method, path, body) => {
  const response = await fetch(`${base}${path}`, { method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
  if (method !== "GET") fixtureWrites.push({ call: `${method} ${path}`, status: response.status });
  if (!response.ok) throw new Error(`${method} ${path} -> ${response.status} ${await response.text().catch(() => "")}`);
  return response.json();
};
const EMPTY_PROJECT = "REF-068 empty project";
// Tranche 5 seed: two projects with the same name (06) and one project whose new synthetic components the snapshot has
// never seen (18, 37-40): two with the same name, and two distinct ones.
const TWIN_PROJECT = "REF-068 twin";
const UNSEEN_PROJECT = "REF-068 unseen";
const TWIN_PART = "REF-068 twin part", INLET = "REF-068 inlet manifold", OUTLET = "REF-068 outlet manifold";
const openProject = async (page, name) => {
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const picker = page.getByRole("combobox", { name: "Project", exact: true });
  const label = (await picker.locator("option").allInnerTexts()).find((text) => text.startsWith(name));
  assert(label, `${name} not offered in the Project picker`);
  await picker.selectOption({ label });
  await page.getByRole("heading", { level: 1, name: new RegExp(`^${name}`) }).waitFor();
};
// The workspace whose Process drafts include the observed draft (the Example's Plant draft may live in its data workspace).
const draftOwner = async (draftId) => {
  for (const ws of await api("GET", "/workspaces")) {
    const drafts = await fetch(`${base}/workspaces/${ws.id}/process/drafts`).then((r) => r.ok ? r.json() : []);
    if (drafts.some((d) => d.draft_id === draftId)) return ws.id;
  }
  throw new Error(`no workspace owns draft ${draftId}`);
};
// Records a same-draft revision through the draft owner, then waits for the editor's 3 s refresh.
const revise = async (page, pObserved, ops) => {
  const draftId = pObserved.draft_id;
  const ws = await draftOwner(draftId);
  const path = `/workspaces/${ws}/process/drafts/${draftId}`;
  const draft = await api("GET", path);
  await api("POST", `${path}/patch`, { expected_revision: draft.revision, ops: ops(draft) });
  await page.waitForTimeout(4500);
};
// Tranche 6 (25-28): Memory/Literature. Two projects, seeded through the literature owner on the snapshot: A holds one
// unique source and two sources with the same title; B holds a source with A's unique title.
const LIT_A = "REF-068 literature A", LIT_B = "REF-068 literature B";
const MONOD = "REF-068 Monod kinetics review", TWIN_REVIEW = "REF-068 twin photobioreactor review";
const seeded = {};
const literature = async (page, name) => {
  await page.goto(`${base}/memory/literature`, { waitUntil: "networkidle" });
  await chooseLiterature(page, name);
};
const chooseLiterature = async (page, name) => {
  const picker = page.getByRole("combobox", { name: "Project", exact: true });
  await picker.selectOption({ label: name });
  await page.waitForFunction((id) => document.querySelector('select#shell-project-picker')?.value === id
    && document.querySelectorAll(`details[data-source-id]`).length > 0 && !document.body.innerText.includes("Loading literature…"), seeded[name]);
};
const openSource = async (page, id) => { await page.locator(`details[data-source-id="${id}"] > summary`).click(); await page.waitForTimeout(300); };
const fieldReady = (page) =>page.getByRole("region", { name: "CFD field slice" }).getByRole("img", { name: /on the axial section/ }).waitFor({ timeout: 60_000 });
// Tranche 7 (33-36): BlueCad 3D on /design/bluecad. The viewer canvas carries one accessible name for the whole scene
// and its meshes carry only glTF mesh names (mesh_N[_k]). A part's engineering identity is the manifest-resolved part id that the
// workbench publishes to the shell selection only after resolving the GLB semantic key against the current manifest.
// The page publishes no surface event, so the observation is read from the DOM: BlueCad workspace, candidate id (the
// header's "Candidate details"), the highlighted mesh in the project tree and the shell's selected-part panel.
const HOME_WS = process.env.JARVISOS_PROOF_HOME_WORKSPACE ?? "main"; // id of the project workspace holding the twin-tube candidates
const EXAMPLE_WS = "c2d49788-1f41-44a9-9d3d-35ba6a972494";
const SERPENTINE = "64edeeb4-38ce-4df5-a9b4-31da98e8e5fc";
const TWIN_TUBES = ["47e98a52-176a-4cfc-97e0-dbef31388a95", "776a819e-62e8-4021-b40b-cff6429eced5"];
const bluecadSurface = (page) => page.evaluate(() => {
  if (!location.pathname.startsWith("/design/bluecad")) return null;
  const dd = [...document.querySelectorAll("dt")].find((d) => d.textContent === "Candidate ID")?.nextElementSibling;
  const highlighted = [...document.querySelectorAll(".bluecad-tree__row")].find((r) => r.querySelector(".bluecad-tree__meta")?.textContent === "highlighted");
  const part = document.querySelector(".shell-properties__selection");
  const activeCase = document.querySelector('.shell-project[aria-label="Active project"] .shell-project__name')?.textContent ?? null;
  return { route: location.pathname, workspace_id: document.querySelector('select#shell-project-picker')?.value ?? activeCase,
    candidate_id: dd?.textContent ?? null, highlighted_mesh: highlighted?.querySelector(".bluecad-tree__label")?.textContent ?? null,
    part_id: part?.querySelector("strong")?.textContent ?? null, part_kind: part?.querySelector("p")?.textContent ?? null,
    part_visible: !!part && part.getClientRects().length > 0,
    viewer_ready: !!document.querySelector('canvas[aria-label^="Interactive 3D preview"]') };
});
const bluecadOpen = async (page, workspaceId, candidateId) => {
  if (workspaceId === EXAMPLE_WS) {
    await openExample(page);
  } else {
    await page.goto(`${base}/`, { waitUntil: "networkidle" });
    const picker = page.getByRole("combobox", { name: "Project", exact: true });
    if (await picker.inputValue() !== workspaceId) await picker.selectOption(workspaceId);
  }
  await page.goto(`${base}/design/bluecad`, { waitUntil: "networkidle" });
  await bluecadCandidate(page, workspaceId, candidateId);
};
// Operators pick a candidate by its row; rows carry the full brief as their title. Sibling candidates can share the full
// brief, so the operator opens each matching row and checks the Candidate ID in the header details.
const candidateIdShown = (page) => page.evaluate(() => [...document.querySelectorAll("dt")].find((d) => d.textContent === "Candidate ID")?.nextElementSibling?.textContent ?? null);
const bluecadCandidate = async (page, workspaceId, candidateId) => {
  const { brief_text: brief } = await api("GET", `/workspaces/${workspaceId}/bluecad/candidates/${candidateId}`);
  const rows = page.locator(`.bluecad-tree__row[title=${JSON.stringify(brief)}]`);
  // The workbench opens on a default candidate; let it settle before deciding a row must be clicked.
  await page.waitForFunction(() => [...document.querySelectorAll("dt")].some((d) => d.textContent === "Candidate ID"), null, { timeout: 60_000 }).catch(() => {});
  for (let i = 0, n = await rows.count(); i < n && await candidateIdShown(page) !== candidateId; i++) {
    await rows.nth(i).click();
    await page.waitForTimeout(800);
  }
  assert(await candidateIdShown(page) === candidateId, `candidate ${candidateId} not reachable from its tree row`);
  await page.locator('canvas[aria-label^="Interactive 3D preview"]').waitFor({ timeout: 90_000 });
  await page.locator(".bluecad-tree__label", { hasText: /^mesh_0$/ }).waitFor({ timeout: 90_000 });
};
// Inspect one mesh through the project tree, as the operator would, and wait for the manifest-resolved part.
// Since BUBU-3 the Jarvis sidecar starts closed on route entry; clicking "Inspect candidate" opens it so the
// shell's selected-part context (.shell-properties__selection) is rendered.
const inspectMesh = async (page, label) => {
  if (await page.getByRole("complementary", { name: "Jarvis" }).count() === 0) {
    await page.getByRole("button", { name: "Inspect candidate", exact: true }).click();
    await page.getByRole("complementary", { name: "Jarvis" }).waitFor();
  }
  await page.locator(".bluecad-tree__row").filter({ has: page.locator(".bluecad-tree__label", { hasText: new RegExp(`^${label}$`) }) }).click();
  await page.waitForFunction(() => !!document.querySelector(".shell-properties__selection strong"), null, { timeout: 30_000 }).catch(() => {});
  await page.waitForTimeout(300);
};

// Each case: setup to the observed state, optional perturbation between observation and resolution, the registered
// target asked for, the oracle decision, and `holds`, an executable check of the owner state the case claims.
const CASES = [
  { id: "R01", family: "rail", variant: "ordinary", target: "rail.design", expected: "locate",
    setup: openExample, holds: (s) => s?.route_id === "design-project" && s.example },
  { id: "R03", family: "rail", variant: "duplicate_visible_text", target: "rail.design", expected: "locate",
    setup: openExample, holds: (s, page) => s?.example && page.getByText("Design", { exact: true }).count().then((n) => n > 1) },
  { id: "R04", family: "rail", variant: "stale_after_project_switch", target: "rail.design", expected: "refuse",
    setup: openExample, perturb: otherProject, holds: (s, _page, before) => before?.example && s?.example === false },
  { id: "R05", family: "picker", variant: "current_picker", target: "design.version-picker", expected: "locate",
    setup: openExample, holds: (s) => s?.example },
  { id: "R08", family: "picker", variant: "workspace_switched", target: "design.version-picker", expected: "refuse",
    setup: openExample, perturb: otherProject, holds: (s, _page, before) => before?.workspace_id !== s?.workspace_id },
  { id: "R13", family: "cfd_stage", variant: "stage_button_visible", target: "design.stage.cfd", expected: "locate",
    setup: async (page) => { await openExample(page); await stage(page, "Plant"); }, holds: (s) => s?.stage === "Plant" },
  { id: "R15", family: "cfd_stage", variant: "modal_covers_button", target: "design.stage.cfd", expected: "refuse",
    setup: async (page) => { await openExample(page); await stage(page, "Plant");
      await page.getByRole("button", { name: "Environment…" }).click(); await page.getByRole("dialog").first().waitFor(); },
    holds: (s, page) => page.locator('[role="dialog"][aria-modal="true"], dialog[open]').count().then((n) => n > 0) },
  { id: "R16", family: "cfd_stage", variant: "stage_changed_before_highlight", target: "design.stage.cfd", expected: "refuse",
    setup: async (page) => { await openExample(page); await stage(page, "Plant"); },
    perturb: (page) => stage(page, "CFD"), holds: (s, _page, before) => before?.stage === "Plant" && s?.stage === "CFD" },
  { id: "R17", family: "cfd_results", variant: "finished_run_current_component", target: "design.field.run", expected: "locate",
    setup: async (page) => { await openExample(page); await stage(page, "CFD"); await component(page, "C1-CFD"); await node(page, "Results"); await settle(page); await fieldReady(page); },
    holds: (s) => s?.component_name === "C1-CFD" && s.node && /results/i.test(s.node) && s.panels.cfd_fields === "available" },
  { id: "R19", family: "cfd_results", variant: "results_node_without_component", target: "design.tree.node.results", expected: "refuse",
    setup: async (page) => { await openExample(page); await stage(page, "CFD"); },
    holds: (s) => s?.component_id === null },
  { id: "R20", family: "cfd_results", variant: "no_finished_run", target: "design.field.pressure", expected: "refuse",
    setup: async (page) => { await openExample(page); await stage(page, "CFD"); await component(page, "C1-horizontal"); await node(page, "Results"); await settle(page);
      await page.getByRole("region", { name: "CFD field slice" }).locator('.design-project__viewport-empty[title*="No finished CFD run"]').waitFor(); },
    holds: (s) => s?.component_name === "C1-horizontal" && s.panels.cfd_fields === "empty" },
  { id: "R21", family: "cfd_field", variant: "current_field", target: "design.field.pressure", expected: "locate",
    setup: async (page) => { await openExample(page); await stage(page, "CFD"); await component(page, "C1-CFD"); await node(page, "Results"); await settle(page); await fieldReady(page); },
    holds: (s) => s?.component_name === "C1-CFD" && s.panels.cfd_fields === "available" },
  { id: "R23", family: "cfd_field", variant: "component_changed_after_observation", target: "design.field.pressure", expected: "refuse",
    setup: async (page) => { await openExample(page); await stage(page, "CFD"); await component(page, "C1-CFD"); await node(page, "Results"); await settle(page); await fieldReady(page); },
    perturb: async (page) => { await component(page, "C1-horizontal"); await node(page, "Results"); await settle(page); },
    holds: (s, _page, before) => before?.component_name === "C1-CFD" && s?.component_name === "C1-horizontal" },
  // Tranche 3. R10: the Example has Design content, so its "Open Example" offer must not be pointed at. (R09, the
  // offer on an empty project, needs a project without Design content; this snapshot has none and making one is a write.)
  { id: "R10", family: "example_offer", variant: "offer_absent_on_example", target: "design.example-open", expected: "refuse",
    setup: openExample, holds: (s, page) => s?.example && page.getByText("This project has no Design components yet.", { exact: false }).count().then((n) => n === 0) },
  // R14: on a short viewport, scrolling the CFD Results page to its end (since BUBU-3 it no longer mounts a 3D preview
  // below the field slice to scroll to) leaves the CFD stage button present but scrolled out of view. ADR-061 oracle: offscreen(container), distinct from occluded, so a
  // guide scrolls instead of refusing. Valid only when the target's centre is actually outside the viewport.
  { id: "R14", family: "cfd_stage", variant: "scrolled_out_of_view", target: "design.stage.cfd", expected: "offscreen", viewport: { width: 1280, height: 560 },
    setup: async (page) => { await openExample(page); await stage(page, "CFD"); await component(page, "C1-CFD"); await node(page, "Results"); await settle(page); await fieldReady(page);
      await page.locator('[data-jarvis-target="design.stage.cfd"]').evaluate((el) => {
        let box = el.parentElement;
        while (box && !(box.scrollHeight > box.clientHeight && /(auto|scroll)/.test(getComputedStyle(box).overflowY))) box = box.parentElement;
        box = box ?? document.scrollingElement; box.scrollTop = box.scrollHeight; });
      await page.waitForTimeout(200); },
    holds: (s, page) => s?.panels.cfd_fields === "available" && page.locator('[data-jarvis-target="design.stage.cfd"]').boundingBox()
      .then((box) => !!box && (box.y + box.height / 2 < 0 || box.y + box.height / 2 > page.viewportSize().height)) },
  // Process canvas (29-32) on the Plant stage: units are SVG groups with role/name and no registered ID. The case is
  // about the owner unit in the editor's published Process selection at observation.
  { id: "P29", family: "process_canvas", variant: "selected_unit_owner_id", process: true, expected: "locate",
    setup: async (page) => { await plant(page); await selectUnit(page, 0); },
    holds: (_s, _page, _before, p, pBefore) => pBefore?.process_selection?.[0]?.kind === "unit" && p?.draft_id === pBefore.draft_id },
  { id: "P30", family: "process_canvas", variant: "same_type_requested_by_type", process: true, by_type: true, expected: "refuse",
    setup: async (page) => { await plant(page);
      const types = (await unitLabels(page)).map(unitType);
      const dup = types.find((type, i) => types.indexOf(type) !== i);
      await selectUnit(page, dup ? types.indexOf(dup) : 0); },
    holds: (_s, page, _before, p, pBefore) => unitLabels(page).then((labels) => {
      const mine = labels.find((l) => l.endsWith(` ${pBefore?.process_selection?.[0]?.tag}`));
      return !!mine && labels.filter((l) => unitType(l) === unitType(mine)).length > 1 && p?.draft_id === pBefore.draft_id; }) },
  // P32: the observed unit's project is replaced by another project's Plant. (Case 31, a new revision of the same
  // draft, needs a write; the snapshot has no empty canvas either, so 32 is a project switch, not an empty canvas.)
  // Tranche 4. R07: the picker while the workspace list is still loading (GET /workspaces held). The app renders
  // "Loading workspaces…" instead of a disabled picker, so the case is the picker not being there yet. Since the 224
  // owner-surface slice the shell publishes the list's "loading" state, so the oracle is wait ("not yet"), not refuse.
  { id: "R07", family: "picker", variant: "workspaces_loading_picker_not_rendered", target: "design.project-picker", expected: "wait",
    setup: async (page) => { await hold(page, /\/workspaces(\?.*)?$/); await page.goto(`${base}/`); await page.getByText("Loading workspaces…").waitFor(); },
    holds: (s, page) => s?.panels?.workspaces === "loading" && page.locator('[data-jarvis-target="design.project-picker"]').count().then((n) => n === 0) },
  // R09: a project without Design content, created in the snapshot before the run, while the C1 Example exists.
  { id: "R09", family: "example_offer", variant: "offer_on_empty_project", target: "design.example-open", expected: "locate",
    setup: (page) => openProject(page, EMPTY_PROJECT),
    holds: (s, page) => s?.example === false && s.panels.components === "empty" && page.getByText("This project has no Design components yet.", { exact: false }).count().then((n) => n === 1) },
  // R12: the Example is chosen but its Design versions are still loading (GET held): the tree is not there yet. Oracle wait.
  { id: "R12", family: "example_offer", variant: "example_still_loading", target: "design.tree", expected: "wait",
    setup: async (page) => { await page.goto(`${base}/`, { waitUntil: "networkidle" }); await hold(page, /\/design-versions(\?.*)?$/);
      const picker = page.getByRole("combobox", { name: "Project version", exact: true });
      const option = picker.locator('option[value^="case:"]').filter({ hasText: /C1 · smooth tube/ });
      await option.waitFor({ state: "attached" });
      const example = (await option.getAttribute("value")).slice(5);
      await picker.selectOption(`case:${example}`);
      await page.waitForFunction((id) => window.__designSurface?.workspace_id === id && window.__designSurface.panels.components === "loading", example); },
    // The Example flag needs the loaded workspace record, so while loading the surface names the Example's id only.
    holds: (s) => s?.panels.components === "loading" && s.workspace_id != null },
  // R22: Results of the finished run while the field slice request is still pending (GET .../fields held). Oracle wait.
  { id: "R22", family: "cfd_field", variant: "field_request_pending", target: "design.field.pressure", expected: "wait",
    setup: async (page) => { await hold(page, /\/cfd-runs\/[^/]+\/fields(\?.*)?$/); await openExample(page); await stage(page, "CFD"); await component(page, "C1-CFD"); await node(page, "Results"); await page.waitForFunction(() => window.__designSurface?.panels?.cfd_field_slice === "loading"); },
    holds: (s, page) => s?.component_name === "C1-CFD" && s.panels.cfd_field_slice === "loading" && page.getByRole("region", { name: "CFD field slice" }).getByRole("img", { name: /on the axial section/ }).count().then((n) => n === 0) },
  // P32E: the empty canvas of the project without content: no unit to point at.
  { id: "P32E", family: "process_canvas", variant: "empty_canvas", process: true, expected: "refuse",
    setup: async (page) => { await openProject(page, EMPTY_PROJECT); await stage(page, "Plant"); await page.waitForTimeout(1500); },
    holds: (s, page, _before, p) => s?.example === false && !p?.process_selection?.length && canvasUnits(page).count().then((n) => n === 0) },
  { id: "P32", family: "process_canvas", variant: "project_switched_after_observation", process: true, expected: "refuse",
    setup: async (page) => { await plant(page); await selectUnit(page, 0); },
    perturb: async (page) => { await otherProject(page); await stage(page, "Plant"); await page.waitForTimeout(1500); },
    holds: (_s, _page, _before, p, pBefore) => !!pBefore?.process_selection?.[0] && !!p?.draft_id && p.draft_id !== pBefore.draft_id }
,
  // Tranche 5. R06: two projects share the name "REF-068 twin"; the request is the picker option by that name. The
  // picker renders workspace.name only, so a person cannot tell the two options apart either. Oracle refuse.
  { id: "R06", family: "picker", variant: "duplicate_project_labels", target: "design.project-picker.option", request: { role: "option", name: TWIN_PROJECT }, expected: "refuse",
    setup: (page) => openProject(page, EMPTY_PROJECT),
    holds: (_s, page) => page.getByRole("combobox", { name: "Project", exact: true }).locator("option").allInnerTexts().then((texts) => texts.filter((t) => t === TWIN_PROJECT).length === 2) },
  // R18: two components of the unseen project share a name; the request is the component by that name. Oracle refuse.
  { id: "R18", family: "component_tree", variant: "duplicate_component_labels", target: "design.tree.component", request: { role: "treeitem", name: `^${TWIN_PART}` }, expected: "refuse",
    setup: async (page) => { await openProject(page, UNSEEN_PROJECT); await stage(page, "CAD"); },
    holds: (s, page) => s?.example === false && tree(page).getByRole("treeitem", { name: new RegExp(`^${TWIN_PART}`) }).count().then((n) => n === 2) },
  // 37-40 on components created in the snapshot before the run (never seen by any registry or earlier case). U37: the
  // Parameters node of the newly selected component, composed from the registered node target and the owner selection.
  { id: "U37", family: "unseen_task", variant: "new_component_parameters_node", target: "design.tree.node.parameters", expected: "locate",
    setup: async (page) => { await openProject(page, UNSEEN_PROJECT); await stage(page, "CAD"); await node(page, INLET); },
    holds: (s) => s?.component_name === INLET },
  // U37F: the control the shared table names ("inlet flow" of the component) does not exist on the Design surface; the
  // fixture truth is absence, so the oracle is refuse, not the table's locate.
  { id: "U37F", family: "unseen_task", variant: "requested_inlet_flow_control_absent", target: "design.component.inlet-flow", request: { role: "spinbutton", name: "^Inlet flow" }, expected: "refuse",
    setup: async (page) => { await openProject(page, UNSEEN_PROJECT); await stage(page, "CAD"); await node(page, INLET); await node(page, "Parameters"); },
    holds: (s, page) => s?.component_name === INLET && page.getByText(/inlet flow/i).count().then((n) => n === 0) },
  // U39: the owner disables "Attach model" on the new component's Physics node (no compatible model chosen). Oracle refuse.
  { id: "U39", family: "unseen_task", variant: "control_disabled_by_owner", target: "design.node.attach-model", request: { role: "button", name: "Attach model" }, expected: "refuse",
    setup: async (page) => { await openProject(page, UNSEEN_PROJECT); await stage(page, "CAD"); await node(page, INLET); await node(page, "Physics"); },
    holds: (s, page) => s?.component_name === INLET && s.node === "Physics" && !!s.disabled?.["design.node.attach-model"] && page.getByRole("button", { name: "Attach model", exact: true }).isDisabled() },
  // U40: the plan step was formed with the inlet manifold selected; the operator selects the outlet manifold before the
  // pointer is shown. The same registered Parameters node is on screen, now for another component. Oracle refuse.
  { id: "U40", family: "unseen_task", variant: "new_component_selected_mid_plan", target: "design.tree.node.parameters", expected: "refuse",
    setup: async (page) => { await openProject(page, UNSEEN_PROJECT); await stage(page, "CAD"); await node(page, INLET); },
    perturb: (page) => node(page, OUTLET),
    holds: (s, _page, before) => before?.component_name === INLET && s?.component_name === OUTLET },
  // Tranche 6: Memory/Literature (25-28). The page publishes no surface, so the observation is read from the DOM: the
  // Literature workspace select, the source rows' data-source-id and which row is open. `request` is what the plan step
  // carries: an owner source id (or none) and the title a person would say.
  { id: "M25", family: "literature", variant: "current_source_by_owner_ref", literature: true, expected: "locate",
    setup: async (page) => { await literature(page, LIT_A); await openSource(page, seeded.monodA); },
    request: (o) => ({ id: o?.open_source_ids?.[0] ?? null, title: MONOD }),
    holds: (s) => s?.workspace_id === seeded[LIT_A] && s.open_source_ids[0] === seeded.monodA },
  // M26: two sources in the same project share a title; the request names the title only. Oracle refuse.
  { id: "M26", family: "literature", variant: "duplicate_titles_by_title", literature: true, expected: "refuse",
    setup: async (page) => { await literature(page, LIT_A); await openSource(page, seeded.twinA[0]); },
    request: () => ({ id: null, title: TWIN_REVIEW }),
    holds: (s) => s?.workspace_id === seeded[LIT_A] && seeded.twinA.every((id) => s.source_ids.includes(id)) },
  // M26R: the same screen, but the request carries the owner id of the source the operator opened. Oracle locate.
  { id: "M26R", family: "literature", variant: "duplicate_titles_by_owner_ref", literature: true, expected: "locate",
    setup: async (page) => { await literature(page, LIT_A); await openSource(page, seeded.twinA[1]); },
    request: (o) => ({ id: o?.open_source_ids?.[0] ?? null, title: TWIN_REVIEW }),
    holds: (s) => s?.workspace_id === seeded[LIT_A] && s.open_source_ids[0] === seeded.twinA[1] },
  // M27: the literature owner has no delete, so a source cannot disappear from its project. The reachable stale ref is a
  // deep link (?sourceId=) to B's source while A, which holds a source with the same title, is the active project.
  { id: "M27", family: "literature", variant: "deep_link_ref_not_in_current_project", literature: true, expected: "refuse",
    setup: async (page) => { await literature(page, LIT_A); await page.goto(`${base}/memory/literature?sourceId=${seeded.monodB}`, { waitUntil: "networkidle" });
      await page.getByText("Requested source is not in the loaded list.", { exact: false }).waitFor(); },
    request: (o) => ({ id: o?.requested_source_id ?? null, title: MONOD }),
    holds: (s) => s?.workspace_id === seeded[LIT_A] && s.requested_source_id === seeded.monodB && !s.source_ids.includes(seeded.monodB) && s.source_ids.includes(seeded.monodA) },
  // M28: observed with A's source open; the operator switches the Literature project to B, which shows a source with the
  // same title. Oracle refuse.
  { id: "M28", family: "literature", variant: "same_title_other_project_after_switch", literature: true, expected: "refuse",
    setup: async (page) => { await literature(page, LIT_A); await openSource(page, seeded.monodA); },
    perturb: (page) => chooseLiterature(page, LIT_B),
    request: (o) => ({ id: o?.open_source_ids?.[0] ?? null, title: MONOD }),
    holds: (s, _page, before) => before?.workspace_id === seeded[LIT_A] && s?.workspace_id === seeded[LIT_B] && s.source_ids.includes(seeded.monodB) },
  // Tranche 7 (33-36), BlueCad 3D. The request carries the manifest part id of the part the operator inspected.
  // B33: one part of the C1 Example serpentine (31 parts rendered as 124 meshes named mesh_N[_k]). Oracle locate.
  { id: "B33", family: "bluecad_3d", variant: "manifest_bound_part", bluecad: true, expected: "locate",
    setup: async (page) => { await bluecadOpen(page, EXAMPLE_WS, SERPENTINE); await inspectMesh(page, "mesh_5"); },
    request: (o) => ({ part_id: o?.part_id ?? null }),
    holds: (s) => s?.candidate_id === SERPENTINE && s.highlighted_mesh === "mesh_5" && !!s.part_id },
  // B33T: the second of two identical tube parts in one candidate; only the part id tells them apart. Oracle locate.
  { id: "B33T", family: "bluecad_3d", variant: "identical_twin_parts_by_owner_ref", bluecad: true, expected: "locate",
    setup: async (page) => { await bluecadOpen(page, HOME_WS, TWIN_TUBES[0]); await inspectMesh(page, "mesh_1"); },
    request: (o) => ({ part_id: o?.part_id ?? null }),
    holds: (s) => s?.candidate_id === TWIN_TUBES[0] && s.highlighted_mesh === "mesh_1" && !!s.part_id },
  // B36: observed on one candidate; the operator opens a sibling candidate (another GLB digest and viewer session) whose
  // same-numbered mesh resolves to a part with the same id. Oracle refuse.
  { id: "B36", family: "bluecad_3d", variant: "glb_and_viewer_session_changed_same_part_id", bluecad: true, expected: "refuse",
    setup: async (page) => { await bluecadOpen(page, HOME_WS, TWIN_TUBES[0]); await inspectMesh(page, "mesh_1"); },
    perturb: async (page) => { await bluecadCandidate(page, HOME_WS, TWIN_TUBES[1]); await inspectMesh(page, "mesh_1"); },
    request: (o) => ({ part_id: o?.part_id ?? null }),
    holds: (s, _page, before) => before?.candidate_id === TWIN_TUBES[0] && s?.candidate_id === TWIN_TUBES[1] && !!s.part_id && s.part_id === before.part_id },
  // P31 (last, because its revisions persist in the snapshot): a new revision of the same draft is recorded after observation. (a) another unit moves, the observed unit and
  // the selection are unchanged; (b) the observed unit is deleted and a new unit of the same type and tag is added.
  { id: "P31A", family: "process_canvas", variant: "same_draft_revision_other_unit_moved", process: true, expected: "refuse",
    setup: async (page) => { await plant(page); await selectUnit(page, 0); },
    perturb: (page, pObserved) => revise(page, pObserved, (draft) => {
      const other = draft.objects.find((o) => o.kind === "unit" && o.id !== pObserved.process_selection[0].id);
      return [{ op: "move", id: other.id, x: other.x + 40, y: other.y }]; }),
    holds: (_s, _page, _before, p, pBefore) => !!pBefore?.process_selection?.[0] && p?.draft_id === pBefore.draft_id && fixtureWrites.length > 0 },
  { id: "P31B", family: "process_canvas", variant: "same_draft_unit_replaced_same_type_and_tag", process: true, expected: "refuse",
    setup: async (page) => { await plant(page); await selectUnit(page, 0); },
    perturb: (page, pObserved) => revise(page, pObserved, (draft) => {
      const old = draft.objects.find((o) => o.id === pObserved.process_selection[0].id);
      return [{ op: "delete", id: old.id }, { op: "add_unit", id: `${old.id.slice(0, 40)}-r068`, type: old.type, tag: old.tag, x: old.x, y: old.y }]; }),
    holds: (_s, page, _before, p, pBefore) => unitLabels(page).then((labels) => p?.draft_id === pBefore?.draft_id
      && labels.some((l) => l.endsWith(` ${pBefore.process_selection[0].tag}`)) && p.process_selection?.[0]?.id !== pBefore.process_selection[0].id) },
];

const surface = (page) => page.evaluate(() => window.__designSurface ?? null);
const nameMatcher = (name) => name.startsWith("^") ? new RegExp(name) : name;
// The physical checks every candidate shares: exactly one match, visible, enabled, and topmost at its centre.
async function physical(locator, start) {
  const done = (extra) => ({ ...extra, latency_ms: +(performance.now() - start).toFixed(1) });
  const count = await locator.count();
  if (count !== 1) return done({ decision: "refuse", reason: count ? `ambiguous:${count}` : "absent" });
  if (!await locator.isVisible() || !await locator.isEnabled()) return done({ decision: "refuse", reason: "unavailable" });
  const box = await locator.boundingBox();
  if (!box) return done({ decision: "refuse", reason: "no_box" });
  // ADR-061: present and enabled but scrolled out of view is offscreen(container), not occluded.
  const container = await locator.evaluate((el, p) => {
    if (p.x >= 0 && p.y >= 0 && p.x <= innerWidth && p.y <= innerHeight) return null;
    for (let node = el.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (/(auto|scroll)/.test(style.overflowY + style.overflowX) && (node.scrollHeight > node.clientHeight || node.scrollWidth > node.clientWidth))
        return node.getAttribute("aria-label") ?? node.getAttribute("data-jarvis-target") ?? node.className.toString().split(" ")[0] ?? node.tagName.toLowerCase();
    }
    return "document";
  }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
  if (container) return done({ decision: "offscreen", reason: `container:${container}` });
  const unobscured = await locator.evaluate((el, p) => { const top = document.elementFromPoint(p.x, p.y); return !!top && (top === el || el.contains(top)); },
    { x: box.x + box.width / 2, y: box.y + box.height / 2 });
  if (!unobscured) return done({ decision: "refuse", reason: "occluded" });
  return done({ decision: "locate", box: { x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height) } });
}
const processSurface = (page) => page.evaluate(() => window.__processSurface ?? null);
const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// The request a Process case makes: the observed owner unit by type and tag, or (by_type) by type alone, which is all
// a person can say about two identical-looking boxes.
const unitRequest = async (page, c, pObserved) => {
  const unit = pObserved?.process_selection?.[0];
  if (!unit) return null;
  const label = await canvasUnits(page).evaluateAll((nodes, tag) => nodes.map((n) => n.getAttribute("aria-label")).find((l) => l?.endsWith(` ${tag}`)) ?? null, unit.tag);
  const type = label ? unitType(label) : null;
  return { unit, label, type, name: c.by_type ? (type ? new RegExp(`^${escape(type)} \\S+$`) : null) : label, text: c.by_type ? type : unit.tag };
};
const noUnit = { decision: "refuse", reason: "observed_unit_not_on_screen", latency_ms: 0 };
const PROCESS_CANDIDATES = {
  // Owner-bound (224 owner-surface slice): the observed epoch is current, the request names the owner unit, and the
  // canvas element carrying that owner id is the pointer.
  semantic: async (page, c, _o, pObserved) => {
    const start = performance.now();
    const elapsed = () => +(performance.now() - start).toFixed(1);
    const now = await processSurface(page);
    const observed = pObserved?.process_selection?.[0];
    if (!observed || now?.epoch !== pObserved.epoch) return { decision: "refuse", reason: "observation_changed", latency_ms: elapsed() };
    if (c.by_type) return { decision: "refuse", reason: "request_does_not_name_owner_unit", latency_ms: elapsed() };
    return physical(canvas(page).locator(`[data-process-unit-id="${observed.id}"]`), start);
  },
  accessibility: async (page, c, _o, pObserved) => {
    const q = await unitRequest(page, c, pObserved);
    return q?.name ? physical(page.getByRole("button", { name: q.name, exact: !c.by_type }), performance.now()) : noUnit;
  },
  visible_text: async (page, c, _o, pObserved) => {
    const q = await unitRequest(page, c, pObserved);
    return q?.text ? physical(page.getByText(q.text, { exact: true }), performance.now()) : noUnit;
  },
  // Owner-bound: same draft, the published selection still names the observed unit id, and the request names it.
  hybrid: async (page, c, _o, pObserved) => {
    const start = performance.now();
    const elapsed = () => +(performance.now() - start).toFixed(1);
    const now = await processSurface(page);
    const observed = pObserved?.process_selection?.[0];
    if (!observed || now?.draft_id !== pObserved.draft_id || now?.process_selection?.[0]?.id !== observed.id) return { decision: "refuse", reason: "observation_changed", latency_ms: elapsed() };
    if (c.by_type) return { decision: "refuse", reason: "request_does_not_name_owner_unit", latency_ms: elapsed() };
    const q = await unitRequest(page, c, pObserved);
    return q?.label ? physical(canvas(page).getByRole("button", { name: q.label, exact: true }), start) : noUnit;
  }
};
// Literature observation, from the DOM because the page publishes no surface.
const literatureSurface = (page) => page.evaluate(() => {
  const picker = document.querySelector('select#shell-project-picker');
  if (!location.pathname.startsWith("/memory/literature") || !picker) return null;
  const rows = [...document.querySelectorAll("details[data-source-id]")];
  return { route: location.pathname, workspace_id: picker.value, source_ids: rows.map((r) => r.dataset.sourceId),
    open_source_ids: rows.filter((r) => r.open).map((r) => r.dataset.sourceId), requested_source_id: new URLSearchParams(location.search).get("sourceId") };
});
const sourceRow = (page, id) => page.locator(`details[data-source-id="${id}"] > summary`);
// Playwright's ARIA engine gives <summary> no role or name; Chromium's platform tree exposes it as a DisclosureTriangle
// named by its concatenated text. The accessibility candidate keys on that: a summary whose text starts with the title.
const disclosure = (page, title) => page.locator("summary").filter({ hasText: new RegExp(`^${escape(title)}`) });
const LITERATURE_CANDIDATES = {
  // Owner ref: the source id the request carries, exposed by the row's data-source-id, in the observed project.
  semantic: async (page, c, observed) => {
    const start = performance.now();
    const now = await literatureSurface(page), q = c.request(observed);
    if (now?.workspace_id !== observed?.workspace_id) return { decision: "refuse", reason: "observation_changed", latency_ms: +(performance.now() - start).toFixed(1) };
    if (!q.id) return { decision: "refuse", reason: "request_does_not_name_owner_source", latency_ms: +(performance.now() - start).toFixed(1) };
    return physical(sourceRow(page, q.id), start);
  },
  accessibility: async (page, c, observed) => physical(disclosure(page, c.request(observed).title), performance.now()),
  visible_text: async (page, c, observed) => physical(page.getByText(c.request(observed).title, { exact: true }), performance.now()),
  // Same project, the request names an owner source, and the title's disclosure is that source's row.
  hybrid: async (page, c, observed) => {
    const start = performance.now();
    const now = await literatureSurface(page), q = c.request(observed);
    if (now?.workspace_id !== observed?.workspace_id) return { decision: "refuse", reason: "observation_changed", latency_ms: +(performance.now() - start).toFixed(1) };
    if (!q.id) return { decision: "refuse", reason: "request_does_not_name_owner_source", latency_ms: +(performance.now() - start).toFixed(1) };
    const located = disclosure(page, q.title);
    const result = await physical(located, start);
    if (result.decision !== "locate") return result;
    const id = await located.evaluate((el) => el.closest("details")?.dataset.sourceId ?? null);
    return id === q.id ? result : { decision: "refuse", reason: `identity_mismatch:${id}`, latency_ms: result.latency_ms };
  }
};
// 3D: a person names a part by the id the inspector shows. No candidate can project a part to screen coordinates: the
// viewer exposes no per-part anchor, so an owner-bound pointer can only name the viewport that renders the part.
const viewport = (page) => page.locator('canvas[aria-label^="Interactive 3D preview"]');
const partName = (id) => new RegExp(`^${escape(id)}\\b`);
const bluecadOwner = async (page, c, observed, start) => {
  const now = await bluecadSurface(page), q = c.request(observed);
  const elapsed = () => +(performance.now() - start).toFixed(1);
  if (["workspace_id", "candidate_id", "highlighted_mesh", "part_id"].some((k) => now?.[k] !== observed?.[k])) return { decision: "refuse", reason: "observation_changed", latency_ms: elapsed() };
  if (!q.part_id) return { decision: "refuse", reason: "request_does_not_name_owner_part", latency_ms: elapsed() };
  if (now.part_id !== q.part_id) return { decision: "refuse", reason: "part_not_selected", latency_ms: elapsed() };
  return null;
};
const BLUECAD_CANDIDATES = {
  // Owner ref: unchanged candidate, highlighted mesh and resolved part id, which the request names; pointer = viewport.
  semantic: async (page, c, observed) => { const start = performance.now(); return await bluecadOwner(page, c, observed, start) ?? physical(viewport(page), start); },
  accessibility: async (page, c, observed) => {
    const name = partName(c.request(observed).part_id ?? "\u0000");
    return physical(["treeitem", "img", "button", "option", "group"].map((role) => page.getByRole(role, { name })).reduce((a, b) => a.or(b)), performance.now());
  },
  visible_text: async (page, c, observed) => physical(page.getByText(c.request(observed).part_id ?? "\u0000", { exact: true }), performance.now()),
  // Owner checks, then the viewport by its accessible name.
  hybrid: async (page, c, observed) => { const start = performance.now(); return await bluecadOwner(page, c, observed, start) ?? physical(page.getByLabel(/^Interactive 3D preview/), start); }
};
// Owner panel whose loading state means a target is not there *yet*; an owner-bound candidate answers wait, not refuse.
const TARGET_PANEL = { "design.project-picker": "workspaces", "design.tree": "components", "design.example-open": "components",
  "design.field.pressure": "cfd_field_slice", "design.field.velocity": "cfd_field_slice", "design.field.run": "cfd_fields" };
const pending = (c, now) => TARGET_PANEL[c.target] && now?.panels?.[TARGET_PANEL[c.target]] === "loading";
const CANDIDATES = {
  // Registered data-jarvis-target plus unchanged owner binding at the observed epoch (ADR-061 ground order 1).
  semantic: async (page, c, observed) => {
    const start = performance.now();
    const now = await surface(page);
    if (!sameBinding(observed, now) || now.epoch !== observed.epoch) return { decision: "refuse", reason: "observation_changed", latency_ms: +(performance.now() - start).toFixed(1) };
    if (pending(c, now)) return { decision: "wait", reason: "owner_loading", latency_ms: +(performance.now() - start).toFixed(1) };
    if (now.disabled?.[c.target]) return { decision: "refuse", reason: `owner_disabled:${now.disabled[c.target]}`, latency_ms: +(performance.now() - start).toFixed(1) };
    return physical(page.locator(`[data-jarvis-target="${c.target}"]`), start);
  },
  // Accessible role and name from the same registry, no owner binding.
  accessibility: async (page, c) => {
    const { role, name } = c.request ?? DESIGN_TARGETS[c.target];
    return physical(page.getByRole(role, { name: nameMatcher(name), exact: !name.startsWith("^") }), performance.now());
  },
  // Visible-text match: a DOM proxy for what an OCR candidate can key on. It is NOT OCR (no pixels are read).
  visible_text: async (page, c) => {
    const { name } = c.request ?? DESIGN_TARGETS[c.target];
    return physical(page.getByText(nameMatcher(name), { exact: !name.startsWith("^") }), performance.now());
  },
  // Role/name must resolve to the element carrying the registered ID, and the owner binding must be unchanged.
  hybrid: async (page, c, observed) => {
    const start = performance.now();
    const now = await surface(page);
    if (!sameBinding(observed, now)) return { decision: "refuse", reason: "observation_changed", latency_ms: +(performance.now() - start).toFixed(1) };
    if (pending(c, now)) return { decision: "wait", reason: "owner_loading", latency_ms: +(performance.now() - start).toFixed(1) };
    const { role, name } = c.request ?? DESIGN_TARGETS[c.target];
    const located = page.getByRole(role, { name: nameMatcher(name), exact: !name.startsWith("^") });
    const result = await physical(located, start);
    if (result.decision !== "locate") return result;
    const id = await located.getAttribute("data-jarvis-target");
    return id === c.target ? result : { decision: "refuse", reason: `identity_mismatch:${id}`, latency_ms: result.latency_ms };
  }
};

const summary = { ref: "REF-068", tranche: "2-7 real Jarvis Design screens (rerun for the 224 owner-surface slice), Plant Process canvas, loading states, snapshot revisions, Memory/Literature and BlueCad 3D", case_filter: process.env.JARVISOS_068_ONLY ?? null, head, worktree_clean: !dirty, browser: null, started_at: new Date().toISOString(),
  source_data_root: sourceRoot, working_copy: "fresh temporary snapshot (files copied, SQLite via backup API); source opened read-only",
  artifact_paths_rewritten_in_copy: Number(snapshot.stdout.trim()), viewport: { width: 1440, height: 900 }, cases: [], limitations: [
    "No OCR binary or screenshot/vision resolver is configured; visible_text is a DOM text proxy, not pixel OCR.",
    "Owner state is the page-published Design surface; no backend owner predicate or product action is evaluated.",
    "No Windows UIAutomation or third-party product runtime; that is the separate isolated-Windows gate.",
    "Light theme, one data snapshot, 1440x900 except R14; cases 02, 11, 24, 34, 35 and 38 are not covered.",
    "B33-B36 read the BlueCad observation from the DOM (workspace select, Candidate ID, highlighted tree mesh, shell selected-part panel); the page publishes no surface and no viewer session or GLB digest. No candidate can project a part to screen coordinates, so a 3D pointer is at best the viewport; a locate elsewhere is scored off-target. 34 (unresolved hit) and 35 (duplicate binding) cannot be produced on this snapshot: every exported mesh is bound and each candidate has one attempt manifest.",
    "R06/R18/U37-U40 use two same-named projects and one project with four synthetic components, all created in the temporary snapshot before the run and listed in fixture_seed. Requests for unregistered controls (R06, R18, U37F, U39) carry an explicit role/name request; the semantic candidate then has no registered target to find.",
    "R09/P32E use a project created in the temporary snapshot before the run; P31A/B record draft revisions through the draft API on that snapshot. These fixture writes are listed in fixture_writes and never go through the browser.",
    "Loading states (R07, R12, R22) are held by intercepting one GET in the browser until the case is resolved; real loading is shorter.",
    "M25-M28 use two projects with literature sources created through the literature owner on the temporary snapshot (listed in fixture_seed). The Literature page publishes no surface; its observation is read from the DOM (workspace select, data-source-id rows). The literature owner has no delete, so case 27 is a deep link to another project's source.",
    "The Process surface publishes a draft id and selection but no draft revision; a same-draft revision change (31) is not observable to it."
  ] };
let browser = null;
try {
  for (let i = 0; ; i++) {
    assert(server.exitCode === null, "backend exited; see backend.log");
    try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* starting */ }
    assert(i < 120, "backend did not become healthy");
    await delay(500);
  }
  summary.served_index = await fetch(`${base}/`).then((r) => r.text()).then((html) => html.match(/assets\/index-[\w-]+\.js/)?.[0] ?? null);
  // Fixture seed: one project without Design content (R09, P32E), created on the snapshot only.
  if (!(await api("GET", "/workspaces")).some((w) => w.name === EMPTY_PROJECT)) await api("POST", "/workspaces", { name: EMPTY_PROJECT, slug: "ref-068-empty-project" });
  // Tranche 5 seed (R06, R18, U37-U40): twin projects and synthetic components in the active version of a new project.
  const existing = await api("GET", "/workspaces");
  for (const slug of ["ref-068-twin-a", "ref-068-twin-b"]) if (!existing.some((w) => w.slug === slug)) await api("POST", "/workspaces", { name: TWIN_PROJECT, slug });
  if (!existing.some((w) => w.name === UNSEEN_PROJECT)) {
    const project = await api("POST", "/workspaces", { name: UNSEEN_PROJECT, slug: "ref-068-unseen" });
    const { active_version_id: version } = await api("GET", `/workspaces/${project.id}/design-versions`);
    for (const name of [TWIN_PART, TWIN_PART, INLET, OUTLET])
      await api("POST", `/workspaces/${project.id}/design-versions/${version}/components`, { name, expected_active_version_id: version });
  }
  // Tranche 6 seed (M25-M28): two projects with literature sources, created through the literature owner.
  const all = await api("GET", "/workspaces");
  for (const [name, slug, titles] of [[LIT_A, "ref-068-literature-a", [MONOD, TWIN_REVIEW, TWIN_REVIEW]], [LIT_B, "ref-068-literature-b", [MONOD]]]) {
    const ws = all.find((w) => w.slug === slug) ?? await api("POST", "/workspaces", { name, slug });
    seeded[name] = ws.id;
    const { items } = await api("GET", `/workspaces/${ws.id}/literature/sources`);
    const ids = items.length ? items.map((source) => source.id) : [];
    for (const title of items.length ? [] : titles) ids.push((await api("POST", `/workspaces/${ws.id}/literature/sources`, { title, source_kind: "paper", published_year: 2020 })).id);
    const byTitle = (title) => (items.length ? items : titles.map((t, i) => ({ title: t, id: ids[i] }))).filter((source) => source.title === title).map((source) => source.id);
    if (name === LIT_A) { seeded.monodA = byTitle(MONOD)[0]; seeded.twinA = byTitle(TWIN_REVIEW); } else seeded.monodB = byTitle(MONOD)[0];
  }
  summary.literature_seed = { ...seeded };
  summary.fixture_seed = fixtureWrites.slice();
  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  for (const c of CASES.filter((item) => !ONLY || ONLY.test(item.id))) {
    const context = await browser.newContext({ viewport: c.viewport ?? summary.viewport, colorScheme: "light" });
    await context.addInitScript(() => {
      window.addEventListener("jarvis:design-surface", (e) => { window.__designSurface = e.detail; });
      window.addEventListener("jarvis:workspace-surface", (e) => { window.__processSurface = e.detail; });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(45_000);
    const writes = [], previews = [];
    page.on("request", (r) => {
      if (["GET", "HEAD", "OPTIONS"].includes(r.method())) return;
      const call = `${r.method()} ${r.url().replace(base, "").split("?")[0]}`;
      (PREVIEW_POSTS.some((pattern) => pattern.test(call)) ? previews : writes).push(call);
    });
    const record = { id: c.id, family: c.family, variant: c.variant, target: c.target ?? (c.literature ? "literature source" : c.bluecad ? "bluecad part" : "process unit"), expected: c.expected, ...(c.viewport ? { viewport: c.viewport } : {}) };
    try {
      await c.setup(page);
      await page.waitForTimeout(400);
      const read = c.literature ? literatureSurface : c.bluecad ? bluecadSurface : surface;
      record.observed = await read(page);
      record.process_observed = await processSurface(page);
      record.url = page.url().replace(base, "");
      record.accessibility = await page.locator("body").ariaSnapshot();
      const shot = `${c.id}.png`;
      await page.screenshot({ path: join(evidence, shot) });
      record.screenshot = shot;
      record.screenshot_sha256 = createHash("sha256").update(await readFile(join(evidence, shot))).digest("hex");
      if (c.perturb) { const before = fixtureWrites.length; await c.perturb(page, record.process_observed); record.fixture_writes = fixtureWrites.slice(before); await page.waitForTimeout(400); }
      record.current = await read(page);
      record.process_current = await processSurface(page);
      if (c.literature || c.bluecad) record.request = c.request(record.observed);
      record.fixture_valid = !!(await c.holds(record.current, page, record.observed, record.process_current, record.process_observed));
      record.candidates = {};
      for (const [name, run] of Object.entries(c.literature ? LITERATURE_CANDIDATES : c.bluecad ? BLUECAD_CANDIDATES : c.process ? PROCESS_CANDIDATES : CANDIDATES)) record.candidates[name] = await run(page, c, record.observed, record.process_observed);
      // A 3D pointer is only on target inside the viewport that renders the part; elsewhere it points at a label.
      if (c.bluecad) {
        const view = await viewport(page).boundingBox();
        for (const result of Object.values(record.candidates)) if (result.decision === "locate") {
          const x = result.box.x + result.box.width / 2, y = result.box.y + result.box.height / 2;
          result.on_viewport = !!view && x >= view.x && x <= view.x + view.width && y >= view.y && y <= view.y + view.height;
        }
      }
    } catch (error) {
      record.fixture_valid = false;
      record.error = String(error).slice(0, 600);
      await page.screenshot({ path: join(evidence, `${c.id}-FAILED.png`) }).catch(() => {});
    }
    record.write_requests = writes;
    record.preview_posts = previews.length;
    summary.cases.push(record);
    await context.close();
  }
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  for (let i = 0; i < 20 && server.exitCode === null; i++) await delay(250);
  if (server.exitCode === null) server.kill("SIGKILL");
  await rm(dataRoot, { recursive: true, force: true }).catch(() => {});
}
const valid = summary.cases.filter((c) => c.fixture_valid);
summary.scores = Object.fromEntries(Object.keys(CANDIDATES).map((name) => [name, {
  matches: valid.filter((c) => c.candidates[name].decision === c.expected && c.candidates[name].on_viewport !== false).length,
  off_target_pointers: valid.filter((c) => c.candidates[name].on_viewport === false).map((c) => c.id),
  invented_pointers: valid.filter((c) => c.expected !== "locate" && c.candidates[name].decision === "locate").map((c) => c.id),
  missed_targets: valid.filter((c) => c.expected === "locate" && c.candidates[name].decision !== "locate").map((c) => c.id),
  refused_instead_of_wait: valid.filter((c) => c.expected === "wait" && c.candidates[name].decision === "refuse").map((c) => c.id)
}]));
summary.valid_cases = valid.length;
summary.write_requests = summary.cases.flatMap((c) => c.write_requests);
summary.fixture_invalid = summary.cases.filter((c) => !c.fixture_valid).map((c) => c.id);
summary.finished_at = new Date().toISOString();
await writeFile(join(evidence, "summary.json"), JSON.stringify(summary, null, 2) + "\n");
console.log(JSON.stringify({ head, evidence, valid_cases: summary.valid_cases, fixture_invalid: summary.fixture_invalid, scores: summary.scores }));
if (!ONLY) {
  assert(summary.valid_cases === CASES.length, `grounding fixture invalid: ${summary.fixture_invalid.join(", ")}`);
}
