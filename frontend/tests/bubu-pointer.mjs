import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parsePointDirectives, readablePointText } from "../src/components/bubu/pointerDirectives.ts";
import { rankTargets, truncate } from "../src/components/bubu/pageContext.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => fs.readFileSync(path.join(root, rel), "utf8");

// --- directives -------------------------------------------------------------------------------------------------
const none = "No pointers here.  Keep   spacing.";
assert.deepEqual(parsePointDirectives(none), { text: none, points: [] });

assert.deepEqual(parsePointDirectives("Open it here [[point:design.stage.cfd]] to continue."), { text: "Open it here to continue.", points: [{ id: "design.stage.cfd" }] });
assert.deepEqual(parsePointDirectives("Use the picker [[point:design.project-picker|Project picker]]."), { text: "Use the picker.", points: [{ id: "design.project-picker", label: "Project picker" }] });
assert.deepEqual(parsePointDirectives("[[point:el-12]]\nThat button."), { text: "\nThat button.", points: [{ id: "el-12" }] });
assert.deepEqual(parsePointDirectives("Here [[point:a]] and there [[point:b|Over there]]."), { text: "Here and there.", points: [{ id: "a" }, { id: "b", label: "Over there" }] });
assert.deepEqual(parsePointDirectives("Both [[point:a]] [[point:b]] done"), { text: "Both done", points: [{ id: "a" }, { id: "b" }] });
assert.deepEqual(parsePointDirectives("See (the tab [[point:x]])"), { text: "See (the tab)", points: [{ id: "x" }] });

// whitespace tolerance inside the brackets
assert.deepEqual(parsePointDirectives("Go [[ point : rail.design | Design ]] now"), { text: "Go now", points: [{ id: "rail.design", label: "Design" }] });
assert.deepEqual(parsePointDirectives("Go [[POINT:rail.design]] now").points, [{ id: "rail.design" }]);

// unique by id (first wins; repeated directives are still removed from the text)
assert.deepEqual(parsePointDirectives("A [[point:a|One]] B [[point:a|Two]] C"), { text: "A B C", points: [{ id: "a", label: "One" }] });

// max 5 (extras are removed from the text but not returned)
const many = Array.from({ length: 8 }, (_, i) => `[[point:t${i}]]`).join(" ");
const capped = parsePointDirectives(`Look: ${many}`);
assert.equal(capped.points.length, 5);
assert.deepEqual(capped.points.map((p) => p.id), ["t0", "t1", "t2", "t3", "t4"]);
assert.ok(!capped.text.includes("[["));

// malformed directives are left as-is
for (const bad of ["[[point:]]", "[[point:a b]]", "[[point a]]", "[point:a]", "[[point:a", "[[pointer:a]]", "[[point:a|label]", "[[point:bad id|x]]"]) {
  assert.deepEqual(parsePointDirectives(`x ${bad} y`), { text: `x ${bad} y`, points: [] }, bad);
}
assert.deepEqual(parsePointDirectives("ok [[point:a]] but [[point:]] stays"), { text: "ok but [[point:]] stays", points: [{ id: "a" }] });
assert.deepEqual(parsePointDirectives("[[point:a|]]x"), { text: "x", points: [{ id: "a" }] }, "empty label -> no label");

// --- helpers ----------------------------------------------------------------------------------------------------
assert.equal(truncate("  hello \n  world  "), "hello world");
assert.equal(truncate("x".repeat(160)), "x".repeat(160));
const long = truncate("y".repeat(300));
assert.equal(long.length, 160);
assert.ok(long.endsWith("…"));
assert.equal(truncate("abcdef", 4), "abc…");
assert.equal(truncate(""), "");

const items = [
  { n: "far", stable: false, inViewport: false },
  { n: "near1", stable: false, inViewport: true },
  { n: "stable-far", stable: true, inViewport: false },
  { n: "near2", stable: false, inViewport: true },
  { n: "stable-near", stable: true, inViewport: true },
];
assert.deepEqual(rankTargets(items).map((i) => i.n), ["stable-far", "stable-near", "near1", "near2", "far"]);
assert.deepEqual(rankTargets(items, 3).map((i) => i.n), ["stable-far", "stable-near", "near1"]);
assert.equal(rankTargets(Array.from({ length: 400 }, () => ({ stable: false, inViewport: true }))).length, 150);
assert.deepEqual(rankTargets([], 5), []);

// --- source contracts -------------------------------------------------------------------------------------------
const ctx = read("src/components/bubu/pageContext.ts");
assert.match(ctx, /\[data-bubu-ui\], #bubu-launcher/, "Bubu UI is excluded from the page description");
assert.match(ctx, /isBubuUi\(el\) \|\| !isRenderable\(el\)/, "collect skips Bubu UI and hidden elements");
assert.match(ctx, /aria-hidden="true"/, "aria-hidden is excluded");
assert.match(ctx, /data-bubu-ref/, "ephemeral ids are written to the DOM");
assert.match(ctx, /MAX_TARGETS = 150/);
assert.match(ctx, /MAX_TEXT = 160/);
assert.match(ctx, /export function collectPageContext/);
assert.match(ctx, /export function resolveTarget/);
assert.doesNotMatch(ctx, /^import /m, "pageContext stays self-contained so Node tests can import it");

// --- spec 231: a directive reads as the target's name ----------------------------------------------------------
const B = (s) => `${s}`, N = (s) => `${s}`;
const page = { "design.tree.component.c1-cfd": "C1-CFD", "design.back": "Back to project", "el-3": "", "el-4": `${"x".repeat(70)}` };
const nameOf = (id) => (id in page ? page[id] : null);
assert.equal(readablePointText("Click first on [[point:design.tree.component.c1-cfd|1. C1]], then Results.", nameOf), `Click first on ${B("C1-CFD")}, then Results.`, "visible name wins");
assert.equal(readablePointText("Use [[point:design.back]].", nameOf), `Use ${B("Back to project")}.`, "unlabelled directive is named from the page");
assert.equal(readablePointText("Open [[point:gone|1. CFD]] now", nameOf), `Open ${B("1. CFD")}${N(" (not shown here)")} now`, "missing target: label plus a truthful note");
assert.equal(readablePointText("Click first on [[point:gone]].", nameOf), `Click first on ${N("an item that isn't shown here")}.`, "missing and unlabelled: no invented name");
assert.equal(readablePointText("Press [[point:el-3]].", nameOf), `Press ${B("the highlighted item")}.`, "unnamed target");
assert.equal(readablePointText("Press [[point:el-3|Run]].", nameOf), `Press ${B("Run")}.`);
assert.equal(readablePointText("See [[point:el-4|Results table]].", nameOf), `See ${B("Results table")}.`, "a long visible name yields to the label");
assert.equal(readablePointText("See [[point:el-4]].", nameOf), `See ${B(`${"x".repeat(59)}…`)}.`, "names are capped");
assert.equal(readablePointText("Go **[[point:design.back]]** or `[[point:design.back]]`", nameOf), `Go ${B("Back to project")} or ${B("Back to project")}`, "repeats render; wrappers are absorbed");
assert.equal(readablePointText("forged x [[point:]] [[point:a b]]", nameOf), "forged x [[point:]] [[point:a b]]", "malformed stays text; model marks are dropped");
assert.equal(readablePointText("A [[point:x|<img src=x onerror=alert(1)>]]", () => null), `A ${B("<img src=x onerror=alert(1)>")}${N(" (not shown here)")}`, "labels stay plain text (React escapes them)");
assert.equal(readablePointText("Use [[point:design.back|Back x]]", nameOf), `Use ${B("Back to project")}`);
assert.equal(readablePointText("Use [[point:gone|ab]]", nameOf), `Use ${B("ab")}${N(" (not shown here)")}`, "labels cannot carry marks");
assert.equal(readablePointText("plain text", nameOf), "plain text");

const dir = read("src/components/bubu/pointerDirectives.ts");
assert.doesNotMatch(dir, /^import /m);

const css = read("src/components/bubu/BubuPointer.css");
assert.match(css, /\.bubu-pointer\s*\{[^}]*pointer-events:\s*none/, "overlay never takes input");
assert.match(css, /z-index:\s*44/);
assert.match(css, /prefers-reduced-motion/);
assert.match(css, /\[data-theme="dark"\]/);

const tsx = read("src/components/bubu/BubuPointer.tsx");
assert.match(tsx, /scrollIntoView\(\{ block: "center"/, "off-screen targets are scrolled to");
assert.match(tsx, /data-bubu-ui="pointer"/);
assert.match(tsx, /createPortal\(/);
assert.match(tsx, /requestAnimationFrame/);
assert.match(tsx, /prefers-reduced-motion/);
assert.doesNotMatch(tsx, /\.click\(|dispatchEvent/, "the pointer never clicks anything");

const hook = read("src/components/bubu/useUserPointer.ts");
assert.match(hook, /addEventListener\("click", onClick, true\)/, "click is intercepted in the capture phase");
assert.match(hook, /e\.preventDefault\(\);\s*e\.stopPropagation\(\);/, "the page action does not run");
assert.match(hook, /isBubuUi\(t\)/, "Bubu UI is not intercepted");
assert.match(hook, /"Escape"/);
assert.match(hook, /removeEventListener\("click", onClick, true\)/, "listeners are cleaned up");
assert.match(hook, /box\.remove\(\)/);

console.log("BUBU-2 pointer acceptance: PASS");
