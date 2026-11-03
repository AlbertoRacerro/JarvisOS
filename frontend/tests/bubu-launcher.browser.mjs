// Run against an isolated Jarvis instance with fake/offline providers, never the live workspace.
// PLAYWRIGHT_MODULE may point to an existing local Playwright install; no product dependency.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

const base = process.env.BUBU_BASE_URL;
const out = process.env.BUBU_EVIDENCE_DIR;
if (!base || !out) throw new Error("Set BUBU_BASE_URL and BUBU_EVIDENCE_DIR for the isolated instance.");
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ headless: true });
const result = { base, checks: [], errors: [], writes: [], status: "running" };
const record = (name) => { result.checks.push(name); console.log(`PASS ${name}`); };
const near = (actual, expected, name) => assert.ok(Math.abs(actual - expected) < 2, `${name}: ${actual} != ${expected}`);

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.on("pageerror", (error) => result.errors.push(error.message));
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) result.writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });
  await page.goto(base + "/");
  // Ordinary first entry: create a test workspace through the visible UI if this data root is empty.
  await page.getByText("Loading workspaces…", { exact: true }).waitFor({ state: "hidden" });
  if (await page.getByRole("textbox", { name: "Workspace name", exact: true }).count()) {
    await page.getByRole("textbox", { name: "Workspace name", exact: true }).fill("Bubu launcher proof");
    await page.getByRole("button", { name: "Create workspace", exact: true }).click();
    await page.getByRole("heading", { name: "Create your first workspace" }).waitFor({ state: "hidden" });
  }
  assert.equal(await page.getByText("Workspace service unavailable", { exact: true }).count(), 0);
  const bubu = page.getByRole("button", { name: /^Bubu —/ });
  // BUBU-2: Bubu opens the speech bubble; Expand opens the full Jarvis panel (the previous Bubu click).
  const expandButton = page.getByRole("button", { name: "Expand", exact: true });
  const openPanel = async () => { await bubu.click(); await expandButton.click(); };
  const main = page.getByRole("main");
  const primary = page.getByRole("navigation", { name: "Primary navigation", exact: true });
  const box = () => bubu.boundingBox();
  const inside = async () => {
    const rect = await box(); const size = page.viewportSize();
    assert.ok(rect.x >= 0 && rect.y >= 0 && rect.x + rect.width <= size.width + 1 && rect.y + rect.height <= size.height + 1);
  };
  await bubu.waitFor();
  assert.equal(await bubu.count(), 1);
  assert.equal(await page.locator("#shell-sidecar, .shell-reopen--sidecar").count(), 0);
  const closedWidth = (await main.boundingBox()).width;
  const workArea = await page.locator(".shell-workspace").boundingBox();
  near(closedWidth, workArea.width, "closed work area width");
  assert.deepEqual(await bubu.evaluate(e => ({ background: getComputedStyle(e).backgroundColor, padding: getComputedStyle(e).padding })), { background: "rgba(0, 0, 0, 0)", padding: "0px" });
  await page.screenshot({ path: join(out, "design-closed.png") });
  record("fresh-root-entry / single launcher / full work area / transparent artwork");

  await openPanel();
  const composer = page.getByRole("textbox", { name: "Message", exact: true });
  await composer.fill("Unsent Bubu draft — keep this text");
  assert.ok((await main.boundingBox()).width < closedWidth - 200);
  assert.ok((await box()).x + (await box()).width < (await page.locator("#shell-sidecar").boundingBox()).x, "Bubu must not cover the open desktop composer");
  await page.screenshot({ path: join(out, "design-open.png") });
  await page.getByRole("button", { name: "Close Jarvis", exact: true }).click();
  await page.waitForFunction(() => document.activeElement?.id === "bubu-launcher");
  await bubu.press("Enter");
  await expandButton.click();
  assert.equal(await composer.inputValue(), "Unsent Bubu draft — keep this text");
  await composer.press("Escape");
  await bubu.press("Space");
  await expandButton.click();
  await composer.waitFor();
  await bubu.click();
  await composer.waitFor({ state: "hidden" });
  record("click / keyboard activation / close focus / unsent draft survives");

  const origin = await box();
  await page.mouse.move(origin.x + origin.width / 2, origin.y + origin.height / 2);
  await page.mouse.down();
  await page.mouse.move(origin.x - 240, origin.y - 180, { steps: 12 });
  await page.mouse.up();
  assert.equal(await page.locator("#shell-sidecar").count(), 0, "drag must not activate chat");
  const moved = await box();
  assert.ok(moved.x < origin.x - 200 && moved.y < origin.y - 150);
  await page.reload();
  await bubu.waitFor();
  const restored = await box();
  near(restored.x, moved.x, "reload x"); near(restored.y, moved.y, "reload y");
  await bubu.press("ArrowLeft");
  near((await box()).x, restored.x - 16, "keyboard move");
  await bubu.press("Home");
  near((await box()).x, origin.x, "keyboard reset");
  record("drag does not click / persisted position / keyboard move and reset");

  const beforeCancel = await box();
  await page.mouse.move(beforeCancel.x + 48, beforeCancel.y + 48);
  await page.mouse.down();
  await page.mouse.move(beforeCancel.x - 160, beforeCancel.y - 90, { steps: 6 });
  await page.keyboard.press("Escape");
  await page.mouse.up();
  near((await box()).x, beforeCancel.x, "Escape restores drag origin");
  assert.equal(await page.locator("#shell-sidecar").count(), 0);
  record("Escape cancels drag without opening chat");

  await page.getByText("More Design tools", { exact: true }).click();
  for (const name of ["Process editor", "Studio", "BlueCad workbench"]) {
    await page.getByRole("navigation", { name: "Design tools", exact: true }).getByRole("link", { name, exact: true }).click();
    await page.waitForTimeout(200);
    assert.equal(await page.locator("#shell-sidecar").count(), 0, `${name} must start closed`);
    near((await box()).x, beforeCancel.x, "route preserves position");
    await openPanel();
    await composer.waitFor();
    await bubu.click();
  }
  record("Process / Studio / BlueCad reachable through visible navigation, closed on entry");

  await primary.getByRole("link", { name: "Memory", exact: true }).click();
  await composer.waitFor();
  await openPanel();
  assert.equal(await page.locator("#jarvis-prompt").count(), 1);
  assert.equal(await page.locator("#shell-sidecar").count(), 0);
  assert.equal(await composer.evaluate(e => e === document.activeElement), true);
  record("in-page Memory chat revealed without duplication");

  await primary.getByRole("link", { name: "Settings", exact: true }).click();
  await page.getByRole("radio", { name: "Dark", exact: true }).check();
  await openPanel();
  await composer.waitFor();
  await page.getByRole("button", { name: "Responder and send target", exact: true }).click();
  await page.getByRole("menuitem", { name: "Show properties", exact: true }).click();
  await page.getByRole("button", { name: "Back to Jarvis", exact: true }).click();
  await bubu.click();
  await primary.getByRole("link", { name: "Design", exact: true }).click();
  await page.screenshot({ path: join(out, "design-dark.png") });
  record("Settings shortcut / existing Properties reachable / dark appearance");

  await page.getByRole("button", { name: "Environment…", exact: true }).click();
  await page.getByRole("dialog", { name: "Environment profiles" }).waitFor();
  assert.equal(await bubu.evaluate(e => {
    const r = e.getBoundingClientRect();
    return document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)?.closest("#bubu-launcher") !== null;
  }), false, "modal layer must remain above Bubu");
  await page.getByRole("button", { name: "Close Environment", exact: true }).click();
  record("existing modal remains above launcher");

  await page.setViewportSize({ width: 390, height: 844 });
  await inside();
  const beforeScroll = await box();
  await page.mouse.move(200, 400);
  await page.mouse.wheel(0, 500);
  await page.waitForTimeout(150);
  near((await box()).y, beforeScroll.y, "fixed during scroll");
  await page.screenshot({ path: join(out, "narrow.png") });
  await openPanel();
  await composer.waitFor();
  await composer.scrollIntoViewIfNeeded();
  await composer.fill("Narrow draft");
  await composer.press("Escape");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await inside();
  record("narrow viewport / scroll / composer / resize back");

  // A separate fresh touch context; input is delivered through Chromium's touch device.
  const touchContext = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  const touchPage = await touchContext.newPage();
  await touchPage.goto(base + "/");
  const touchBubu = touchPage.getByRole("button", { name: /^Bubu —/ });
  await touchBubu.waitFor();
  const touchOrigin = await touchBubu.boundingBox();
  const cdp = await touchContext.newCDPSession(touchPage);
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: touchOrigin.x + 48, y: touchOrigin.y + 48 }] });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: 120, y: 350 }] });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await touchPage.waitForFunction(() => document.getElementById("bubu-launcher").getBoundingClientRect().x < 150);
  assert.equal(await touchPage.locator("#shell-sidecar").count(), 0);
  await touchBubu.tap();
  await touchPage.getByRole("button", { name: "Expand", exact: true }).click();
  await touchPage.getByRole("textbox", { name: "Message", exact: true }).waitFor();
  await touchContext.close();
  record("touch drag distinct from tap");

  assert.deepEqual(result.errors, [], "no unhandled browser errors");
  // Existing read-only surface briefs/context previews use POST; neither executes a model.
  assert.ok(result.writes.every(write => write === "POST /workspaces" || write.endsWith("/context-preview") || write.endsWith("/actions/brief")), `unexpected writes: ${result.writes.join(", ")}`);
  record("no AI submission / no unhandled browser errors");
  result.status = "PASS";
} catch (error) {
  result.status = "FAIL";
  result.failure = String(error.stack ?? error);
  throw error;
} finally {
  await writeFile(join(out, "browser-result.json"), JSON.stringify(result, null, 2) + "\n");
  await browser.close();
}
