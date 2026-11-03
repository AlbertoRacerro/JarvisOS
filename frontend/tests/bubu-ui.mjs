// BUBU-3 (UI): pointer dismiss (x buttons) and the draggable bubble (geometry + wiring).
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  BUBU_BUBBLE_POSITION_KEY, clampBubblePosition, denormalizeBubblePosition, exceedsDragThreshold, loadStoredPosition,
  normalizeBubblePosition, nudgeBubblePosition, parseStoredPosition, saveStoredPosition,
} from "../src/components/bubu/bubbleDrag.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => fs.readFileSync(path.join(root, rel), "utf8");

const vp = { width: 1440, height: 900 };
const size = { width: 360, height: 280 };

// --- geometry ---------------------------------------------------------------------------------------------------
assert.deepEqual(clampBubblePosition({ left: 100, top: 100 }, size, vp), { left: 100, top: 100 });
assert.deepEqual(clampBubblePosition({ left: -50, top: -9 }, size, vp), { left: 12, top: 12 });
assert.deepEqual(clampBubblePosition({ left: 5000, top: 5000 }, size, vp), { left: 1440 - 360 - 12, top: 900 - 280 - 12 });
assert.deepEqual(clampBubblePosition({ left: 300, top: 300 }, { width: 2000, height: 2000 }, vp), { left: 12, top: 12 }, "bigger than the viewport sticks to the margin");

const n = normalizeBubblePosition({ left: 720, top: 450 }, vp);
assert.deepEqual(n, { x: 0.5, y: 0.5 });
assert.deepEqual(denormalizeBubblePosition(n, size, vp), { left: 720, top: 450 });
assert.deepEqual(denormalizeBubblePosition({ x: 0.5, y: 0.5 }, size, { width: 800, height: 600 }), { left: 400, top: 300 }, "relative position survives a resize");
assert.deepEqual(denormalizeBubblePosition({ x: 1, y: 1 }, size, { width: 800, height: 600 }), { left: 800 - 360 - 12, top: 600 - 280 - 12 }, "a resize keeps it on screen");
assert.deepEqual(denormalizeBubblePosition({ x: -3, y: Number.NaN }, size, vp), { left: 12, top: 12 }, "garbage is clamped");
for (const point of [{ left: 12, top: 12 }, { left: 333, top: 417 }, { left: 1068, top: 608 }]) {
  assert.deepEqual(denormalizeBubblePosition(normalizeBubblePosition(point, vp), size, vp), point, "round trip");
}
assert.deepEqual(normalizeBubblePosition({ left: 10, top: 10 }, { width: 0, height: 0 }), { x: 0, y: 0 });

assert.equal(exceedsDragThreshold(3, 0), false);
assert.equal(exceedsDragThreshold(2, 2), false);
assert.equal(exceedsDragThreshold(0, 4), true);
assert.equal(exceedsDragThreshold(3, 3), true);

const mid = { left: 500, top: 400 };
assert.deepEqual(nudgeBubblePosition(mid, "ArrowLeft", false, size, vp), { left: 484, top: 400 });
assert.deepEqual(nudgeBubblePosition(mid, "ArrowRight", true, size, vp), { left: 548, top: 400 });
assert.deepEqual(nudgeBubblePosition(mid, "ArrowUp", false, size, vp), { left: 500, top: 384 });
assert.deepEqual(nudgeBubblePosition(mid, "ArrowDown", true, size, vp), { left: 500, top: 448 });
assert.deepEqual(nudgeBubblePosition({ left: 12, top: 12 }, "ArrowLeft", true, size, vp), { left: 12, top: 12 }, "stays inside");
assert.equal(nudgeBubblePosition(mid, "a", false, size, vp), null);

// --- storage ----------------------------------------------------------------------------------------------------
assert.equal(BUBU_BUBBLE_POSITION_KEY, "jarvisos:bubu-bubble-position:v1");
const memory = () => { const m = new Map(); return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v), removeItem: (k) => void m.delete(k), m }; };
const store = memory();
assert.equal(loadStoredPosition(store), null);
saveStoredPosition({ x: 0.25, y: 0.75 }, store);
assert.ok(store.m.has(BUBU_BUBBLE_POSITION_KEY));
assert.deepEqual(loadStoredPosition(store), { x: 0.25, y: 0.75 });
saveStoredPosition(null, store);
assert.equal(loadStoredPosition(store), null, "re-attach forgets the position");
for (const bad of ["", "nope", "null", "[]", "{\"x\":\"1\",\"y\":0}", "{\"x\":1}"]) assert.equal(parseStoredPosition(bad), null, bad);
assert.deepEqual(parseStoredPosition("{\"x\":2,\"y\":-1}"), { x: 1, y: 0 });
const throwing = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };
assert.equal(loadStoredPosition(throwing), null);
assert.doesNotThrow(() => saveStoredPosition({ x: 0.1, y: 0.1 }, throwing));
assert.doesNotThrow(() => loadStoredPosition(null));

assert.doesNotMatch(read("src/components/bubu/bubbleDrag.ts"), /^import /m, "bubbleDrag stays self-contained so Node tests can import it");

// --- pointer dismiss: source contracts --------------------------------------------------------------------------
const ptsx = read("src/components/bubu/BubuPointer.tsx");
const pcss = read("src/components/bubu/BubuPointer.css");
assert.match(ptsx, /onDismiss\?: \(id: string\) => void/, "optional onDismiss(id) prop");
assert.match(ptsx, /className="bubu-pointer__dismiss"[^>]*data-bubu-ui="pointer-dismiss"/, "x buttons are Bubu UI");
assert.match(ptsx, /aria-label="Dismiss"/);
assert.match(ptsx, /title=\{s\.label \? `Dismiss: \$\{s\.label\}` : "Dismiss"\}/);
assert.match(ptsx, /onClick=\{\(\) => dismiss\(s\.id\)\}/, "click dismisses that pointer");
assert.match(ptsx, /event\.key === "Escape"/, "Esc on the x dismisses");
assert.match(ptsx, /shapes\.length > 1 && onDismiss/, "Clear all only with two or more pointers");
assert.match(ptsx, /data-bubu-ui="pointer-dismiss"[^>]*onClick=\{\(\) => shapes\.forEach/, "Clear all dismisses every pointer");
assert.match(pcss, /\.bubu-pointer\s*\{[^}]*pointer-events:\s*none/, "the overlay stays pass-through");
assert.match(pcss, /\.bubu-pointer button\.bubu-pointer__dismiss\[data-bubu-ui\],[^{]*\{[^}]*pointer-events:\s*auto/, "only the buttons take input");
assert.match(pcss, /width:\s*18px;\s*height:\s*18px/);
assert.match(pcss, /:focus-visible/);
const hook = read("src/components/bubu/useBubuIntegration.tsx");
assert.match(hook, /onDismiss=\{\(id\) => setPoints\(\(current\) => current\.filter\(\(p\) => p\.id !== id\)\)\}/, "the hook drops the dismissed pointer");
assert.match(read("src/components/bubu/pageContext.ts"), /\[data-bubu-ui\]/, "data-bubu-ui elements are ignored by page context");
assert.match(read("src/components/bubu/useUserPointer.ts"), /isBubuUi\(t\)/, "and by user pointing");

// --- draggable bubble: source contracts -------------------------------------------------------------------------
const btsx = read("src/components/bubu/BubuBubble.tsx");
const bcss = read("src/components/bubu/BubuBubble.css");
assert.match(btsx, /bubu-bubble__bar/);
assert.match(btsx, /setPointerCapture\(/, "pointer capture");
assert.match(btsx, /onPointerDown=.*onPointerMove=.*onPointerUp=.*onPointerCancel=/, "pointer events (mouse, touch, pen)");
assert.match(btsx, /exceedsDragThreshold\(/);
assert.match(btsx, /loadStoredPosition\(\)/);
assert.match(btsx, /saveStoredPosition\(/);
assert.match(btsx, /aria-label="Attach to Bubu" title="Attach to Bubu"/, "re-attach control");
assert.match(btsx, /onDoubleClick=/, "double-click re-attaches");
assert.match(btsx, /event\.key === "Home"/, "Home re-attaches");
assert.match(btsx, /nudgeBubblePosition\(/, "arrow keys move the handle");
assert.match(btsx, /data-placement=\{isDetached \? undefined : layout\.placement\}/, "no tail when detached");
assert.match(btsx, /event\.key === "Escape"/, "Esc still closes the bubble");
assert.match(btsx, /placeBubble\(anchor/, "attached placement is unchanged");
assert.match(bcss, /touch-action:\s*none/);
assert.match(bcss, /cursor:\s*grab;/);
assert.match(bcss, /\[data-detached\]::after\s*\{\s*display:\s*none/);

console.log("BUBU-3 UI acceptance: PASS");
