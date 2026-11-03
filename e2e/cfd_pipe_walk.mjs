#!/usr/bin/env node
// Spec 216 acceptance 3: real-Chromium walk of the Design Phase 2 smooth-tube CFD path in Lumen Light and Dark.
// Isolated data root, built frontend served by the backend, real OpenFOAM toolchain from an operator registry,
// seeded only through public API routes (project + Process draft). Every other step is driven through the UI.
//   (cd frontend && VITE_API_BASE_URL= npm run build)
//   JARVISOS_PROOF_PYTHON=<backend venv python> node e2e/cfd_pipe_walk.mjs <evidence-dir>
// Env: JARVISOS_PROOF_PORT (8216), JARVISOS_PROOF_REGISTRY (operator registry YAML; default: build from
// JARVISOS_PROOF_CFD_ENV with e2e/cfd_operator_registry.py), JARVISOS_PROOF_RANKS (MPI ranks, 6),
// JARVISOS_PROOF_APPEARANCES (light,dark).
import { createHash } from "node:crypto";
import { copyFile, mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { openSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) { console.log('node e2e/cfd_pipe_walk.mjs [evidence-dir]  (needs built frontend, OpenFOAM toolchain env, Playwright)'); process.exit(0); }
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = process.env.JARVISOS_PROOF_PYTHON ?? join(backend, ".venv/bin/python");
const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8216);
const base = `http://127.0.0.1:${port}`;
const ranks = process.env.JARVISOS_PROOF_RANKS ?? "6";
const appearances = (process.env.JARVISOS_PROOF_APPEARANCES ?? "light,dark").split(",");
const evidence = resolve(process.argv[2] ?? await mkdtemp(join(tmpdir(), "jarvisos-216-ui-")));
await mkdir(evidence, { recursive: true });
const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-216-data-"));
const assert = (condition, message) => { if (!condition) throw new Error(message); };

// Operator-owned registry enabling the pinned OpenFOAM tools .
const registry = join(evidence, "operator_registry.yaml");
if (process.env.JARVISOS_PROOF_REGISTRY) await copyFile(process.env.JARVISOS_PROOF_REGISTRY, registry);
else {
  const cfdEnv = process.env.JARVISOS_PROOF_CFD_ENV;
  assert(cfdEnv, "set JARVISOS_PROOF_CFD_ENV (conda env with OpenFOAM) or JARVISOS_PROOF_REGISTRY");
  const built = spawnSync(python, [join(root, "e2e/cfd_operator_registry.py"), "--env", cfdEnv, "--out", registry],
    { cwd: backend, env: { ...process.env, PYTHONPATH: backend }, encoding: "utf8" });
  assert(built.status === 0, `operator registry build failed: ${built.stderr}`);
}
const env = { ...process.env, PYTHONPATH: backend, JARVISOS_DATA_ROOT: dataRoot, JARVISOS_BLUECAD_TOOL_REGISTRY: registry,
  JARVISOS_STUDIO_USER_CODE_ENABLED: "1", XDG_STATE_HOME: join(dataRoot, "host"), LOCALAPPDATA: join(dataRoot, "host") };
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

await new Promise((ok, fail) => {
  const probe = createServer().once("error", () => fail(new Error(`port ${port} is in use; refusing to seed a foreign backend`)))
    .once("listening", () => probe.close(ok)).listen(port, "127.0.0.1");
});
const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const init = spawnSync(python, ["-c", "from app.core.database import initialize_database; initialize_database()"], { cwd: backend, env, encoding: "utf8" });
assert(init.status === 0, `isolated database initialization failed: ${init.stderr}`);
const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], { cwd: backend, env, stdio: ["ignore", log, log] });

async function api(path, init = {}) {
  const response = await fetch(`${base}${path}`, { ...init, headers: { "content-type": "application/json", ...(init.headers ?? {}) } });
  const body = await response.text();
  assert(response.ok, `${init.method ?? "GET"} ${path} -> ${response.status}: ${body.slice(0, 400)}`);
  return body ? JSON.parse(body) : null;
}

// Study design: a 10 mm bore, 0.3 m tube (Process unit parameters), laminar water. Re 100 and 150 at mesh level 1 are the two
// fitted points; Re 100 at mesh levels 0, 1, 2 is the three-mesh study. Four runs per publication.
const INNER_D_M = 0.010, LENGTH_M = 0.3, WALL_FIRST = "2", WALL_CHANGED = "3";
const velocityFor = (re) => String(re * 1.002e-3 / (998.2 * INNER_D_M));
const PLAN = [{ re: 100, level: 1, point: true, study: true }, { re: 150, level: 1, point: true, study: false },
  { re: 100, level: 0, point: false, study: true }, { re: 100, level: 2, point: false, study: true }];
const RUN_TIMEOUT_MS = 40 * 60_000;

const summary = { spec: "216", acceptance: "3 real-browser walk", head, browser: null, started_at: new Date().toISOString(),
  data_root: "isolated temporary JARVISOS_DATA_ROOT", toolchain_registry: "operator_registry.yaml (OpenFOAM v2412 + gmsh pins)", mpi_ranks: Number(ranks),
  study: { inner_diameter_m: INNER_D_M, length_m: LENGTH_M, regime: "laminar", plan: PLAN, wall_mm_first: WALL_FIRST, wall_mm_changed: WALL_CHANGED },
  appearances: {}, limits: [
    "The mesh level is chosen in the Studies template form; the Mesh node displays recorded mesh status, level and cell count after a run.",
    "The study template form records the default solver bounds (20000 iterations, 1800 s wall), and the runner judges convergence only after simpleFoam ends (no early stop on residual target), so each template is re-saved through PUT cfd-study-templates with max_iterations=600 (as integrated_evidence.py does) right after the UI records it; the UI form has no control for it.",
    "Seeding (project and Process draft with the PBR unit and tube parameters) uses public API routes; everything else is driven through the UI."] };

try {
  for (let i = 0; ; i++) {
    assert(server.exitCode === null, "isolated backend exited; see backend.log");
    try { if ((await fetch(`${base}/health`)).ok) break; } catch { /* starting */ }
    assert(i < 120, "backend did not become healthy");
    await delay(500);
  }
  const browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  for (const appearance of appearances) {
    const record = { screenshots: [], console_errors: [], page_errors: [], steps: [], runs: {}, started_at: new Date().toISOString() };
    summary.appearances[appearance] = record;
    // Seed through the public Process draft owner: one Plant draft with a PBR unit carrying the 10 mm / 0.3 m tube.
    const project = await api("/workspaces", { method: "POST", body: JSON.stringify({ name: `216 CFD proof ${appearance}`, slug: `cfd-216-${appearance}-${Date.now()}` }) });
    const created = await api(`/workspaces/${project.id}/process/drafts`, { method: "POST", body: JSON.stringify({ name: "Plant" }) });
    const seeded = await api(`/workspaces/${project.id}/process/drafts/${created.draft_id}/patch`, { method: "POST", body: JSON.stringify({
      expected_revision: created.revision, ops: [
        { op: "add_unit", id: "pbr", type: "PhotobioreactorT1", tag: "PBR1", x: 160, y: 120 },
        { op: "set_unit_params", unit: "pbr", values: { tube_inner_diameter: { value: INNER_D_M, unit: "m" }, tube_length: { value: LENGTH_M, unit: "m" } } }] }) });
    record.seed = { project_id: project.id, draft_id: created.draft_id, draft_revision: seeded.revision };
    const compBase = (componentId) => `/workspaces/${project.id}/design-versions/${project.id}/components/${componentId}`;
    const apiRuns = async () => {
      const components = await api(`/workspaces/${project.id}/design-versions/${project.id}/components`);
      return components.length ? api(`${compBase(components[0].id)}/cfd-runs`) : [];
    };

    const context = await browser.newContext({ viewport: { width: 1500, height: 1100 } });
    await context.addInitScript(([key, value]) => { try { localStorage.setItem(key, value); } catch { /* ignore */ } }, ["jarvisos:appearance:v1", appearance]);
    await context.addInitScript(([key, value]) => { try { localStorage.setItem(key, value); } catch { /* ignore */ } }, ["jarvisos.active-workspace", project.id]);
    const page = await context.newPage();
    page.setDefaultTimeout(15_000);
    page.on("console", (message) => { if (message.type() === "error") record.console_errors.push(message.text()); });
    page.on("pageerror", (error) => record.page_errors.push(String(error)));
    page.on("request", (request) => { if (!request.url().startsWith(base) && !request.url().startsWith("data:") && !request.url().startsWith("blob:")) record.foreign_requests = [...(record.foreign_requests ?? []), request.url()]; });
    const shot = async (name, locator) => {
      const file = join(evidence, `${appearance}-${String(record.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
      await page.evaluate(() => { window.scrollTo(0, 0); document.scrollingElement?.scrollTo(0, 0); });
      await (locator ?? page).screenshot(locator ? { path: file } : { path: file, fullPage: true });
      record.screenshots.push({ file: file.slice(evidence.length + 1), sha256: createHash("sha256").update(await readFile(file)).digest("hex") });
    };
    const step = async (name, work) => {
      const began = Date.now();
      try {
        const detail = (await work()) ?? {};
        record.steps.push({ step: name, status: "pass", seconds: Math.round((Date.now() - began) / 1000), ...detail });
      } catch (error) {
        record.steps.push({ step: name, status: "fail", seconds: Math.round((Date.now() - began) / 1000), error: String(error).slice(0, 600) });
        await shot(`FAILED-${name.replace(/\W+/g, "-")}`).catch(() => {});
        throw error;
      }
    };

    const stages = page.getByRole("navigation", { name: "Design stages" });
    const tree = page.getByRole("tree", { name: "Component model tree" });
    const inspector = page.getByLabel("Inspector", { exact: true });
    const runsPanel = page.getByRole("region", { name: "CFD runs", exact: true });
    const gotoNode = (name) => tree.getByRole("treeitem", { name: new RegExp(`^${name}\\b`) }).click();
    const recordTemplate = async (re, level) => {
      const form = page.getByRole("form", { name: "Record a study template" });
      await form.getByLabel("Regime").selectOption("laminar");
      await form.locator("#study-velocity").fill(velocityFor(re));
      await form.locator("#study-mesh").fill(String(level));
      await page.getByText(new RegExp(`Re ${re}\\b`)).first().waitFor();
      await form.getByRole("button", { name: "Record laminar study template" }).click();
      await page.getByRole("button", { name: "Edit template" }).waitFor();
      // The form always records the contract's default solver bounds (20000 iterations; the runner judges convergence only after the
      // solver ends), which is hours of CPU per run. Re-save the same contract through the public route with the 600-iteration bound
      // the integrated evidence harness uses, then remount the panel so the UI shows the saved template.
      const componentId = (await api(`/workspaces/${project.id}/design-versions/${project.id}/components`))[0].id;
      let saved;
      for (let i = 0; i < 60; i++) {
        saved = (await api(`${compBase(componentId)}/cfd-study-templates`)).find((item) => item.regime === "laminar");
        if (saved && saved.contract.mesh_level === level && Math.abs(saved.contract.inlet.mean_velocity_m_s / Number(velocityFor(re)) - 1) < 1e-9) break;
        await delay(250);
      }
      assert(saved && saved.contract.mesh_level === level, "UI-recorded template did not reach the server");
      const components = await api(`/workspaces/${project.id}/design-versions/${project.id}/components`);
      const component = components[0];
      assert(saved.contract.solver.max_iterations === 20000, `UI template no longer carries the default solver bounds: ${JSON.stringify(saved.contract.solver)}`);
      await api(`${compBase(component.id)}/cfd-study-templates/laminar`, { method: "PUT", body: JSON.stringify({
        contract: { ...saved.contract, solver: { max_iterations: 600, residual_target: 1e-6, wall_time_limit_s: 1800 } },
        expected_revision: component.revision, expected_active_version_id: project.id }) });
      // Reload so the page holds the component's new revision, then return to the component's CFD Studies node.
      await page.reload({ waitUntil: "networkidle" });
      await stages.getByRole("button", { name: "CFD" }).click();
      await tree.getByRole("treeitem", { name: /^PBR1\b/ }).first().click();
      await page.getByRole("heading", { name: "CFD study templates" }).waitFor();
      await page.getByRole("button", { name: "Edit template" }).waitFor();
    };
    const setRanks = async () => { const input = runsPanel.getByLabel("MPI ranks"); await input.fill(ranks); await input.blur(); };
    const consoleRegion = () => page.getByRole("region", { name: "CFD run console" });
    const waitConsole = (state, timeout) => page.locator(`section[aria-label='CFD run console'].solver-console--${state}`).waitFor({ timeout });
    const waitFinished = async () => {
      await page.locator("section[aria-label='CFD run console'].solver-console--queued, section[aria-label='CFD run console'].solver-console--running").first().waitFor({ timeout: 60_000 });
      await waitConsole("finished", RUN_TIMEOUT_MS);
    };
    const startRun = async () => { await setRanks(); await runsPanel.getByRole("button", { name: "Run laminar", exact: true }).click(); };
    const apiRunFor = async (re, level, known) => {
      const list = await apiRuns();
      return list.find((run) => !known.has(run.run_id) && run.measurement?.mesh_level === level && Math.abs((run.measurement?.reynolds ?? 0) - re) < 1);
    };

    const phases = [{ name: "first", wall: WALL_FIRST }, { name: "after_geometry_change", wall: WALL_CHANGED }];
    let publishedIds = null;
    await step("plant_flowsheet_visible", async () => {
      await page.goto(`${base}/design/project`, { waitUntil: "networkidle" });
      assert(await page.evaluate(() => document.documentElement.dataset.theme) === appearance, `Lumen ${appearance} not applied`);
      await page.getByRole("heading", { name: `216 CFD proof ${appearance}` }).waitFor();
      await page.getByTestId("node-PBR1").waitFor();
      const closeSetup = page.getByRole("button", { name: "Close setup", exact: true }).filter({ hasText: "Close setup" });
      if (await closeSetup.isVisible()) await closeSetup.click();
      await shot("plant-flowsheet");
    });
    await step("tube_unit_opened_as_component", async () => {
      const unit = page.getByTestId("node-PBR1");
      await unit.click(); await page.waitForTimeout(300); await unit.dblclick();
      await page.getByRole("heading", { level: 2, name: "PBR1 · Parameters" }).waitFor();
      await shot("component-opened");
    });
    const deepen = async (wall) => {
      const input = page.locator("#design-geometry-wall");
      await input.fill(wall); await input.blur();
      await page.getByRole("button", { name: "Generate and attach geometry" }).click();
      await page.getByText("A validated BlueCad candidate is attached to this component").waitFor({ timeout: 90_000 });
      await page.getByRole("region", { name: "Component viewport" }).locator("canvas").waitFor({ timeout: 30_000 });
    };
    await step("geometry_deepened_from_process_tube", async () => {
      await gotoNode("Geometry");
      await page.getByText(/Deepen from Process: PhotobioreactorT1 → BlueCad tube/).waitFor();
      await deepen(WALL_FIRST);
      await shot("tube-geometry-attached");
    });
    await step("cfd_stage_and_mesh_node", async () => {
      await stages.getByRole("button", { name: "CFD" }).click();
      assert(await stages.getByRole("button", { name: "CFD" }).getAttribute("aria-current") === "step", "CFD stage not current");
      await page.getByRole("heading", { name: "CFD study templates" }).waitFor();
      await page.getByText(/CFD tools: pinned binaries verified/).waitFor({ timeout: 30_000 });
      await page.getByText(/Tube fluid domain: diameter 10 mm/).waitFor();
      const toolchain = await page.getByText(/CFD tools:/).first().textContent();
      await shot("cfd-stage-studies");
      await gotoNode("Mesh");
      await inspector.getByText(/No mesh has been built/).waitFor();
      const meshText = await inspector.locator("section[aria-label='Mesh details']").innerText();
      assert(/No mesh has been built/.test(meshText), `unexpected initial mesh status: ${meshText}`);
      await shot("mesh-node");
      await gotoNode("Studies");
      await page.getByRole("heading", { name: "CFD study templates" }).waitFor();
      return { toolchain, mesh_node_text: meshText.slice(0, 200) };
    });

    for (const phase of phases) {
      const known = new Set((await apiRuns()).map((run) => run.run_id));
      const ids = {};
      const tag = phase.name;
      if (phase.name === "first") {
        await step("mesh_level_chosen_in_template", async () => {
          await recordTemplate(100, 1);
          await shot("template-recorded-re100-level1");
          return { re: 100, mesh_level: 1 };
        });
        await step("run_live_residuals_and_stop", async () => {
          await startRun();
          const table = page.getByRole("table", { name: "Latest residual per field" });
          await table.waitFor({ timeout: 10 * 60_000 });
          await page.getByText("Latest residual per field (live)").waitFor();
          await page.waitForTimeout(3000);
          const live = await table.innerText();
          await shot("run-live-residuals"); await shot("run-live-residuals-console", consoleRegion());
          await consoleRegion().locator("button.solver-stop").click();
          await waitConsole("stopped", 5 * 60_000);
          await consoleRegion().getByText(/Stopped before completion/).waitFor();
          await shot("run-stopped"); await shot("run-stopped-console", consoleRegion());
          return { live_residual_rows: live.split("\n").slice(0, 8) };
        });
        await step("rerun_to_completion_after_stop", async () => {
          await startRun();
          await waitFinished();
          const text = await consoleRegion().innerText();
          assert(/residuals converged/.test(text), `run did not converge: ${text.slice(0, 300)}`);
          await shot("run-finished"); await shot("run-finished-console", consoleRegion());
          const run = await apiRunFor(100, 1, known);
          assert(run, "finished run not found through API");
          ids["100/1"] = run.run_id;
          return { run_id: run.run_id, dp_per_l_pa_m: run.measurement.pressure_gradient_pa_m, cells: run.measurement.cells };
        });
      }
      const remaining = phase.name === "first" ? PLAN.slice(1) : PLAN;
      for (const item of remaining) {
        await step(`${tag}_run_re${item.re}_level${item.level}`, async () => {
          await recordTemplate(item.re, item.level);
          await startRun();
          await waitFinished();
          const text = await consoleRegion().innerText();
          assert(/residuals converged/.test(text), `run did not converge: ${text.slice(0, 300)}`);
          const run = await apiRunFor(item.re, item.level, known);
          assert(run, "finished run not found through API");
          ids[`${item.re}/${item.level}`] = run.run_id;
          if (item.re === 100 && item.level === 0 || item.re === 100 && item.level === 2) await shot(`run-finished-re${item.re}-level${item.level}-console`, consoleRegion());
          return { run_id: run.run_id, dp_per_l_pa_m: run.measurement.pressure_gradient_pa_m, cells: run.measurement.cells };
        });
      }
      record.runs[tag] = ids;
      await step(`${tag}_fields_in_viewport`, async () => {
        await gotoNode("Results");
        const slice = page.getByRole("region", { name: "CFD field slice" });
        await slice.getByRole("img", { name: /Pressure on the axial section/ }).waitFor({ timeout: 60_000 });
        await slice.getByText(/Time .* cells/).waitFor();
        const pText = await slice.innerText();
        assert(/Measured ΔP\/L:/.test(pText) && /Wall shear from developed pipe force balance:/.test(pText),
          `viewport lacks run measurements: ${pText.slice(0, 300)}`);
        await shot("fields-pressure"); await shot("fields-pressure-viewport", page.getByRole("region", { name: "Component viewport" }));
        await slice.getByRole("button", { name: "|U|" }).click();
        await slice.getByRole("img", { name: /Velocity magnitude on the axial section/ }).waitFor();
        await shot("fields-velocity"); await shot("fields-velocity-viewport", page.getByRole("region", { name: "Component viewport" }));
        const run = (await apiRuns()).find((r) => r.run_id === ids["100/1"]);
        return { run_id: ids["100/1"], slice_text: pText.slice(-260), dp_per_l_pa_m_from_api: run.measurement.pressure_gradient_pa_m };
      });
      await step(`${tag}_publish_reduced_model`, async () => {
        await gotoNode("Studies");
        const panel = page.getByRole("region", { name: "Publish reduced model" });
        await panel.getByRole("button", { name: "Refresh runs" }).click();
        const fieldset = panel.getByRole("group", { name: "laminar runs" });
        const row = (id) => fieldset.locator("li", { has: page.locator("code", { hasText: id.slice(0, 8) }) });
        for (const item of PLAN) {
          const id = ids[`${item.re}/${item.level}`];
          if (item.point) await row(id).getByLabel("Study run", { exact: true }).check();
          if (item.study) await row(id).getByLabel("Mesh-study run").check();
        }
        await panel.locator("#publish-model-id").fill("d10_tube.darcy.laminar");
        await panel.locator("#publish-tol-laminar").fill("0.02");
        await panel.locator("#publish-tol-turbulent_rans").fill("0.1");  // the form requires it even when only laminar runs are published
        await shot("publish-selection", panel);
        const button = panel.getByRole("button", { name: "Publish reduced model" });
        await page.waitForFunction(() => !document.querySelector("form[aria-label='Publish a reduced model'] button[type=submit]")?.disabled);
        await button.click();
        await panel.getByText(/The server published the reduced model and reports it as/).waitFor({ timeout: 5 * 60_000 });
        const done = await panel.getByRole("status").first().textContent();
        await shot("published"); await shot("published-inspector", inspector);
        return { server_answer: done };
      });
      await step(`${tag}_pin_on_process_unit_with_fidelity_label`, async () => {
        await gotoNode("Reduced model");
        const models = page.getByRole("region", { name: "CFD reduced models" });
        await models.getByText("Current", { exact: true }).waitFor();
        await models.getByRole("button", { name: "Pin on Process unit" }).first().click();
        await models.getByText("Pinned on Process unit").waitFor({ timeout: 60_000 });
        const label = await models.locator(".design-project__reduced-item p").first().innerText();
        const validity = await models.getByRole("list", { name: "Validity range" }).first().innerText();
        await shot("pinned-reduced-model"); await shot("pinned-reduced-model-inspector", inspector);
        return { fidelity_label: label, validity };
      });
      if (phase.name === "first") {
        await step("geometry_change_gives_obsolescence_warning", async () => {
          await gotoNode("Geometry");
          await deepen(WALL_CHANGED);
          await gotoNode("Reduced model");
          const models = page.getByRole("region", { name: "CFD reduced models" });
          await models.getByText("Reduced model obsolete, rerun the study").first().waitFor({ timeout: 30_000 });
          const reasons = await models.getByRole("list", { name: "Reasons" }).first().innerText();
          assert(!await models.getByRole("button", { name: "Pin on Process unit" }).count(), "obsolete model still offers a pin");
          await shot("obsolescence-warning"); await shot("obsolescence-warning-inspector", inspector);
          return { reasons };
        });
        await stages.getByRole("button", { name: "CFD" }).click();
        await gotoNode("Studies");
        await page.getByText(/Tube fluid domain: diameter 10 mm/).waitFor();
      } else {
        publishedIds = ids;
      }
    }
    await step("republished_model_is_current_and_pinned", async () => {
      await gotoNode("Reduced model");
      const models = page.getByRole("region", { name: "CFD reduced models" });
      await models.getByText("Pinned on Process unit").waitFor();
      const items = await models.locator(".design-project__reduced-item > p").allInnerTexts();
      await stages.getByRole("button", { name: "Plant" }).click();
      await page.getByTestId("node-PBR1").waitFor();
      await shot("plant-after-republish");
      return { reduced_model_items: items };
    });
    record.final_reduced_models = await api(`${compBase((await api(`/workspaces/${project.id}/design-versions/${project.id}/components`))[0].id)}/reduced-models`);
    record.final_pin_in_process_draft = await api(`/workspaces/${project.id}/process/drafts/${created.draft_id}`).then((d) => JSON.stringify(d).match(/"hydraulics_model":\{[^}]*\}/)?.[0] ?? null).catch((e) => String(e));
    record.run_states = (await apiRuns()).map((r) => ({ run_id: r.run_id.slice(0, 8), state: r.state, re: r.measurement?.reynolds, mesh_level: r.measurement?.mesh_level, dp_per_l_pa_m: r.measurement?.pressure_gradient_pa_m }));
    record.finished_at = new Date().toISOString();
    assert(record.console_errors.length === 0 && record.page_errors.length === 0 && !record.foreign_requests?.length,
      `${appearance} browser errors or external requests: ${JSON.stringify({ console: record.console_errors, page: record.page_errors, foreign: record.foreign_requests })}`);
    await context.close();
  }
  await browser.close();
} finally {
  server.kill("SIGTERM");
  summary.finished_at = new Date().toISOString();
  summary.passed = Object.values(summary.appearances).length === appearances.length && Object.values(summary.appearances).every((a) => a.steps.every((s) => s.status === "pass") && a.finished_at);
  await writeFile(join(evidence, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  console.log(JSON.stringify({ evidence, passed: summary.passed, steps: Object.fromEntries(Object.entries(summary.appearances).map(([k, a]) => [k, a.steps.map((s) => `${s.status} ${s.step} ${s.seconds}s`)])) }, null, 2));
}
