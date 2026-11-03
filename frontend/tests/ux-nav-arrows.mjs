// Back/Forward arrows in the rail and the Design view in the URL: the in-app history position (navHistory.ts) against
// a scripted browser history, the Design view round trip (designView.ts), and the wiring that makes both reachable.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const require = createRequire(import.meta.url);

function load(path, imports = {}) {
  const module = { exports: {} };
  const code = ts.transpileModule(read(path), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 }
  }).outputText.replace(/import\.meta\.env/g, "({ DEV: false })");
  new Function("require", "module", "exports", code)((name) => imports[name] ?? require(name), module, module.exports);
  return module.exports;
}

// ---- A scripted browser tab: one history stack that survives "reloads" (a fresh module instance) ----
function createTab({ storage = true } = {}) {
  const entries = [{ state: null, url: "/design/project" }];
  let at = 0;
  const listeners = new Map();
  const store = new Map();
  const fire = (type) => (listeners.get(type) ?? []).forEach((listener) => listener({ type, state: entries[at].state }));
  const window = {
    history: {
      get state() { return entries[at].state; },
      pushState(state, _title, url) { entries.splice(at + 1); entries.push({ state: structuredClone(state), url }); at += 1; },
      replaceState(state, _title, url) { entries[at] = { state: structuredClone(state), url }; },
      back() { if (at > 0) { at -= 1; fire("popstate"); } },
      forward() { if (at < entries.length - 1) { at += 1; fire("popstate"); } }
    },
    location: { get href() { return `http://localhost${entries[at].url}`; } },
    get sessionStorage() {
      if (!storage) throw new Error("SecurityError: storage is disabled");
      return { getItem: (key) => store.get(key) ?? null, setItem: (key, value) => store.set(key, String(value)) };
    },
    addEventListener(type, listener) { listeners.set(type, [...(listeners.get(type) ?? []), listener]); },
    removeEventListener(type, listener) { listeners.set(type, (listeners.get(type) ?? []).filter((item) => item !== listener)); },
    dispatchEvent(event) { fire(event.type); return true; }
  };
  return { window, entries, url: () => entries[at].url, rawPush(url) { entries.splice(at + 1); entries.push({ state: null, url }); at += 1; fire("popstate"); } };
}
const loadHistory = (tab) => { globalThis.window = tab.window; return load("src/app/navHistory.ts"); };
const position = (history) => { const { canGoBack, canGoForward } = history.readHistoryPosition(); return [canGoBack, canGoForward]; };

{
  const tab = createTab();
  let history = loadHistory(tab);
  assert.deepEqual(position(history), [false, false], "a fresh tab has nothing to go back or forward to");
  assert.equal(tab.entries[0].state.jarvisNavIndex, 0, "the first entry is stamped in place");
  history.pushHistoryEntry({}, "/design/project?stage=cad&component=c1&node=geometry");
  assert.deepEqual(position(history), [true, false], "opening a component from Plant makes Back available");
  history.pushHistoryEntry({}, "/design/project?stage=cfd&component=c1&node=studies");
  tab.window.history.back();
  assert.equal(tab.url(), "/design/project?stage=cad&component=c1&node=geometry");
  assert.deepEqual(position(history), [true, true], "after Back, Forward is available");
  tab.window.history.back();
  assert.deepEqual(position(history), [false, true], "back on Plant, only Forward remains");
  history.replaceHistoryEntry({}, "/design/project");
  assert.equal(tab.entries[0].state.jarvisNavIndex, 0, "a replaced entry keeps its position");
  assert.deepEqual(position(history), [false, true]);
  tab.window.history.forward();
  history.pushHistoryEntry({ jarvisCodingRepository: { path: "src", file: null } }, "/coding/repository");
  assert.deepEqual(position(history), [true, false], "a new entry drops whatever was ahead");
  assert.deepEqual(tab.entries.at(-1).state.jarvisCodingRepository, { path: "src", file: null }, "page state rides along unchanged");
  tab.rawPush("/coding/repository#app-main");
  assert.deepEqual(position(history), [true, false]);
  assert.equal(tab.entries.at(-1).state.jarvisNavIndex, 3, "an entry the app did not push (a fragment link) follows the last one seen");
  tab.window.history.back(); tab.window.history.back();
  history = loadHistory(tab); // reload: module state is gone, history.state and sessionStorage remain
  assert.deepEqual(position(history), [true, true], "a reload keeps Back and Forward");
}
{
  const tab = createTab({ storage: false });
  let history = loadHistory(tab);
  assert.deepEqual(position(history), [false, false], "without storage the arrows still work");
  history.pushHistoryEntry({}, "/memory/models");
  history.pushHistoryEntry({}, "/runs");
  tab.window.history.back();
  assert.deepEqual(position(history), [true, true]);
  history = loadHistory(tab);
  assert.deepEqual(position(history), [true, false], "after a storage-less reload Forward is unknown until seen");
  tab.window.history.forward();
  assert.deepEqual(position(history), [true, false]);
  tab.window.history.back();
  assert.deepEqual(position(history), [true, true], "an entry seen again restores Forward");
}
delete globalThis.window;

// ---- The Design view round trip ----
const targets = load("src/app/designTargets.ts");
const tree = load("src/components/design/DesignComponentTree.tsx", { "../../app/designTargets": targets });
const view = load("src/pages/designView.ts", { "../components/design/DesignComponentTree": tree });
assert.deepEqual(view.readDesignView(""), { stage: "Plant", componentId: null, node: "Parameters" }, "the bare route is Plant");
assert.equal(view.designViewHref(view.readDesignView(""), ""), "/design/project");
for (const stage of view.STAGES) {
  for (const node of tree.DESIGN_NODES) {
    const href = view.designViewHref({ stage, componentId: "c-1", node }, "");
    assert.deepEqual(view.readDesignView(new URL(href, "http://localhost").search), { stage, componentId: "c-1", node }, `${href} restores its view`);
  }
}
assert.equal(view.designViewHref({ stage: "CAD", componentId: "c-1", node: "Geometry" }, ""), "/design/project?stage=cad&component=c-1&node=geometry");
assert.equal(view.designViewHref({ stage: "Reduced model", componentId: "c-1", node: "Reduced model" }, ""), "/design/project?stage=reduced-model&component=c-1&node=reduced-model");
assert.deepEqual(view.readDesignView("?stage=cfd&component=c-1"), { stage: "CFD", componentId: "c-1", node: "Results" }, "a component without a node opens on the stage's node");
assert.deepEqual(view.readDesignView("?stage=cfd&component=c-1&node=studies"), { stage: "CFD", componentId: "c-1", node: "Results" }, "an old Studies link opens the merged Results node");
assert.deepEqual(view.readDesignView("?stage=warp&node=mesh"), { stage: "Plant", componentId: null, node: "Parameters" }, "malformed values fall back; a node needs a component");
assert.equal(view.readDesignView(`?component=${"x".repeat(201)}`).componentId, null, "an overlong component id is ignored");
assert.equal(view.designViewHref({ stage: "Plant", componentId: null, node: "Parameters" }, "?recordKind=decision&stage=cad&component=c-1&node=mesh"), "/design/project?recordKind=decision",
  "other query parameters are kept and stale view parameters dropped");
// Dynamic carries its chosen Process draft; other stages drop it, so it never sticks to a later view.
const dynamicHref = view.designViewHref({ stage: "Dynamic", componentId: null, node: "Parameters", draftId: "d-7" }, "");
assert.equal(dynamicHref, "/design/project?stage=dynamic&draft=d-7");
assert.deepEqual(view.readDesignView("?stage=dynamic&draft=d-7"), { stage: "Dynamic", componentId: null, node: "Parameters", draftId: "d-7" }, `${dynamicHref} restores its draft`);
assert.deepEqual(view.readDesignView("?stage=cad&draft=d-7"), { stage: "CAD", componentId: null, node: "Geometry" }, "a draft outside Dynamic is ignored");
assert.equal(view.designViewHref({ stage: "CAD", componentId: null, node: "Geometry", draftId: "d-7" }, "?stage=dynamic&draft=d-7"), "/design/project?stage=cad");
assert.equal(view.readDesignView(`?stage=dynamic&draft=${"x".repeat(201)}`).draftId, undefined, "an overlong draft id is ignored");

// ---- Wiring ----
const router = read("src/app/useAppRouter.ts");
assert.ok(!/window\.history\.(push|replace)State/.test(router) && router.includes("pushHistoryEntry({}, target)"), "the router pushes through the positioned history");
assert.ok(read("src/pages/CodingWorkbench.tsx").includes("pushHistoryEntry({ ...state, jarvisCodingRepository: navigation }"), "the repository browser pushes through it too");
const rail = read("src/components/shell/Rail.tsx");
for (const [label, call] of [["Back", "window.history.back()"], ["Forward", "window.history.forward()"]]) {
  assert.ok(rail.includes(`aria-label="${label}" title="${label}"`) && rail.includes(call), `the rail carries ${label}`);
}
assert.ok(rail.indexOf("<HistoryArrows />") < rail.indexOf("shell-rail__identity"), "the arrows sit at the top of the rail, the same place on every page");
const design = read("src/pages/DesignProject.tsx");
assert.ok(design.includes("const view = readDesignView(search);") && read("src/App.tsx").includes("search={search}"), "the Design view is read from the URL");
assert.ok(design.includes("showView({ stage: target, componentId: component.id, node: STAGE_NODE[target] });"), "opening a unit from Plant is a new entry, so Back returns to Plant");
assert.match(read("src/styles/shell.css"), /\.shell-history button\.shell-history__button \{/);

assert.match(read("package.json"), /npm run test:nav/);
console.log("ux nav arrows ok");
