import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { resolveTrustedFixture } from "./fixture-registry.mjs";
import { checkJsonContract, inputEmptyResult, jsonPointer, loadTrustedPlan } from "./plan-lib.mjs";
import { isMutatingSameOriginRequest } from "./request-policy.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const planId = process.env.PROOF_PLAN_ID;
const baseUrl = process.env.PROOF_BASE_URL ?? "http://127.0.0.1:8000";
const artifactDir = process.env.PROOF_ARTIFACT_DIR;
const expectedHead = process.env.PROOF_EXPECTED_HEAD_SHA;
const resolvedHead = process.env.PROOF_RESOLVED_HEAD_SHA;
const checkedOutHead = process.env.PROOF_CHECKED_OUT_HEAD_SHA;
const controllerSha = process.env.PROOF_CONTROLLER_SHA;
const prBaseSha = process.env.PROOF_PR_BASE_SHA;
const repository = process.env.PROOF_REPOSITORY;
const prNumber = process.env.PROOF_PR_NUMBER;
const runId = process.env.GITHUB_RUN_ID ?? "unknown";
const proofPython = process.env.PROOF_PYTHON;
const candidateUser = process.env.PROOF_CANDIDATE_USER;

if (!planId || !artifactDir || !expectedHead || !resolvedHead || !checkedOutHead || !controllerSha || !prBaseSha || !repository) throw new Error("missing required proof identity environment");
if (expectedHead !== resolvedHead || expectedHead !== checkedOutHead) throw new Error("exact-head identity mismatch before browser execution");

const plan = await loadTrustedPlan(planId, join(here, "plans"));
const readOnlySameOriginPostPaths = new Set(plan.readOnlySameOriginPostPaths ?? []);
const artifactMode = plan.artifactMode ?? "full";
await mkdir(artifactDir, { recursive: true });
const startedAt = new Date().toISOString();
const assertions = [];
const artifacts = [];
const captures = new Map();
const record = (name, pass, detail) => { assertions.push({ name, pass, detail }); if (!pass) throw new Error(`${name}: ${detail}`); };
const ASSERTION_POLL_TIMEOUT_MS = 5_000;
const ASSERTION_POLL_INTERVAL_MS = 50;

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext(plan.viewport ? { viewport: plan.viewport } : {});
const traceEnabled = artifactMode === "full";
if (traceEnabled) await context.tracing.start({ screenshots: true, snapshots: true, sources: false });
const page = await context.newPage();
const proofOrigin = new URL(baseUrl).origin;
const mutatingBrowserRequests = [];
const consoleErrors = [];
context.on("request", (request) => {
  const url = new URL(request.url());
  if (url.origin === proofOrigin && isMutatingSameOriginRequest(request.method(), url.pathname, readOnlySameOriginPostPaths)) {
    mutatingBrowserRequests.push({ method: request.method(), path: url.pathname });
  }
});
page.on("pageerror", (error) => assertions.push({ name: "pageerror", pass: false, detail: artifactMode === "metadata-only" ? "browser page error" : String(error) }));
page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });

const regexFromTrusted = (value) => new RegExp(value);
const locatorFromSpec = (spec, root = page) => {
  if (spec.kind === "role") return root.getByRole(spec.role, { name: spec.regex ? regexFromTrusted(spec.name) : spec.name, exact: spec.exact ?? false });
  if (spec.kind === "text") return root.getByText(spec.regex ? regexFromTrusted(spec.text) : spec.text, { exact: spec.exact ?? false });
  if (spec.kind === "label") return root.getByLabel(spec.regex ? regexFromTrusted(spec.text) : spec.text, { exact: spec.exact ?? false });
  if (spec.kind === "testid") return root.getByTestId(spec.testid);
  if (spec.kind === "css") return root.locator(spec.selector);
  if (spec.kind === "within") { const parent = locatorFromSpec(spec.parent, root); const child = locatorFromSpec(spec.child, parent); return spec.first ? child.first() : child; }
  throw new Error(`unsupported locator kind ${spec.kind}`);
};

const openTechnicalDetails = async (details) => {
  const summary = details.locator(":scope > summary").filter({ hasText: /^Technical details$/ });
  if ((await summary.count()) !== 1) throw new Error("Technical details requires exactly one native summary control");
  await summary.waitFor({ state: "visible" }); await summary.focus();
  if (!(await summary.evaluate((node) => node === document.activeElement))) throw new Error("Technical details summary did not receive keyboard focus");
  const initiallyOpen = await details.evaluate((node) => node.open); await summary.press("Enter");
  const afterEnter = await details.evaluate((node) => node.open); if (afterEnter === initiallyOpen) throw new Error("Technical details did not toggle with Enter");
  await summary.press("Space"); const afterSpace = await details.evaluate((node) => node.open); if (afterSpace === afterEnter) throw new Error("Technical details did not toggle with Space");
  if (!afterSpace) { await summary.press("Enter"); if (!(await details.evaluate((node) => node.open))) throw new Error("Technical details did not finish open"); }
};

const valueOf = (ref) => {
  if ("capture" in ref) { if (!captures.has(ref.capture)) throw new Error(`missing capture ${ref.capture}`); return captures.get(ref.capture); }
  if ("env" in ref) return ref.env === "expectedHead" ? expectedHead : repository;
  if ("literal" in ref) return ref.literal;
  if ("json" in ref) { if (!captures.has(ref.json.source)) throw new Error(`missing JSON source ${ref.json.source}`); const value = jsonPointer(captures.get(ref.json.source), ref.json.pointer); return ref.json.length ? (Array.isArray(value) || typeof value === "string" ? value.length : (() => { throw new Error("length requested for non-sized JSON value"); })()) : value; }
  throw new Error("unsupported value reference");
};

const template = (input) => input.replace(/{{([^}]+)}}/g, (_match, token) => {
  if (token.startsWith("capture:")) { const name = token.slice("capture:".length); if (!captures.has(name)) throw new Error(`missing capture ${name}`); return String(captures.get(name)); }
  if (token.startsWith("env:")) { const raw = token.slice("env:".length); const [name, filter = null] = raw.split("|"); let value = name === "expectedHead" ? expectedHead : name === "repository" ? repository : (() => { throw new Error(`unsupported env token ${name}`); })(); if (filter === "urlencode") value = encodeURIComponent(String(value)); else if (filter !== null) throw new Error(`unsupported env template filter ${filter}`); return String(value); }
  if (token.startsWith("json:")) { const rest = token.slice("json:".length); const firstColon = rest.indexOf(":"); const source = rest.slice(0, firstColon); let pointerAndFilter = rest.slice(firstColon + 1); let filter = null; const pipe = pointerAndFilter.lastIndexOf("|"); if (pipe > -1) { filter = pointerAndFilter.slice(pipe + 1); pointerAndFilter = pointerAndFilter.slice(0, pipe); } if (!captures.has(source)) throw new Error(`missing JSON source ${source}`); let value = jsonPointer(captures.get(source), pointerAndFilter); if (filter === "length") { if (!Array.isArray(value) && typeof value !== "string") throw new Error("length template requested for non-sized value"); value = value.length; } else if (filter === "urlencode") value = encodeURIComponent(String(value)); else if (filter !== null) throw new Error(`unsupported template filter ${filter}`); return String(value); }
  throw new Error(`unsupported template token ${token}`);
});

const screenshot = async (name) => {
  if (artifactMode !== "full") throw new Error("browser screenshot disabled by trusted artifact policy");
  const path = join(artifactDir, `${planId}-${name}.png`);
  await page.screenshot({ path, fullPage: true });
  artifacts.push(path);
};
const runFixture = (fixture, phase) => {
  if (!proofPython || !candidateUser) throw new Error("fixture requires bounded unprivileged Python identity");
  const script = resolveTrustedFixture(fixture, phase, join(here, "fixtures"));
  const seedEnv = ["GITHUB_TOKEN=", "GH_TOKEN=", `JARVISOS_DATA_ROOT=${process.env.JARVISOS_DATA_ROOT ?? ""}`, `PYTHONPATH=${process.env.PYTHONPATH ?? ""}`];
  const seeded = spawnSync("sudo", ["-u", candidateUser, "-H", "env", ...seedEnv, proofPython, script, phase], { encoding: "utf8" });
  return { pass: seeded.status === 0, detail: seeded.stderr || seeded.stdout || `status=${seeded.status}` };
};
const stepName = (step, index) => step.name ?? `${planId}:${String(index + 1).padStart(3, "0")}:${step.op}`;

async function execute(step, index) {
  const name = stepName(step, index);
  if (step.op === "navigate") { const route = template(step.route); const response = await page.goto(`${baseUrl}${route}`, { waitUntil: "networkidle", timeout: 30_000 }); record(`${name}:http`, Boolean(response) && response.status() < 500, `status=${response?.status() ?? "none"}`); record(`${name}:path`, new URL(page.url()).pathname === new URL(`${baseUrl}${route}`).pathname, `url=${page.url()}`); }
  else if (step.op === "reload") { await page.reload({ waitUntil: "networkidle", timeout: 30_000 }); record(name, true, "page reloaded"); }
  else if (step.op === "assert-visible") { await locatorFromSpec(step.locator).waitFor({ state: "visible" }); record(name, true, "locator visible"); }
  else if (step.op === "assert-count") { const locator = locatorFromSpec(step.locator); const deadline = Date.now() + ASSERTION_POLL_TIMEOUT_MS; let count = await locator.count(); while (count !== step.equals && Date.now() < deadline) { await page.waitForTimeout(ASSERTION_POLL_INTERVAL_MS); count = await locator.count(); } record(name, count === step.equals, `count=${count} expected=${step.equals}`); }
  else if (step.op === "click") { await locatorFromSpec(step.locator).click(); record(name, true, "clicked trusted locator"); }
  else if (step.op === "fill") {
    const locator = locatorFromSpec(step.locator);
    if (await locator.evaluate((node) => node.tagName === "SELECT")) {
      const options = await locator.locator("option").evaluateAll((nodes) => nodes.map((node) => ({ value: node.value, label: node.label })));
      const match = options.find((option) => option.value === step.value) ?? options.find((option) => option.label === step.value);
      if (!match) throw new Error(`${name}: select option ${JSON.stringify(step.value)} is unavailable`);
      await locator.selectOption(match.value);
    } else await locator.fill(step.value);
    record(name, true, "filled trusted locator");
  }
  else if (step.op === "open-technical-details") { await openTechnicalDetails(locatorFromSpec(step.locator)); record(name, true, "keyboard disclosure verified and left open"); }
  else if (step.op === "assert-attribute") { const value = await locatorFromSpec(step.locator).getAttribute(step.attribute); record(name, value === step.equals, `${step.attribute}=${JSON.stringify(value)} expected=${JSON.stringify(step.equals)}`); }
  else if (step.op === "assert-input-empty") { const locator = locatorFromSpec(step.locator); await locator.waitFor({ state: "visible" }); const result = inputEmptyResult(await locator.inputValue()); record(name, result.pass, result.detail); }
  else if (step.op === "same-origin-get") { const path = template(step.path); const response = await context.request.get(`${baseUrl}${path}`); record(`${name}:http`, response.ok(), `status=${response.status()}`); captures.set(step.capture, await response.json()); record(name, true, `captured JSON as ${step.capture}`); }
  else if (step.op === "capture-text") { const locator = locatorFromSpec(step.locator); await locator.waitFor({ state: "visible" }); let value = (await locator.innerText()).trim(); if (step.stripPrefix !== undefined) { if (!value.startsWith(step.stripPrefix)) throw new Error(`${name}: expected prefix ${step.stripPrefix}`); value = value.slice(step.stripPrefix.length).trim(); } captures.set(step.capture, value); record(name, true, `captured text as ${step.capture}`); }
  else if (step.op === "capture-attribute") { const locator = locatorFromSpec(step.locator); await locator.waitFor({ state: "visible" }); const value = await locator.getAttribute(step.attribute); captures.set(step.capture, value); record(name, true, `captured ${step.attribute} as ${step.capture}`); }
  else if (step.op === "capture-json-find") { if (!captures.has(step.source)) throw new Error(`${name}: missing source ${step.source}`); const array = jsonPointer(captures.get(step.source), step.arrayPointer); if (!Array.isArray(array)) throw new Error(`${name}: target is not an array`); const matches = array.filter((item) => item && typeof item === "object" && item[step.field] === step.equals); record(name, matches.length === 1, `matches=${matches.length} field=${step.field} equals=${step.equals}`); captures.set(step.capture, matches[0]); }
  else if (step.op === "capture-mutating-request-count") { captures.set(step.capture, mutatingBrowserRequests.length); record(name, true, `captured mutating request count=${mutatingBrowserRequests.length} as ${step.capture}`); }
  else if (step.op === "assert-value-equals") { const left = valueOf(step.left); const right = valueOf(step.right); record(name, JSON.stringify(left) === JSON.stringify(right), `left=${JSON.stringify(left)} right=${JSON.stringify(right)}`); }
  else if (step.op === "map-value") { const source = String(valueOf(step.source)); if (!(source in step.cases)) throw new Error(`${name}: unmapped value ${source}`); captures.set(step.capture, step.cases[source]); record(name, true, `mapped ${source} as ${step.capture}`); }
  else if (step.op === "assert-text-template") { const expected = template(step.template); const locator = locatorFromSpec(step.locator); await locator.waitFor({ state: "visible" }); const actual = (await locator.innerText()).trim(); record(name, actual === expected, `actual=${JSON.stringify(actual)} expected=${JSON.stringify(expected)}`); }
  else if (step.op === "assert-template-in") { const value = template(step.template); record(name, step.allowed.includes(value), `value=${JSON.stringify(value)} allowed=${JSON.stringify(step.allowed)}`); }
  else if (step.op === "assert-text-template-map") { const source = String(valueOf(step.source)); if (!(source in step.cases)) throw new Error(`${name}: unmapped value ${source}`); const expected = template(step.cases[source]); const locator = locatorFromSpec(step.locator); await locator.waitFor({ state: "visible" }); const actual = (await locator.innerText()).trim(); record(name, actual === expected, `actual=${JSON.stringify(actual)} expected=${JSON.stringify(expected)}`); }
  else if (step.op === "assert-json-deep-equals") { const locator = locatorFromSpec(step.locator); await locator.waitFor({ state: "visible" }); const rendered = JSON.parse(await locator.innerText()); const expected = valueOf(step.right); record(name, JSON.stringify(rendered) === JSON.stringify(expected), `rendered=${JSON.stringify(rendered)} expected=${JSON.stringify(expected)}`); }
  else if (step.op === "assert-json-contract") { if (!captures.has(step.source)) throw new Error(`${name}: missing source ${step.source}`); const value = jsonPointer(captures.get(step.source), step.pointer); const result = checkJsonContract(value, step.contract); record(name, result.pass, result.detail); }
  else if (step.op === "assert-body-absent") { let body = await page.locator("body").innerText(); let forbidden = step.forbidden; if (step.caseInsensitive) { body = body.toLowerCase(); forbidden = forbidden.map((item) => item.toLowerCase()); } record(name, forbidden.every((item) => !body.includes(item)), `forbidden=${JSON.stringify(step.forbidden)}`); }
  else if (step.op === "assert-no-button-label") { const forbiddenPattern = new RegExp(step.pattern, step.caseInsensitive ? "i" : ""); const matches = await page.getByRole("button", { name: forbiddenPattern }).count(); record(name, matches === 0, `matched ${matches} forbidden accessible button name(s)`); }
  else if (step.op === "assert-all-attributes-in") { const locator = locatorFromSpec(step.locator); const count = await locator.count(); const values = await locator.evaluateAll((nodes, attribute) => nodes.map((node) => node.getAttribute(attribute)), step.attribute); record(name, (step.count === undefined || count === step.count) && values.every((value) => step.allowed.includes(value)), `count=${count} values=${JSON.stringify(values)}`); }
  else if (step.op === "run-fixture") { const result = runFixture(step.fixture, step.phase); record(name, result.pass, result.detail); }
  else if (step.op === "screenshot") { await screenshot(step.file); record(name, true, `saved ${step.file}`); }
  else throw new Error(`unsupported operation ${step.op}`);
}

let verdict = "PASS"; let failure = null;
try {
  for (const [index, step] of plan.steps.entries()) await execute(step, index);
}
catch (error) { verdict = "FAIL"; failure = String(error?.stack ?? error); }

let teardownFailure = null;
if (traceEnabled) {
  const trace = join(artifactDir, `${planId}-trace.zip`);
  try {
    await context.tracing.stop({ path: trace });
    artifacts.push(trace);
  }
  catch (error) { teardownFailure = `trace teardown failed: ${String(error?.stack ?? error)}`; }
}
try { await browser.close(); }
catch (error) { teardownFailure = teardownFailure ?? `browser teardown failed: ${String(error?.stack ?? error)}`; }

const remainingConsoleErrors = [...consoleErrors];
for (const [index, expected] of (plan.expectedConsoleErrors ?? []).entries()) {
  const observed = remainingConsoleErrors.filter((text) => text === expected.text).length;
  const pass = observed === expected.count;
  assertions.push({
    name: `console-error-expected-${index}`,
    pass,
    detail: artifactMode === "metadata-only" ? `observed=${observed} expected=${expected.count}` : `text=${JSON.stringify(expected.text)} observed=${observed} expected=${expected.count}`,
  });
  for (let i = remainingConsoleErrors.length - 1; i >= 0; i -= 1) if (remainingConsoleErrors[i] === expected.text) remainingConsoleErrors.splice(i, 1);
}
for (const text of remainingConsoleErrors) {
  assertions.push({ name: "console-error-unexpected", pass: false, detail: artifactMode === "metadata-only" ? "unexpected browser console error" : text });
}

if (plan.forbidMutatingRequests) {
  const pass = mutatingBrowserRequests.length === 0;
  const detail = pass ? "no same-origin mutating browser requests observed through teardown" : `observed=${JSON.stringify(mutatingBrowserRequests)}`;
  assertions.push({ name: "browser-mutating-requests", pass, detail });
  if (!pass && verdict === "PASS") { verdict = "FAIL"; failure = `browser-mutating-requests: ${detail}`; }
}
if (teardownFailure && verdict === "PASS") { verdict = "FAIL"; failure = teardownFailure; }
else if (teardownFailure && failure === null) failure = teardownFailure;

const failedAssertions = assertions.filter((item) => item.pass === false);
if (verdict === "PASS" && failedAssertions.length > 0) { verdict = "FAIL"; failure = failure ?? `browser emitted ${failedAssertions.length} failed asynchronous assertion(s)`; }
const backendLog = join(artifactDir, "backend.log");
if (artifactMode === "metadata-only") await rm(backendLog, { force:true });
else try { await readFile(backendLog); artifacts.push(backendLog); } catch (error) { if (error?.code !== "ENOENT") throw error; }
const digests = {};
for (const path of artifacts) digests[path.split("/").at(-1)] = createHash("sha256").update(await readFile(path)).digest("hex");
const manifest = { schema:"jarvisos.exact-head-browser-proof.v1", repository, pr_number:prNumber ? Number(prNumber) : null, pr_base_sha:prBaseSha, expected_head_sha:expectedHead, resolved_pr_head_sha:resolvedHead, checked_out_head_sha:checkedOutHead, controller_sha:controllerSha, workflow_run_id:runId, plan_id:planId, artifact_mode:artifactMode, browser:"chromium", playwright_version:"1.55.0", started_at:startedAt, ended_at:new Date().toISOString(), assertions, artifacts:digests, verdict, failure };
await writeFile(join(artifactDir, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
if (verdict !== "PASS" || failedAssertions.length > 0) process.exitCode = 1;
