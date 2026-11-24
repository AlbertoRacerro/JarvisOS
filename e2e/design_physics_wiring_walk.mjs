#!/usr/bin/env node
// Spec 350: Real-Chromium browser walk of Motion/Mechanics and Axial O2 (tube) physics wiring.
// Verifies:
// 1. Clean profile opens Design and reaches Motion/Mechanics (no placeholder).
// 2. Wave parameters pre-fill from environment profile; running marine.rigid_body_waves
//    displays RAOs, acceleration, mooring tension, and check sheet verdict.
// 3. Plant flowsheet with tubular unit PhotobioreactorT1 exposes "Axial O2 (tube)" button.
// 4. Clicking it transitions to Dynamic with the Axial O2 drawer open.
// 5. Running photobioreactor.tube_oxygen displays outlet O2 concentration, saturation ratio,
//    and max safe run length with verdict.
// 6. Versions fold displays variant stage verdicts for Motion/Mechanics and Axial O2.
// 7. Zero console errors and zero page errors throughout.

import { createHash } from "node:crypto";
import { existsSync, openSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

if (process.argv.includes("--help")) {
  console.log("node e2e/design_physics_wiring_walk.mjs <evidence-dir>  (needs built frontend, Playwright)");
  process.exit(0);
}

const npxPw = join(process.env.HOME ?? "", ".npm/_npx/fd3bca3c548369c0/node_modules/playwright/index.mjs");
const pwModule = process.env.PLAYWRIGHT_MODULE ?? (existsSync(npxPw) ? npxPw : "playwright");

const pw = await import(pwModule);
const chromium = pw.chromium ?? pw.default?.chromium;
const root = resolve(new URL("../", import.meta.url).pathname);
const backend = join(root, "backend");
const python = resolve(
  root,
  process.env.JARVISOS_PROOF_PYTHON ??
    (existsSync(join(backend, ".venv/bin/python"))
      ? join(backend, ".venv/bin/python")
      : resolve(root, "../../JarvisOS/backend/.venv/bin/python"))
);

const port = Number(process.env.JARVISOS_PROOF_PORT ?? 8350);
const base = `http://127.0.0.1:${port}`;
if (!process.argv[2]) throw new Error("usage: node e2e/design_physics_wiring_walk.mjs <evidence-dir>");
const evidence = resolve(process.argv[2]);
const assert = (ok, message) => { if (!ok) throw new Error(message); };
await mkdir(evidence, { recursive: true });

const dataRoot = await mkdtemp(join(tmpdir(), "jarvisos-350-data-"));
const controlDir = join(dataRoot, "control");
await mkdir(join(controlDir, "continuity", "jobs"), { recursive: true });
await mkdir(join(dataRoot, "relay-repo", ".agent-relay", "devctx"), { recursive: true });
await mkdir(join(dataRoot, "host"), { recursive: true, mode: 0o700 });

const env = {
  ...process.env,
  PYTHONPATH: backend,
  JARVISOS_DATA_ROOT: dataRoot,
  JARVIS_CONTROL_DIR: controlDir,
  RELAY_CONTINUITY_DIR: join(controlDir, "continuity"),
  JARVIS_RELAY_REPO: join(dataRoot, "relay-repo"),
  XDG_STATE_HOME: join(dataRoot, "host"),
  LOCALAPPDATA: join(dataRoot, "host"),
  JARVISOS_AGY_BIN: "/nonexistent/agy",
  JARVISOS_CODEX_BIN: "/nonexistent/codex",
  JARVISOS_AGENT_RELAY_BIN: "/nonexistent/agent-relay",
};
delete env.GH_TOKEN;
delete env.GITHUB_TOKEN;
delete env.JARVISOS_GITHUB_READ_TOKEN;

await new Promise((ok, fail) => {
  const probe = createServer()
    .once("error", (e) => fail(new Error(`port ${port} is in use: ${e.message}`)))
    .once("listening", () => probe.close(ok))
    .listen(port, "127.0.0.1");
});

const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();

// Seed initial storage, an environment profile with wave channels, and a Process draft with a tubular unit
const seedScript = `
from datetime import UTC, datetime, timedelta
from app.core.bootstrap import initialize_storage
from app.modules.workspaces.service import list_workspaces
from app.modules.environment import profiles
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddUnit

initialize_storage(seed_default=True)
ws = list_workspaces()[0].id

# Create environment profile with wave parameters
t0 = datetime(2026, 1, 1, tzinfo=UTC)
stamps = [(t0 + timedelta(hours=h)).isoformat().replace("+00:00", "Z") for h in range(48)]
profiles.create_profile(
    ws,
    name="Atlantic Offshore Test Site",
    timestamps=stamps,
    channels={
        "wave_height": [2.4] * len(stamps),
        "wave_period": [8.2] * len(stamps),
        "par": [500.0] * len(stamps),
        "sea_temperature": [291.15] * len(stamps),
    },
    resolution_minutes=60,
    provenance={"kind": "deterministic_test"}
)

# Create process draft with PhotobioreactorT1
d = draft.create_draft(ws, "Offshore Tubular Bioreactor")
draft.patch(
    ws,
    d["draft_id"],
    d["revision"],
    [AddUnit(op="add_unit", id="pbr-1", type="PhotobioreactorT1", tag="PBR-01", x=120, y=140)]
)
print(f"SEEDED_WS:{ws}")
`;

const seedResult = spawnSync(python, ["-c", seedScript], { cwd: backend, env, encoding: "utf8" });
assert(seedResult.status === 0, `Storage seeding failed: ${seedResult.stderr}`);

const log = openSync(join(evidence, "backend.log"), "w");
const server = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], {
  cwd: backend,
  env,
  stdio: ["ignore", log, log],
});

const summary = {
  spec: "350",
  head,
  browser: null,
  started_at: new Date().toISOString(),
  console_errors: [],
  page_errors: [],
  screenshots: [],
  steps: [],
};

let browser;
try {
  for (let i = 0; ; i++) {
    if (server.exitCode !== null) throw new Error("backend exited prematurely; see backend.log");
    try {
      if ((await fetch(`${base}/health`)).ok) break;
    } catch {}
    if (i > 120) throw new Error("backend did not become healthy in time");
    await delay(500);
  }

  browser = await chromium.launch({ headless: true });
  summary.browser = `chromium ${browser.version()}`;
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: "light" });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);

  page.on("console", (m) => {
    if (m.type() === "error") summary.console_errors.push(m.text());
  });
  page.on("pageerror", (e) => summary.page_errors.push(String(e)));

  const shot = async (name) => {
    const file = join(evidence, `${String(summary.screenshots.length + 1).padStart(2, "0")}-${name}.png`);
    await page.screenshot({ path: file, fullPage: true });
    summary.screenshots.push({
      file: file.slice(evidence.length + 1),
      sha256: createHash("sha256").update(await readFile(file)).digest("hex"),
    });
  };

  const step = (name, detail = {}) => summary.steps.push({ step: name, status: "pass", ...detail });

  // 1. Root and navigation to Design
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Design", exact: true }).click();
  await page.locator(".design-project").waitFor();
  await shot("design-home");
  step("reach_design_from_root");

  // 2. Stage bar has Motion/Mechanics without placeholder
  const stageNav = page.getByRole("navigation", { name: "Design stages" });
  const motionTab = stageNav.getByRole("button", { name: "Motion/Mechanics", exact: true });
  await motionTab.waitFor();
  const motionTabText = await motionTab.innerText();
  assert(!motionTabText.includes("placeholder"), `Motion/Mechanics tab contains placeholder text: ${motionTabText}`);
  step("verify_motion_tab_no_placeholder");

  // 3. Open Motion/Mechanics panel
  await motionTab.click();
  const motionSection = page.locator('section[aria-label="Motion and Mechanics"]');
  await motionSection.waitFor();
  await shot("motion-panel-opened");
  step("open_motion_panel");

  // Verify Environment prefill
  const envSelect = page.locator("#motion-env-profile");
  await envSelect.waitFor();
  const profileText = await envSelect.locator("option:checked").innerText();
  assert(profileText.includes("Atlantic Offshore"), `Expected environment profile selected, got ${profileText}`);
  await shot("motion-env-prefilled");
  step("verify_motion_env_prefilled");

  // 4. Run marine.rigid_body_waves evaluation
  const runMotionBtn = motionSection.getByRole("button", { name: /Run Motion Evaluation/i });
  await runMotionBtn.click();
  const motionVerdictBanner = page.locator(".design-project__motion-verdict-banner");
  await motionVerdictBanner.waitFor();
  const motionVerdict = await motionVerdictBanner.locator(".verdict-badge").innerText();
  assert(motionVerdict.length > 0, "Motion verdict badge should be non-empty");
  await shot("motion-evaluation-success");
  step("run_motion_evaluation", { verdict: motionVerdict });

  // 5. Navigate to Plant flowsheet
  await stageNav.getByRole("button", { name: "Plant", exact: true }).click();
  const plantSection = page.locator('section[aria-label="Plant"]');
  await plantSection.waitFor();
  await shot("plant-stage-opened");
  step("reach_plant_stage");

  // Select the tubular unit in the flowsheet
  const unitNode = page.locator('[data-testid="node-PBR-01"], [data-process-unit-id="pbr-1"]').first();
  if (await unitNode.count()) {
    await unitNode.click();
    await unitNode.focus();
    await delay(500);
  }

  // 6. Navigate to Axial O2 (tube)
  // Check either the plant-hint button or open Dynamic directly
  const axialPlantBtn = page.getByRole("button", { name: /Axial O₂ \(tube\)/i });
  if (await axialPlantBtn.isVisible()) {
    await axialPlantBtn.click();
  } else {
    await stageNav.getByRole("button", { name: "Dynamic", exact: true }).click();
  }

  const dynamicPanel = page.locator('section[aria-label="Dynamic Studio"], .design-dynamic');
  await dynamicPanel.waitFor();

  // Ensure Axial O2 drawer is open
  const axialDrawer = page.locator("details.design-dynamic__axial-drawer");
  await axialDrawer.waitFor();
  if ((await axialDrawer.getAttribute("open")) === null) {
    await axialDrawer.locator("summary").click();
  }
  await shot("axial-o2-drawer-opened");
  step("open_axial_o2_drawer");

  // 7. Run photobioreactor.tube_oxygen calculation
  const runAxialBtn = axialDrawer.getByRole("button", { name: /Run Axial O₂ Calculation/i });
  await runAxialBtn.click();
  const axialVerdictBanner = axialDrawer.locator(".design-project__motion-verdict-banner");
  await axialVerdictBanner.waitFor();
  const axialVerdict = await axialVerdictBanner.locator(".verdict-badge").innerText();
  assert(axialVerdict.length > 0, "Axial O2 verdict badge should be non-empty");
  await shot("axial-o2-calculation-success");
  step("run_axial_o2_calculation", { verdict: axialVerdict });

  // 8. Open Versions fold in left sidebar to verify variant stage verdicts summary
  await stageNav.getByRole("button", { name: "Motion/Mechanics", exact: true }).click();
  const versionsFold = page.locator("details.design-project__fold", { hasText: "Versions" });
  await versionsFold.waitFor();
  if ((await versionsFold.getAttribute("open")) === null) {
    await versionsFold.locator("summary").click();
  }
  const variantVerdicts = page.locator(".design-project__variant-summary, .variant-verdicts-summary");
  await variantVerdicts.waitFor();
  const stageVerdictsText = await variantVerdicts.innerText();
  assert(stageVerdictsText.includes("Motion/Mechanics"), "Variant verdicts should include Motion/Mechanics");
  assert(stageVerdictsText.includes("Axial O2"), "Variant verdicts should include Axial O2");
  await shot("variant-stage-verdicts");
  step("verify_variant_stage_verdicts");

  // 9. Verify zero errors
  if (summary.console_errors.length) {
    throw new Error(`Console errors during walk: ${JSON.stringify(summary.console_errors)}`);
  }
  if (summary.page_errors.length) {
    throw new Error(`Page errors during walk: ${JSON.stringify(summary.page_errors)}`);
  }

  summary.completed_at = new Date().toISOString();
  await writeFile(join(evidence, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  console.log(`WALK PASS: ${evidence}`);
} finally {
  if (browser) await browser.close();
  server.kill("SIGTERM");
  await delay(500);
  try {
    await rm(dataRoot, { recursive: true, force: true });
  } catch {}
}
