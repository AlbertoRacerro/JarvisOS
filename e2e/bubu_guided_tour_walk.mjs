#!/usr/bin/env node
// Spec 242: real-Chromium walk of Bubu's guided tour. A clean profile opens "/", opens Bubu and starts "Guided tour" with
// visible controls, presses Next through every stop (recording the route, the pointed target or the "Not available here"
// line), tests Back, Exit and Escape, and records every network request: none may be POST, PUT, DELETE or PATCH.
// With a data-root copy that holds the C1 case study the Geometry/CFD stops must point at the real surfaces; on an empty
// data root they must say so and the tour must still complete. Runs on a temporary data root; never the live app.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/bubu_guided_tour_walk.mjs <evidence-dir> [<source-data-root>]
// Env: JARVISOS_PROOF_PORT (8242), PLAYWRIGHT_MODULE.
import { createHash } from "node:crypto";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) { console.log("node e2e/bubu_guided_tour_walk.mjs <evidence-dir> [<source-data-root>]  (needs built frontend, Playwright)"); process.exit(0); }
const playwright = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const chromium = playwright.chromium ?? playwright.default.chromium;
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8242);
const assert = (ok, message) => { if (!ok) throw new Error(message); };
assert(port !== 8000, "port 8000 is the live app; refusing");
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: bubu_guided_tour_walk.mjs <evidence-dir> [<source-data-root>]");
const evidence = resolve(process.argv[2]);
const sourceRoot = process.argv[3] ? resolve(process.argv[3]) : null;
await mkdir(evidence, { recursive: true });

await new Promise((ok, fail) => {
  const probe = createServer().once("error", () => fail(new Error(`port ${port} is in use; refusing to use a foreign backend`)))
    .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1");
});
const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-242-data-"));
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host") };
delete env.JARVISOS_BLUECAD_TOOL_REGISTRY;
if (sourceRoot) {
  // Snapshot: files are copied, the SQLite database through the backup API; the source is opened read-only.
  await cp(sourceRoot, dataRoot, { recursive: true, filter: (path) => !/jarvisos\.db(-wal|-shm|-journal)?$/.test(path) });
  const snapshot = spawnSync(python, ["-I", "-c", [
    "import sqlite3,sys",
    "src=sqlite3.connect('file:'+sys.argv[2]+'/jarvisos.db?mode=ro',uri=True); dst=sqlite3.connect(sys.argv[1]+'/jarvisos.db'); src.backup(dst)",
    "n=dst.execute(\"UPDATE artifacts SET stored_path=? || substr(stored_path, ?) WHERE stored_path LIKE ? || '%'\",(sys.argv[1],len(sys.argv[2])+1,sys.argv[2])).rowcount",
    "dst.commit(); print(n)"].join("\n"), dataRoot, sourceRoot], { encoding: "utf8" });
  assert(snapshot.status === 0, `data-root snapshot failed: ${snapshot.stderr}`);
}
// Bring the (fresh or copied) database to the current schema before the backend serves it; a fresh root gets a workspace.
const init = spawnSync(python, ["-c", [
  "from app.core.database import initialize_database",
  "from app.modules.workspaces.service import create_workspace, default_workspace_payload",
  "initialize_database()", ...(sourceRoot ? [] : ["create_workspace(default_workspace_payload())"])].join("; ")], { cwd: backend, env, encoding: "utf8" });
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);
const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const dirty = spawnSync("git", ["status", "--porcelain"], { cwd: root, encoding: "utf8" }).stdout.trim();
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], { cwd: backend, env, stdio: ["ignore", log, log] });

const summary = { spec: "242", head, worktree_dirty: Boolean(dirty), browser: null, started_at: new Date().toISOString(),
  data_root: sourceRoot ? "temporary snapshot of a source data root (source opened read-only)" : "fresh temporary JARVISOS_DATA_ROOT",
  c1_case_study_present: null, stops: [], checks: [], screenshots: [], tour_requests: [], all_requests: [], error_responses: [], console_errors: [], page_errors: [] };
let browser;
let failure = null;
try {
  for (let i = 0; ; i++) {
    assert(server.exitCode === null, "backend exited; see backend.log");
    try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* starting */ }
    assert(i < 120, "backend did not become healthy");
    await delay(500);
  }
  const workspaces = await (await fetch(`${base}/workspaces`)).json();
  summary.c1_case_study_present = workspaces.some((item) => item.slug === "example-c1-smooth-tube");
  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, colorScheme: "light" });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  let touring = false;
  page.on("console", (m) => { if (m.type() === "error") summary.console_errors.push(m.text()); });
  page.on("pageerror", (e) => summary.page_errors.push(String(e)));
  page.on("request", (r) => {
    const line = `${r.method()} ${r.url().replace(base, "").split("?")[0]}`;
    summary.all_requests.push(line);
    if (touring) summary.tour_requests.push(line);
  });
  page.on("response", (r) => { if (r.status() >= 400) summary.error_responses.push(`${r.status()} ${r.request().method()} ${r.url().replace(base, "")}`); });
  const shot = async (name) => {
    const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file });
    summary.screenshots.push({ file: file.slice(evidence.length + 1), sha256: createHash("sha256").update(await readFile(file)).digest("hex") });
  };
  const check = (name, detail = {}) => summary.checks.push({ check: name, status: "pass", ...detail });
  const card = page.getByRole("dialog", { name: "Guided tour" });
  const position = async () => (await card.locator(".bubu-tour__count").innerText()).trim();

  // A clean profile at "/", reaching the tour with visible controls only.
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  const create = page.getByRole("button", { name: "Create workspace", exact: true });
  if (await create.isVisible()) { await page.getByLabel("Workspace name").fill("Guided Tour Proof"); await create.click(); }
  await page.locator("#bubu-launcher").waitFor();
  await page.locator("#bubu-launcher").click();
  const start = page.getByRole("button", { name: "Guided tour", exact: true });
  await start.waitFor();
  await shot("bubu-bubble-with-guided-tour-entry");
  touring = true;
  await start.click();
  await card.waitFor();
  check("start_from_root_with_visible_controls", { url: page.url().replace(base, "") });

  const settled = async (index) => {
    await page.waitForFunction((label) => document.querySelector(".bubu-tour__count")?.textContent?.trim() === label, `${index} / ${total}`);
    await page.waitForFunction(() => document.querySelector(".bubu-tour")?.getAttribute("data-tour-state") !== "preparing", null, { timeout: 90_000 });
  };
  const read = async () => card.evaluate((el) => {
    const tour = el.querySelector(".bubu-tour");
    const target = tour.getAttribute("data-tour-target");
    const pointed = [...document.querySelectorAll(`[data-jarvis-target="${target}"]`)].find((node) => { const r = node.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
    const rings = [...document.querySelectorAll(".bubu-pointer__ring")].map((ring) => ({ x: +ring.getAttribute("x"), y: +ring.getAttribute("y"), w: +ring.getAttribute("width"), h: +ring.getAttribute("height") }));
    const box = pointed?.getBoundingClientRect();
    const ringsOnTarget = box ? rings.filter((r) => box.left + box.width / 2 >= r.x && box.left + box.width / 2 <= r.x + r.w && box.top + box.height / 2 >= r.y && box.top + box.height / 2 <= r.y + r.h).length : 0;
    return { position: tour.querySelector(".bubu-tour__count").textContent.trim(), title: tour.querySelector(".bubu-tour__title").textContent.trim(),
      route_shown: tour.querySelector(".bubu-tour__route code").textContent.trim(), state: tour.getAttribute("data-tour-state"), target,
      note: tour.querySelector(".bubu-tour__note").textContent.trim(), url: location.pathname + location.search,
      target_on_screen: Boolean(pointed), rings: rings.length, rings_on_target: ringsOnTarget, next_focused: document.activeElement?.id === "bubu-tour-next" };
  });
  const total = Number((await position()).split("/")[1]);
  assert(total >= 12 && total <= 18, `unexpected stop count ${total}`);

  // Next through every stop.
  for (let index = 1; index <= total; index += 1) {
    await settled(index);
    let stop = await read();
    for (let wait = 0; wait < 30 && stop.state === "pointing" && stop.rings_on_target < 1; wait += 1) { await page.waitForTimeout(100); stop = await read(); } // the pointer scrolls, then measures

    assert(stop.position === `${index} / ${total}`, `position ${stop.position}`);
    if (stop.state === "pointing") assert(stop.rings_on_target >= 1, `${stop.title}: no pointer ring on ${stop.target}`);
    else { assert(stop.state === "absent" && /^Not available here: .+\.$/.test(stop.note), `${stop.title}: bad absent line "${stop.note}"`); assert(stop.rings === 0, `${stop.title}: a ring is drawn although the target is absent`); }
    summary.stops.push(stop);
    await shot(`stop-${String(index).padStart(2, "0")}-${stop.title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-${stop.state}`);
    if (index < total) await card.getByRole("button", { name: "Next", exact: true }).click();
  }
  assert(await card.getByRole("button", { name: "Next", exact: true }).isDisabled(), "Next is enabled on the last stop");
  check("next_reaches_last_stop", { stops: total, pointing: summary.stops.filter((s) => s.state === "pointing").length, absent: summary.stops.filter((s) => s.state === "absent").length });
  const byTitle = (title) => summary.stops.find((s) => s.title === title);
  const expected = summary.c1_case_study_present ? "pointing" : "absent";
  for (const title of ["Geometry node", "Feature inspector", "Compare runs"]) assert(byTitle(title).state === expected, `${title} should be ${expected} (C1 present: ${summary.c1_case_study_present}), is ${byTitle(title).state}`);
  check("c1_dependent_stops_match_data_root", { c1_case_study_present: summary.c1_case_study_present, expected, three_d_view: byTitle("3D symmetry view").state, three_d_note: byTitle("3D symmetry view").note });
  assert(byTitle("The left rail").state === "pointing" && byTitle("Design stages").state === "pointing", "the rail and stages stops should point");
  // Inside a case study the picker is replaced by a Back button, and the stop says exactly that.
  const picker = byTitle("Project picker");
  assert(picker.state === "pointing" || /case study/.test(picker.note), `unexpected picker stop: ${picker.state} ${picker.note}`);

  // Back returns to the previous stop.
  await card.getByRole("button", { name: "Back", exact: true }).click();
  await settled(total - 1);
  const back = await read();
  assert(back.title === summary.stops[total - 2].title && back.url === summary.stops[total - 2].url, `Back landed on ${back.title} ${back.url}`);
  check("back_returns_to_previous_stop", { title: back.title, url: back.url });
  await card.getByRole("button", { name: "Next", exact: true }).click();
  await settled(total);

  // Keyboard: focus sits on Next after a stop settles and Tab reaches Exit.
  await card.getByRole("button", { name: "Back", exact: true }).click();
  await settled(total - 1);
  await page.waitForFunction(() => document.activeElement?.id === "bubu-tour-next");
  await page.keyboard.press("Tab");
  assert(await page.evaluate(() => document.activeElement?.textContent?.trim()) === "Exit", "Tab from Next does not reach Exit");
  check("keyboard_reaches_next_and_exit");

  // Exit closes the tour and the pointer, back to the conversation.
  await card.getByRole("button", { name: "Exit", exact: true }).click();
  await card.waitFor({ state: "detached" });
  await page.waitForFunction(() => document.querySelectorAll(".bubu-pointer__ring").length === 0);
  await page.getByRole("dialog", { name: "Jarvis" }).waitFor();
  assert(await page.locator(".shell-project__back").count() === 0, "the tour left the case study open after Exit");
  await shot("after-exit");
  check("exit_closes_tour_and_pointer_and_restores_project");

  // Escape closes the tour from the middle.
  await page.getByRole("button", { name: "Guided tour", exact: true }).click();
  await card.waitFor();
  await settled(1);
  await card.getByRole("button", { name: "Next", exact: true }).click();
  await settled(2);
  await page.keyboard.press("Escape");
  await card.waitFor({ state: "detached" });
  await page.waitForFunction(() => document.querySelectorAll(".bubu-pointer__ring").length === 0);
  check("escape_closes_tour_and_pointer");
  touring = false;

  // The shell publishes the current surface to the read-only brief endpoint on page changes (it computes a preview and
  // stores nothing, the same allowance as the 229 walk); it is recorded separately and is not a tour request.
  const PREVIEW_POST = /^POST \/workspaces\/[^/]+\/actions\/brief$/;
  const writes = summary.tour_requests.filter((line) => !/^(GET|HEAD|OPTIONS) /.test(line) && !PREVIEW_POST.test(line));
  assert(writes.length === 0, `mutating requests during the tour: ${writes.join(", ")}`);
  check("no_post_put_delete_during_tour", { tour_requests: summary.tour_requests.length, mutating: 0,
    page_preview_posts_not_from_the_tour: summary.tour_requests.filter((line) => PREVIEW_POST.test(line)).length });
  assert(summary.page_errors.length === 0, `page errors: ${summary.page_errors.join("; ")}`);
} catch (error) {
  failure = error;
  summary.failure = String(error?.stack ?? error);
  const pages = browser?.contexts()[0]?.pages() ?? [];
  await pages[0]?.screenshot({ path: join(evidence, "failure.png") }).catch(() => {});
} finally {
  summary.finished_at = new Date().toISOString();
  summary.result = failure ? "fail" : "pass";
  await writeFile(join(evidence, "evidence.json"), JSON.stringify(summary, null, 2) + "\n");
  await browser?.close().catch(() => {});
  server.kill("SIGTERM");
  await delay(500);
  await rm(dataRoot, { recursive: true, force: true });
}
if (failure) { console.error(failure); process.exit(1); }
console.log(`bubu guided tour walk: pass (${evidence})`);
