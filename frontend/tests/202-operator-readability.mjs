import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = path => readFileSync(resolve(root, path), "utf8");
const require = createRequire(import.meta.url);

// Render the actual header and AppLink, not a string fixture or a browser mock.
function component(path, imports = {}) {
  const module = { exports: {} };
  const code = ts.transpileModule(read(path), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
  }).outputText;
  new Function("require", "module", "exports", code)(
    name => imports[name] ?? require(name), module, module.exports
  );
  return module.exports.default;
}
const AppLink = component("src/app/AppLink.tsx");
const Header = component("src/components/fusion/FinalWorkspaceHeader.tsx", {
  "../../app/AppLink": { default: AppLink }
});
for (const [group, active, title, hrefs] of [
  ["memory", "models", "Project knowledge", ["/memory/project-basis", "/memory/models", "/memory/literature", "/review"]],
  ["development", "roadmap", "Development", ["/development/roadmap/timeline"]],
  ["coding", "runtime", "Coding", ["/coding/repository", "/coding/runtime"]]
]) {
  const html = renderToStaticMarkup(React.createElement(Header, { group, active, navigate() {} }));
  assert.ok(html.includes(`<h1>${title}</h1>`));
  assert.equal((html.match(/aria-current="page"/g) ?? []).length, 1);
  assert.equal((html.match(/<a /g) ?? []).length, hrefs.length);
  for (const href of hrefs) assert.ok(html.includes(`href="${href}"`));
  assert.ok(!html.includes("operator workspace") && !html.includes("<p>"));
}

const foundation = read("src/styles/foundation.css");
assert.match(foundation, /:root\s*\{[^}]*font-size: 100%/);
assert.match(foundation, /body\s*\{[^}]*font-size: var\(--font-size-body\)/);
assert.match(foundation, /font-variant-numeric: tabular-nums/);
const headerCss = read("src/styles/final-workspace-header.css");
const navCss = read("src/styles/100g-ui-repair.css");
assert.ok(!headerCss.includes('"description tabs"'));
for (const css of [headerCss, navCss]) {
  assert.match(css, /font-size: var\(--font-size-label\)/);
  assert.match(css, /flex-wrap: wrap/);
  assert.match(css, /var\(--color-bg-surface-raised\)/);
  assert.match(css, /var\(--color-text-primary\)/);
  assert.ok(!/#[a-f\d]{3,8}\b/i.test(css), "Shared navigation must consume theme roles");
}
const studio = read("src/pages/Studio.tsx");
for (const redundant of ["Create, test and publish reusable", "Choose a path.", "Blank contract. Declare", "Start wizard"]) assert.ok(!studio.includes(redundant));
for (const essential of ["scientifically unqualified", "Studio execution unavailable:", "STEADY_LIMITS", "GEOMETRY_NOTICE", "Save Draft", "Stale:", "Superseded:"]) assert.ok(studio.includes(essential));
assert.ok(!read("src/components/fusion/FinalSettingsSurface.tsx").includes("Configure visual preferences"));

function luminance(hex) {
  const [r, g, b] = hex.slice(1).match(/../g).map(x => parseInt(x, 16) / 255)
    .map(x => x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a, b) {
  const values = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (values[0] + 0.05) / (values[1] + 0.05);
}
const tokens = read("src/styles/tokens.css");
for (const theme of ["light", "dark"]) {
  const block = tokens.match(new RegExp(`\\[data-theme="${theme}"\\]\\s*\\{([^}]+)\\}`))[1];
  const value = name => block.match(new RegExp(`${name}:\\s*(#[a-f\\d]{6})`, "i"))[1];
  for (const text of ["primary", "secondary", "muted"]) {
    for (const surface of ["canvas", "shell", "surface", "surface-raised", "subtle"]) {
      const ratio = contrast(value(`--color-text-${text}`), value(`--color-bg-${surface}`));
      assert.ok(ratio >= 4.5, `${theme} ${text}/${surface}: ${ratio.toFixed(2)} < 4.5`);
    }
  }
}
console.log("202 rendered header, preserved controls, typography and semantic-token contrast: PASS (not browser proof)");
