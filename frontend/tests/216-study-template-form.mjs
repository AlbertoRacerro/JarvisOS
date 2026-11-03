// Spec 216 contract 8/9: study template form builds a server-valid typed contract from the derived domain only.
import assert from "node:assert/strict";
import fs from "node:fs";
import { buildStudyContract, formFromStudyContract, initialStudyTemplateForm, normalizeStudyTemplateForm, shortDigest, wallTreatmentsFor }
  from "../src/pages/studyTemplateForm.ts";

const domain = { domain_version: "cfd_pipe_fluid_domain_v1", geometry_digest: "geometry_spec:" + "a".repeat(64),
  source_part_id: "tube-1", inner_diameter_m: 0.046, length_m: 2.6, patches: ["inlet", "outlet", "wall"] };
const form = (patch) => normalizeStudyTemplateForm({ ...initialStudyTemplateForm(), velocity: "0.5", ...patch });

const defaults = initialStudyTemplateForm();
assert.equal(defaults.density, "998.2");
assert.equal(defaults.viscosity, "0.001002");

// turbulent: model and wall treatment carried, domain copied verbatim from the server value
const t = buildStudyContract(form({ velocity: "0.5" }), domain);
assert.deepEqual(t.errors, []);
assert.equal(t.contract.domain, domain);
assert.deepEqual(t.contract.inlet, { mean_velocity_m_s: 0.5 });
assert.equal(t.contract.regime, "turbulent_rans");
assert.equal(t.contract.turbulence_model, "kOmegaSST");
assert.equal(t.contract.wall_treatment, "low_re_resolved");
assert.equal(t.contract.outlet_pressure_pa, 0);
assert.equal(t.contract.mesh_level, 1);
assert.equal(t.contract.fluid.density_kg_m3, 998.2);
assert.equal(t.contract.fluid.name, "water_20C");
assert.equal(buildStudyContract(form({ density: "1010" }), domain).contract.fluid.name, "user_defined");
assert.ok(Math.abs(t.reynolds - 0.5 * 0.046 * 998.2 / 1.002e-3) < 1e-6);

// laminar: no turbulence model, resolved wall
const l = buildStudyContract(form({ regime: "laminar", velocity: "0.005" }), domain);
assert.deepEqual(l.errors, []);
assert.equal(l.contract.turbulence_model, null);
assert.equal(l.contract.wall_treatment, "resolved");
assert.equal("flow_rate_m3_s" in l.contract.inlet, false);

// regime / model / wall compatibility
assert.deepEqual(wallTreatmentsFor("laminar", "kEpsilon"), ["resolved"]);
assert.deepEqual(wallTreatmentsFor("turbulent_rans", "kEpsilon"), ["wall_function"]);
assert.equal(form({ turbulenceModel: "kEpsilon" }).wallTreatment, "wall_function");
assert.equal(form({ regime: "laminar" }).wallTreatment, "resolved");

// refusals mirror the server; no contract is built
const bad = (patch, pattern, dom = domain) => {
  const r = buildStudyContract(form(patch), dom);
  assert.equal(r.contract, null);
  assert.ok(r.errors.some((e) => pattern.test(e)), `${JSON.stringify(patch)} -> ${r.errors}`);
};
bad({ velocity: "" }, /velocity/);
bad({ velocity: "abc" }, /velocity/);
bad({ velocity: "51" }, /velocity/);
bad({ density: "0" }, /density/);
bad({ viscosity: "11" }, /viscosity/);
bad({ meshLevel: "5" }, /Mesh level/);
bad({ meshLevel: "1.5" }, /Mesh level/);
bad({ velocity: "0.01" }, /below 4000/);
bad({ regime: "laminar", velocity: "1" }, /above 2000/);
bad({ velocity: "0.5" }, /too short/, { ...domain, length_m: 0.2 });
bad({ velocity: "0.5" }, /fluid domain is unknown/, null);

assert.equal(shortDigest("sha256:" + "ab".repeat(32)), "abababababab");

// Saved standard templates can be edited without changing their typed inputs; advanced ones are refused.
const saved = { ...t.contract, inlet: { ...t.contract.inlet, flow_rate_m3_s: null,
  turbulence_intensity: 0.05, mixing_length_over_d: 0.07 },
solver: { max_iterations: 20000, residual_target: 1e-6, wall_time_limit_s: 1800 } };
assert.deepEqual(buildStudyContract(formFromStudyContract(saved), domain).contract, t.contract);
assert.equal(formFromStudyContract({ ...saved, solver: { ...saved.solver, max_iterations: 500 } }), null);
assert.equal(formFromStudyContract({ ...saved, inlet: { ...saved.inlet, flow_rate_m3_s: 0.001 } }), null);

// the domain is never a form field, and the PUT sends the component revision and active version
const panel = fs.readFileSync(new URL("../src/pages/StudyTemplatesPanel.tsx", import.meta.url), "utf8");
assert.ok(!/domain\.(inner_diameter_m|length_m|geometry_digest)\s*=/.test(panel) && !/id="study-(diameter|length)/.test(panel));
const api = fs.readFileSync(new URL("../src/api/designProject.ts", import.meta.url), "utf8");
assert.match(api, /cfd-study-templates\/\$\{encodeURIComponent\(contract\.regime\)\}`, "PUT", \{\s*contract, expected_revision: component\.revision, expected_active_version_id: versionId/);
console.log("216 study template form: ok");
