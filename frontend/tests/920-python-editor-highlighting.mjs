import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const fail = (message) => { console.error(`920 Python editor highlighting failed: ${message}`); process.exit(1); };
const editor = read("src/components/studio/PythonEditor.tsx");
const theme = read("src/components/studio/vscodeDarkPlus.ts");
const css = read("src/pages/studio.css");
// The Workshop Python pane is highlighted like VS Code Dark+, dark in both app themes.
for (const needle of ["...vscodeDarkPlus", "bracketMatching()", "indentOnInput()", "indentGuides", 'indentUnit.of("    ")']) if (!editor.includes(needle)) fail(`editor missing ${needle}`);
for (const needle of ["HighlightStyle.define", "t.controlKeyword", "t.definitionKeyword", "t.function(t.variableName)", "t.string", "t.comment", "t.number", "cm-py-constant", "dark: true"]) if (!theme.includes(needle)) fail(`theme missing ${needle}`);
for (const needle of ["cm-matchingBracket", "cm-indent-guide", "cm-linked-line", "line-height:1.5", "--code-bg:#1e1e1e"]) if (!css.includes(needle)) fail(`studio.css missing ${needle}`);
if (/\[data-theme="(light|dark)"\] \.studio-code-editor\{/.test(css)) fail("the code pane must not switch palette with the app theme");
const luminance = (hex) => { const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((x) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4)); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; };
const ratio = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
const palette = theme.slice(theme.indexOf("export const darkPlus"), theme.indexOf("};"));
const colours = [...palette.matchAll(/(\w+): "(#[0-9a-f]{6})"/g)];
if (colours.length < 8) fail("token palette not found");
for (const [, token, hex] of colours) if (ratio(hex, "#1e1e1e") < 4.5) fail(`${token} ${hex} contrast below 4.5 on #1e1e1e`);
console.log("920 Python editor highlighting: PASS");
