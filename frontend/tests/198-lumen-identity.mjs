// Spec 198 STUDIO-VISUAL-DESIGN-1 deterministic acceptance (slice 1: tokens, appearance, shell, BlueCad spelling).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const failures = [];
const check = (condition, message) => { if (!condition) failures.push(message); };

const tokens = read("src/styles/tokens.css");
const lumen = read("src/styles/lumen.css");
const main = read("src/main.tsx");
const theme = read("src/theme.ts");

// Lumen is the single identity, selected only by the existing appearance preference.
const light = tokens.split('[data-theme="light"] {')[1]?.split('[data-theme="dark"] {')[0] ?? "";
const dark = tokens.split('[data-theme="dark"] {')[1]?.split("@supports")[0] ?? "";
for (const token of ["--lumen-canvas-base", "--lumen-field-light", "--lumen-panel-fill", "--lumen-rail-bg", "--lumen-emblem", "--lumen-shadow-panel", "--color-chart-bg", "--color-flow-bg", "--color-selection"]) {
  check(light.includes(`${token}:`), `Lumen Light is missing ${token}`);
  check(dark.includes(`${token}:`), `Lumen Dark is missing ${token}`);
}
// Spec 213 moved Lumen Light from yellow-green toward a cooler pine/fern family.
check(light.includes("--color-bg-canvas: #e8f1e9"), "Lumen Light canvas drifted");
check(dark.includes("--color-bg-canvas: #050b06"), "Lumen Dark canvas drifted");
check(tokens.includes("--lumen-accent-fill: var(--accent-seed)"), "lit accent fill needs a plain-seed fallback");
check(/--lumen-accent-fill: radial-gradient\([^;]*var\(--accent-seed\)/.test(tokens), "lit accent fill must derive from the user accent seed");
check(!/\[data-jarvis-(theme|family)/.test(tokens + lumen), "exploration direction selectors must not ship");
check(!fs.existsSync(path.join(root, "src/visualDirection.ts")) && !main.includes("visualDirection"), "the ?direction= exploration switcher must not ship");
check(!/jarvisos:visual-direction/.test(main + theme), "exploration storage key must not ship");
check(!/!important/.test(lumen), "the Lumen composition must not escalate with !important; restyle the owning rule instead");

// Lumen composition is imported last so the identity wins over the earlier layered sheets.
const imports = [...main.matchAll(/import "\.\/styles\/([^"]+)";/g)].map((match) => match[1]);
check(imports.at(-1) === "lumen.css", "lumen.css must be the last production stylesheet");

// Accent preference keeps working and keeps AA text contrast.
check(theme.includes('DEFAULT_ACCENT_HEX = "#8DB943"'), "Lumen chlorophyll must be the default accent seed");
check(theme.includes("mixHex(normalized, target"), "accent text must shade the seed toward AA contrast, not jump to black/white");

// CSS must not re-uppercase product names in eyebrows.
check(/\.eyebrow \{[^}]*text-transform: none/.test(read("src/styles/foundation.css")), "eyebrows must keep authored case so BlueCad is not rendered as BLUECAD");

// Bundled, licensed, local assets only.
for (const relative of ["public/fonts/BricolageGrotesque-latin.woff2", "public/fonts/OFL-BricolageGrotesque.txt", "public/brand/emblem-detailed.svg", "public/brand/emblem-detailed-dark.svg"]) {
  check(fs.existsSync(path.join(root, relative)) && fs.statSync(path.join(root, relative)).size > 0, `${relative} missing`);
}
check(read("public/fonts/OFL-BricolageGrotesque.txt").includes("SIL OPEN FONT LICENSE"), "Bricolage Grotesque licence text missing");
check(read("public/fonts/README.md").includes("BricolageGrotesque-latin.woff2"), "Bricolage Grotesque provenance missing");
for (const svg of ["public/brand/emblem-detailed.svg", "public/brand/emblem-detailed-dark.svg"]) {
  const body = read(svg);
  check(!/(href|src)=["']?https?:/i.test(body) && !/url\(["']?https?:/i.test(body), `${svg} must not reference remote resources`);
  check(!/exploration|not approved/i.test(body), `${svg} still carries exploration labelling`);
}
check(!/url\(["']?https?:\/\//i.test(tokens + lumen), "Lumen must not add network egress");

// Product text spells BlueCad. Identifiers (BLUECAD_*), API paths and comments are out of scope.
const sourceFiles = [];
const walk = (dir) => {
  for (const entry of fs.readdirSync(path.join(root, dir), { withFileTypes: true })) {
    const relative = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(relative);
    else if (/\.(tsx|ts)$/.test(entry.name) && !/Harness\.ts$/.test(entry.name)) sourceFiles.push(relative);
  }
};
walk("src");
for (const relative of sourceFiles) {
  read(relative).split("\n").forEach((line, index) => {
    const code = line.replace(/\/\/.*$/, "");
    // The Sidecar converts a backend-owned context summary at display time.
    if (relative === "src/components/ai/useJarvisSidecar.tsx" && code.includes('return text.replace(/\\bBLUECAD\\b/g, "BlueCad")')) return;
    if (/(?<![A-Z_])BLUECAD(?![A-Z_])/.test(code)) failures.push(`${relative}:${index + 1} renders BLUECAD instead of BlueCad`);
  });
}

if (failures.length) {
  console.error("STUDIO-VISUAL-DESIGN-1 Lumen acceptance failed:\n- " + failures.join("\n- "));
  process.exit(1);
}
console.log("STUDIO-VISUAL-DESIGN-1 Lumen acceptance: PASS");
