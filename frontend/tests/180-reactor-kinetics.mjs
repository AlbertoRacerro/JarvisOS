import assert from "node:assert/strict";
import fs from "node:fs";
import {
  buildReaction, compilationState, emptyForm, equationTree, formFromReaction, reactionBody, reactionFindings, reactorFindings,
  serverFieldErrors, toRangeUnit, validateForm,
} from "../src/components/process/kineticsLogic.ts";
import { actionSummaryText, describeProcessAction } from "../src/components/ai/workspaceActionPresentation.ts";

const read = (path) => fs.readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
const editor = read("stages/ProcessDraftEditor.tsx");
const kineticsTsx = read("components/process/ReactorKinetics.tsx");
const resultsTsx = read("components/process/ReactorResults.tsx");
const css = read("components/process/ReactorKinetics.css");

const registry = {
  version: "1", unsupported: ["vapour or two-phase reactor contents"],
  forms: [
    { id: "power_law_arrhenius", label: "Power law (Arrhenius)", explanation: "x", parameters: [], equation: { tag: "math" } },
    { id: "monod", label: "Monod", explanation: "m", equation: { tag: "math" }, parameters: [
      { key: "v_max", symbol: "V_max", kind: "reaction_rate", domain: "≥ 0", minimum: 0, maximum: 1e6, unit: "kmol/[m3.h]" },
      { key: "k_s", symbol: "K_S", kind: "molar_concentration", domain: "> 0", minimum: 1e-12, maximum: 1e6, unit: "kmol/m3" }] },
    { id: "haldane", label: "Haldane / Andrews", explanation: "h", equation: { tag: "math" }, parameters: [
      { key: "v_max", symbol: "V_max", kind: "reaction_rate", domain: "≥ 0", minimum: 0, maximum: 1e6, unit: "kmol/[m3.h]" },
      { key: "k_s", symbol: "K_S", kind: "molar_concentration", domain: "> 0", minimum: 1e-12, maximum: 1e6, unit: "kmol/m3" },
      { key: "k_i", symbol: "K_I", kind: "molar_concentration", domain: "> 0", minimum: 1e-12, maximum: 1e6, unit: "kmol/m3" }] },
  ],
  inhibition: { max_terms: 3, kinds: ["noncompetitive", "competitive"], explanation: "i", k_i: { symbol: "K_i", kind: "molar_concentration", domain: "> 0", minimum: 1e-12, maximum: 1e6, unit: "kmol/m3" } },
  temperature: { explanation: "t",
    activation_energy: { symbol: "E_a", kind: "molar_energy", domain: "≥ 0", minimum: 0, maximum: 3e5, unit: "J/mol" },
    reference_temperature: { symbol: "T_ref", kind: "temperature", domain: "> 0", minimum: 200, maximum: 1000, unit: "K" } },
  provenance_kinds: ["literature", "measurement", "operator_estimate", "synthetic"],
};
const compounds = ["Water", "Ethylene Oxide", "Ethylene Glycol"];

// ---- units
assert.equal(toRangeUnit(1, "mol/m3"), 1e-3);
assert.equal(toRangeUnit(2, "mmol/L"), 2e-3);
assert.equal(toRangeUnit(5, "kJ/mol"), 5000);
assert.equal(toRangeUnit(25, "degC"), 298.15);
assert.equal(toRangeUnit(1, "mol/[m3.s]"), 3.6);
assert.ok(Number.isNaN(toRangeUnit(1, "furlong")));

// ---- equation trees use only allowlisted tags and grow with options
const tags = (node, out = new Set()) => { out.add(node.tag); (node.children ?? []).forEach((child) => tags(child, out)); return out; };
const allowed = new Set(["math", "mrow", "mi", "mn", "mo", "mtext", "msup", "msub", "mfrac", "msqrt"]);
for (const form of ["power_law_arrhenius", "monod", "haldane"]) for (const tag of tags(equationTree(form, [{ kind: "competitive" }, { kind: "noncompetitive" }], true))) assert.ok(allowed.has(tag), tag);
assert.ok(JSON.stringify(equationTree("monod", [], true)).includes("T_ref"));
assert.ok(JSON.stringify(equationTree("haldane")).includes("K_I"));
assert.ok(JSON.stringify(equationTree("monod", [{ kind: "noncompetitive" }])).includes("K_i1"));

// ---- validation: K_S = 0 gives the inline domain error; literature needs a citation
const form = emptyForm({}, "monod");
assert.equal(form.id, "r1");
Object.assign(form, { name: "EO to EG", rows: [{ compound: "Ethylene Oxide", coeff: "-1" }, { compound: "Water", coeff: "-1" }, { compound: "Ethylene Glycol", coeff: "1" }], base: "Ethylene Oxide" });
form.q.v_max = { text: "5", unit: "kmol/[m3.h]" };
form.q.k_s = { text: "0", unit: "kmol/m3" };
let errors = validateForm(form, registry, compounds);
assert.match(errors["rate_law.k_s"], /greater than zero/);
form.q.k_s = { text: "2", unit: "kmol/m3" };
assert.deepEqual(validateForm(form, registry, compounds), {});
form.q.k_s = { text: "2e9", unit: "mol/m3" };
assert.match(validateForm(form, registry, compounds)["rate_law.k_s"], /within/, "range is checked after unit conversion");
form.q.k_s = { text: "2", unit: "kmol/m3" };
form.provenance = "literature";
assert.match(validateForm(form, registry, compounds)["provenance.citation"], /citation/);
form.citation = "Smith 2020";
form.base = "Water";
form.provenance = "synthetic";
form.inhibitions = [{ kind: "competitive", inhibitor: "Water", k_i: { text: "4", unit: "kmol/m3" } }];
assert.ok(validateForm(form, registry, compounds)["rate_law.inhibitions.0.inhibitor"], "inhibitor must differ from the substrate");
form.inhibitions[0].inhibitor = "Ethylene Glycol";
form.base = "Ethylene Oxide";
assert.deepEqual(validateForm(form, registry, compounds), {});
form.useTemperature = true;
form.q.activation = { text: "50", unit: "kJ/mol" };
form.q.reference = { text: "25", unit: "degC" };
assert.deepEqual(validateForm(form, registry, compounds), {});
form.q.tMin = { text: "350", unit: "K" }; form.q.tMax = { text: "300", unit: "K" };
assert.match(validateForm(form, registry, compounds).tMax, /exceed/);
form.q.tMin = { text: "", unit: "K" }; form.q.tMax = { text: "", unit: "K" };

// ---- built DraftOp body: typed law is Liquid, no Arrhenius constants, quantities are value+unit only
const body = buildReaction(form);
assert.equal(body.phase, "Liquid");
assert.equal(body.A_forward, undefined);
assert.equal(body.orders, undefined);
assert.equal(body.rate_law.form, "monod");
assert.equal(body.rate_law.substrate, "Ethylene Oxide");
assert.deepEqual(body.rate_law.k_s, { value: 2, unit: "kmol/m3" });
assert.deepEqual(body.rate_law.temperature.activation_energy, { value: 50, unit: "kJ/mol" });
assert.deepEqual(body.stoichiometry, { "Ethylene Oxide": -1, Water: -1, "Ethylene Glycol": 1 });
assert.equal(body.validity, undefined);
assert.equal(body.provenance.kind, "synthetic");
assert.ok(!JSON.stringify(body).includes("\"si\""));

const power = { ...emptyForm({}, "power_law_arrhenius"), name: "Legacy", rows: form.rows, base: "Ethylene Oxide" };
power.q.A = { text: "1e5", unit: "kmol/[m3.h]" }; power.q.E = { text: "60", unit: "kJ/mol" }; power.orders = { "Ethylene Oxide": "1", Water: "" };
assert.deepEqual(validateForm(power, registry, compounds), {});
const legacy = buildReaction(power);
assert.equal(legacy.rate_law, undefined);
assert.equal(legacy.provenance, undefined);
assert.deepEqual(legacy.orders, { "Ethylene Oxide": 1 });
assert.deepEqual(legacy.A_forward, { value: 1e5, unit: "kmol/[m3.h]" });

// ---- stored reaction round trip; assigning an existing reaction strips the SI mirror
const stored = { name: "EO to EG", stoichiometry: { "Ethylene Oxide": -1, Water: -1, "Ethylene Glycol": 1 }, base_reactant: "Ethylene Oxide", phase: "Liquid", basis: "MolarConc",
  rate_law: { form: "haldane", substrate: "Ethylene Oxide", v_max: { value: 5, unit: "kmol/[m3.h]", si: 1.38 }, k_s: { value: 2, unit: "kmol/m3", si: 2000 }, k_i: { value: 10, unit: "kmol/m3", si: 1e4 }, inhibitions: [] },
  provenance: { kind: "literature", citation: "X" } };
const back = formFromReaction("r7", stored, { r7: stored });
assert.equal(back.kind, "haldane");
assert.equal(back.existing, true);
assert.equal(back.q.k_i.text, "10");
assert.deepEqual(validateForm(back, registry, compounds), {});
assert.ok(!JSON.stringify(reactionBody(stored)).includes("\"si\""));
assert.equal(reactionBody(stored).orders, undefined);

// ---- findings land on the right card and input
const findings = [
  { severity: "blocker", code: "KINETICS_PARAMETER_DOMAIN", object: "CSTR-1", field: "reactions.r1.rate_law.k_s", message: "K_S bad", source: "jarvis" },
  { severity: "blocker", code: "KINETICS_PROVENANCE_MISSING", object: "CSTR-1", field: "reactions.r1.provenance.citation", message: "cite", source: "jarvis" },
  { severity: "info", code: "KINETICS_PARAMETER_UNVERIFIED", object: "CSTR-1", field: "reactions", message: "estimate", source: "jarvis" },
  { severity: "blocker", code: "REACTOR_ENERGY_STREAM_MISSING", object: "CSTR-1", field: "energy_inlets", message: "energy", source: "jarvis" },
  { severity: "blocker", code: "KINETICS_PARAMETER_DOMAIN", object: "CSTR-2", field: "reactions.r1.rate_law.k_s", message: "other", source: "jarvis" },
];
assert.equal(reactionFindings(findings, "CSTR-1", "r1").length, 2);
assert.deepEqual(reactorFindings(findings, "CSTR-1").map((item) => item.code), ["KINETICS_PARAMETER_UNVERIFIED", "REACTOR_ENERGY_STREAM_MISSING"]);
assert.deepEqual(serverFieldErrors(findings, "CSTR-1", "r1"), { "rate_law.k_s": "K_S bad", "provenance.citation": "cite" });

// ---- compilation state
const ok = { verified: true };
assert.equal(compilationState(ok, { state: "current" }, true).tone, "ok");
assert.equal(compilationState(ok, { state: "stale" }, true).tone, "stale");
assert.equal(compilationState({ verified: false, code: "KINETICS_TWO_PHASE_UNSUPPORTED" }, { state: "current" }, true).tone, "failed");
assert.match(compilationState({ verified: false, code: "KINETICS_TWO_PHASE_UNSUPPORTED" }, { state: "current" }, true).text, /vapour/);
assert.equal(compilationState(undefined, { state: "none" }, true).tone, "pending");
assert.equal(compilationState(undefined, { state: "none" }, false).tone, "failed");

// ---- agent presentation: plain words, no raw JSON or form ids
assert.equal(describeProcessAction({ op: "set_reaction", unit: "CSTR-1", reaction_id: "r1", reaction: { name: "EO to EG", rate_law: { form: "monod" } } }), "Set Monod reaction EO to EG on CSTR-1");
assert.equal(describeProcessAction({ op: "set_reaction", unit: "PFR-1", reaction_id: "r1", reaction: { name: "Slow", rate_law: { form: "haldane" } } }), "Set Haldane / Andrews reaction Slow on PFR-1");
assert.equal(describeProcessAction({ op: "set_reaction", unit: "PFR-1", reaction_id: "r1", reaction: { name: "Old" } }), "Set Power law (Arrhenius) reaction Old on PFR-1");
assert.equal(describeProcessAction({ op: "set_reaction", unit: "CSTR-1", reaction_id: "r1", reaction: null }), "Remove a reaction from CSTR-1");
assert.equal(actionSummaryText({ summary: "", request: { actions: [{ op: "set_reaction", unit: "CSTR-1", reaction_id: "r1", reaction: { name: "A", rate_law: { form: "monod" } } }] } }), "Set Monod reaction A on CSTR-1");

// ---- source wiring
assert.match(editor, /role="tablist" aria-label="Reactor sections"/);
assert.match(editor, /aria-controls=\{`\$\{reactorTabBase\}-panel-\$\{index\}`\}/);
assert.match(editor, /event\.key === "Home"/);
assert.match(editor, /CSTR: "CSTR"/);
assert.match(editor, /Reactions are defined on each reactor \(select a PFR or CSTR\)\./);
assert.doesNotMatch(editor, /set_reactions|reactionPick/, "no Thermo or Setup reaction editor remains");
assert.match(editor, /<ReactorKinetics /);
assert.match(editor, /<ReactorResults /);
assert.match(kineticsTsx, /MathTree/);
assert.match(kineticsTsx, /Evaluated by DWSIM · authored by Jarvis/);
assert.match(kineticsTsx, /<details><summary>Generated script \(read-only\)<\/summary>/);
assert.match(kineticsTsx, /role="alertdialog"/);
assert.match(kineticsTsx, /registry\.kinetics|kinetics\?\.unsupported/);
assert.match(resultsTsx, /Verified by Jarvis/);
assert.match(resultsTsx, /Results · DWSIM/);
for (const source of [kineticsTsx, resultsTsx]) assert.doesNotMatch(source, /dangerouslySetInnerHTML|innerHTML|JSON\.stringify\(/);
assert.doesNotMatch(css, /overflow-x:\s*scroll/);
console.log("180 reactor kinetics frontend: ok");
