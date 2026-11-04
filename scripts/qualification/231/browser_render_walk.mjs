#!/usr/bin/env node
// Controlled-reply rendering proof against the branch frontend and the running local app data.
// Start the branch Vite server on 5173 (proxying /api to the local backend on 8000).
// Replies are injected into the existing conversation store after navigation; no model call is claimed.
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve, join } from "node:path";
import { spawnSync } from "node:child_process";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const root = resolve(new URL("../../../", import.meta.url).pathname);
const base = process.env.JARVISOS_231_BASE ?? "http://127.0.0.1:5173";
const evidence = resolve(process.argv[2] ?? "/tmp/jarvisos-231-browser");
await mkdir(evidence, { recursive: true });
const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
const result = { head, base, reply_source: "controlled conversation-store publish after visible navigation", browser: null, cases: [] };
const assert = (test, message) => { if (!test) throw new Error(message); };
const browser = await chromium.launch({ headless: true });
result.browser = `chromium ${browser.version()}`;
try {
  for (const viewport of [{ width: 1500, height: 900 }, { width: 900, height: 700 }]) {
    const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
    await context.addInitScript(() => { window.__initialStorage = [localStorage.length, sessionStorage.length]; });
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    const record = { viewport, checks: [], screenshots: [] };
    result.cases.push(record);
    const check = async (name, fn) => { await fn(); record.checks.push(name); };
    const shot = async (name) => {
      const file = join(evidence, `${viewport.width}-${name}.png`);
      await page.screenshot({ path: file });
      record.screenshots.push({ name, sha256: createHash("sha256").update(await readFile(file)).digest("hex") });
    };
    const reply = async (text, id) => page.evaluate(async ({ text, id }) => {
      const store = await import("/src/components/bubu/conversationStore.ts");
      store.publishBubuConversation({ ready: true, pending: false, error: null,
        turns: [{ id: `u${id}`, role: "user", text: "Where should I go?" }, { id, role: "jarvis", text }] });
    }, { text, id });
    try {
      await page.goto(`${base}/`, { waitUntil: "networkidle" });
      await check("clean root entry", async () => {
        assert(JSON.stringify(await page.evaluate(() => window.__initialStorage)) === "[0,0]", "browser storage was not clean");
        assert(new URL(page.url()).pathname === "/design/project", "root did not land on Design");
      });
      const picker = page.getByRole("combobox", { name: "Project version", exact: true });
      const option = picker.locator('option[value^="case:"]').filter({ hasText: /C1 · smooth tube/ });
      await option.waitFor({ state: "attached" });
      await picker.selectOption(await option.getAttribute("value"));
      await page.getByRole("heading", { level: 1, name: /case study C1 smooth tube/ }).waitFor();
      await page.getByRole("navigation", { name: "Design stages" }).getByRole("button", { name: "CAD", exact: true }).click();
      const tree = page.getByRole("tree", { name: "Component model tree" });
      const cfd = tree.getByRole("treeitem", { name: /^C1-CFD$/ });
      const horizontal = tree.getByRole("treeitem", { name: /^C1-horizontal$/ });
      await check("distinct component facts", async () => {
        await cfd.waitFor(); await horizontal.waitFor();
        const a = await cfd.getAttribute("data-jarvis-facts");
        const b = await horizontal.getAttribute("data-jarvis-facts");
        assert(a?.includes("Component") && b?.includes("Component") && a !== b, `component facts absent or identical: ${a} / ${b}`);
        record.component_facts = { cfd: a, horizontal: b };
      });
      await page.getByRole("button", { name: "Bubu — Open Jarvis" }).click();
      await reply("Click first on [[point:design.tree.component.c1-cfd|the CFD component]].", "cfd");
      await check("CFD reply and pointer", async () => {
        await page.getByRole("dialog", { name: "Jarvis" }).getByText(/Click first on/).waitFor();
        assert((await page.getByRole("dialog", { name: "Jarvis" }).innerText()).includes("C1-CFD"), "CFD name missing from reply");
        assert(!(await page.getByRole("dialog", { name: "Jarvis" }).innerText()).includes("[[point:"), "raw directive shown");
        await page.locator('.bubu-pointer [data-bubu-ui="pointer-dismiss"]').first().waitFor();
      });
      await shot("cfd");
      const dismiss = page.getByRole("button", { name: "Dismiss", exact: true }).first();
      await dismiss.focus(); await page.keyboard.press("Escape");
      await check("keyboard pointer dismissal", async () => {
        await page.locator('.bubu-pointer [data-bubu-ui="pointer-dismiss"]').first().waitFor({ state: "detached" });
      });
      await reply("Open [[point:absent.target|the missing target]] next.", "missing");
      await check("unavailable target fallback", async () => {
        const body = await page.getByRole("dialog", { name: "Jarvis" }).innerText();
        assert(body.includes("not shown here") && !body.includes("[[point:"), `missing-target fallback failed: ${body}`);
        assert(await page.locator('.bubu-pointer [data-bubu-ui="pointer-dismiss"]').count() === 0, "missing target drew a pointer");
      });
      await page.getByRole("button", { name: /^Back to / }).waitFor();
      const back = await page.evaluate(async () => {
        const { collectPageContext } = await import("/src/components/bubu/pageContext.ts");
        return collectPageContext().targets.find((item) => item.role === "button" && item.name.startsWith("Back to "))?.id;
      });
      assert(back, "return control has no point target");
      await reply(`Return with [[point:${back}|your project]].`, "back");
      await check("return reply and pointer", async () => {
        const body = await page.getByRole("dialog", { name: "Jarvis" }).innerText();
        assert(body.includes("Back to ") && !body.includes("[[point:"), `return control name missing: ${body}`);
        await page.locator('.bubu-pointer [data-bubu-ui="pointer-dismiss"]').first().waitFor();
      });
      await shot("return");
      record.status = "pass";
    } catch (error) {
      record.status = "fail"; record.error = String(error);
      await shot("failed").catch(() => {});
    }
    await context.close();
  }
} finally { await browser.close(); }
await writeFile(join(evidence, "summary.json"), JSON.stringify(result, null, 2) + "\n");
if (result.cases.some((c) => c.status !== "pass")) process.exitCode = 1;
console.log(JSON.stringify(result, null, 2));
