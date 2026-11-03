import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const backend = resolve(root, "backend");
const panel = readFileSync(resolve(root, "frontend/src/components/process/BiologyModelLibrary.tsx"), "utf8");
const css = readFileSync(resolve(root, "frontend/src/components/process/BiologyModelLibrary.css"), "utf8");
const api = readFileSync(resolve(root, "frontend/src/api/bioModels.ts"), "utf8");
const editor = readFileSync(resolve(root, "frontend/src/stages/ProcessDraftEditor.tsx"), "utf8");
const python = execFileSync(resolve(backend, ".venv/bin/python"), ["-c", "import json; from app.modules.bio_models.forms import FORM_CARDS; print(json.dumps(FORM_CARDS))"], { cwd: backend, encoding: "utf8" });
const forms = JSON.parse(python);
const fail = (message) => { throw new Error(`169 biology model card acceptance failed: ${message}`); };
const byId = Object.fromEntries(forms.map((form) => [form.id, form]));
const tagsOf = (node) => new Set([node.tag, ...(node.children ?? []).flatMap((child) => [...tagsOf(child)])]);
const visit = (node) => {
  if (!["math", "mrow", "mi", "mn", "mo", "mtext", "msup", "msub", "mfrac", "msqrt"].includes(node.tag)) fail(`unexpected MathML node ${node.tag}`);
  for (const child of node.children ?? []) visit(child);
};
for (const form of forms) {
  visit(form.equation);
  const tags = tagsOf(form.equation);
  if (form.equation.tag !== "math" || !tags.has("mrow") || !tags.has("mi") || !tags.has("mo")) fail(`${form.id} lacks a typed MathML tree`);
  if (!Array.isArray(form.parameters) || !Array.isArray(form.inputs)) fail(`${form.id} does not separate parameters and operating inputs`);
  if (form.symbols.some((item) => item.unit === "1")) fail(`${form.id} exposes the opaque unit 1`);
}
for (const tag of ["mfrac", "msub", "msup", "mn"]) if (!tagsOf(byId["light.haldane"].equation).has(tag)) fail(`Haldane equation lacks ${tag}`);
if (byId["light.eilers_peeters_steady"].parameters.find((item) => item.meaning === "curve shape")?.key !== "beta") fail("Eilers–Peeters does not expose canonical beta key");
if (byId["nutrient.monod"].parameters[0]?.key !== "K_j" || byId["nutrient.monod"].inputs[0]?.key !== "S_j") fail("nutrient parameter/input classification is incorrect");
if (!panel.includes("activeForm.parameters.map") || panel.includes("activeForm.symbols.map")) fail("reviewed form table must show parameters and omit operating inputs");
if (!editor.includes("Biology models…")) fail("Biology model library must open from the Process draft toolbar");
if (!panel.includes("allowedMathTags") || /dangerouslySetInnerHTML|innerHTML\s*=/.test(panel)) fail("MathML must use a typed allowlist without raw HTML insertion");
if (!api.includes("locator_confirmation") || !panel.includes("sourceState") || !panel.includes("entryState")) fail("provenance action must show source and entry states and submit locator confirmation");
if (/window\.(prompt|confirm)/.test(panel)) fail("operator actions must use accessible inline controls");
if (!panel.includes("Duplicate") || !panel.includes("Evaluate preview") || !panel.includes("S_0") || !panel.includes("Q_0")) fail("model set, indexed nutrient, and evaluation preview controls are incomplete");
if (!css.includes("position: fixed") || !css.includes("overflow: auto") || !css.includes("position: sticky") || !css.includes(":focus-visible")) fail("the operator panel must stay in viewport, retain its header and feedback while scrolling, and support keyboard focus");
console.log("169 biology model cards behavioral form and operator contract: PASS");
