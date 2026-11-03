// Manual real-browser regression against an existing C1 Example; does not create or run a solver.
// node frontend/tests/222-plant-context.browser.mjs FRONTEND_URL WORKSPACE_ID SOURCE_DRAFT_ID OUTPUT_DIR
// Optional PLAYWRIGHT_MODULE points to an already installed Playwright module.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const [base, workspace, sourceDraft, destination] = process.argv.slice(2);
assert(base && workspace && sourceDraft && destination, "frontend, workspace, source draft and output directory required");
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "/home/user/jarvis-control/work/tools/pw/node_modules/playwright/index.mjs");
const out = resolve(destination);
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ headless: true });
const result = { base, workspace, source_draft: sourceDraft, started_at: new Date().toISOString(), checks: [] };
try {
  for (const appearance of ["light", "dark"]) {
    for (const missing of [false, true]) {
      const context = await browser.newContext({ viewport: { width: 1500, height: 1100 } });
      await context.addInitScript(([id, theme]) => {
        localStorage.setItem("jarvisos.active-workspace", id);
        localStorage.setItem("jarvisos:appearance:v1", theme);
      }, [workspace, appearance]);
      const check = { appearance, missing_source: missing, passed: false, page_errors: [] };
      result.checks.push(check);
      const page = await context.newPage();
      page.setDefaultTimeout(20000);
      page.on("pageerror", error => check.page_errors.push(String(error)));
      await page.route(`**/workspaces/${workspace}/process/drafts`, async route => {
        if (route.request().method() !== "GET") return route.continue();
        const response = await route.fetch();
        const rows = await response.json();
        assert(rows.some(row => row.draft_id === sourceDraft), "fixture source draft must exist");
        const others = rows.filter(row => row.draft_id !== sourceDraft);
        assert(others.length, "fixture must also contain an unrelated draft");
        const ordered = missing ? others : [...others, rows.find(row => row.draft_id === sourceDraft)];
        check.returned_draft_ids = ordered.map(row => row.draft_id);
        await route.fulfill({ response, json: ordered });
      });
      await page.goto(`${base}/design/project`, { waitUntil: "networkidle" });
      await page.getByRole("navigation", { name: "Design stages" }).getByRole("button", { name: "Plant", exact: true }).click();
      if (missing) {
        await page.getByRole("alert").filter({ hasText: "The Process draft referenced by this Design version is unavailable." }).waitFor().catch(async error => {await writeFile(`${out}/missing-body.txt`,await page.locator("body").innerText());await page.screenshot({path:`${out}/missing-failure.png`,fullPage:true});throw error;});
        assert.equal(await page.locator("[data-testid^='node-']").count(), 0, "missing source must not display an unrelated default");
        assert.equal(await page.getByRole("button", { name: "Create process draft", exact: true }).count(), 0, "missing referenced draft must not offer creation of an unrelated replacement");
      } else {
        await page.locator("[data-testid='node-C1-CFD']").waitFor();
        await page.locator("[data-testid='node-C1-horizontal']").waitFor();
        assert.equal(await page.locator("[data-testid='node-Feed']").count(), 0, "unrelated default draft must not win list order");
      }
      assert.deepEqual(check.page_errors, []);
      await page.screenshot({ path: `${out}/${appearance}-${missing ? "missing" : "bound"}.png`, fullPage: true });
      check.passed = true;
      await context.close();
    }
  }
} finally {
  await browser.close();
  await writeFile(`${out}/summary.json`, JSON.stringify(result, null, 2) + "\n");
}
console.log(JSON.stringify(result, null, 2));
