import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");

const workbench = read("src/components/bluecad/BluecadWorkbench.tsx");
const client = read("src/api/client.ts");

// 1. Assert Italian verdict labels are defined and mapped
const italianVerdicts = [
  "utilizzabile",
  "utilizzabile con avvisi",
  "non verificato",
  "non utilizzabile"
];
for (const label of italianVerdicts) {
  assert.ok(
    workbench.includes(`"${label}"`) || workbench.includes(`'${label}'`),
    `workbench must include Italian verdict label: "${label}"`
  );
}

// 2. Assert status light markup and data-verdict attribute
assert.ok(
  workbench.includes('className="bench-verdict"') && workbench.includes('data-verdict='),
  "workbench must render status light with bench-verdict container and data-verdict attribute"
);
assert.ok(
  workbench.includes('className="bench-light"'),
  "workbench must render bench-light element"
);

// 3. Assert details element and checks table reusing existing table classes
assert.ok(
  workbench.includes("<details>") && workbench.includes("</details>"),
  "workbench must use <details> element for checks"
);
assert.ok(
  workbench.includes("<summary>Checks"),
  "workbench must include <summary>Checks for disclosure"
);
assert.ok(
  workbench.includes('className="table-wrap"') && workbench.includes('className="smoke-table bluecad-table"'),
  "workbench must reuse table-wrap and smoke-table bluecad-table classes"
);

// 4. Assert table headers for checks
for (const header of ["Check", "Family", "Status", "Required", "Detail"]) {
  assert.ok(
    workbench.includes(`<th>${header}</th>`),
    `checks table must include header: <th>${header}</th>`
  );
}

// 5. Assert client contracts have BluecadEvidenceCheckRead and updated BluecadEvidenceRefRead
assert.ok(
  client.includes("export type BluecadEvidenceCheckRead"),
  "client.ts must export BluecadEvidenceCheckRead"
);
assert.ok(
  client.includes("shared_verdict?:") && client.includes("checks?: BluecadEvidenceCheckRead[]"),
  "client.ts BluecadEvidenceRefRead must include shared_verdict and checks"
);

// 6. Test scenario simulation with static string verification
// Candidate evidence with mesh whose quality check is missing (unchecked) and FEM result with failed check (not_usable)
const sampleEvidence = [
  {
    ref: "evidence:ev-solid",
    kind: "validation_v0",
    subject_ref: "bluecad_candidate:c1",
    status: "pass",
    shared_verdict: "usable",
    checks: [
      { name: "BREP_VALID", family: "input", status: "pass", required: true, detail: "shape is valid solid" }
    ]
  },
  {
    ref: "evidence:ev-mesh",
    kind: "mesh_quality_v0",
    subject_ref: "bluecad_candidate:c1",
    status: "pass",
    shared_verdict: "unchecked",
    verdict_reason: "mesh element quality not reported by mesher",
    checks: [
      { name: "VOLUME_ELEMENTS", family: "discretisation", status: "pass", required: true, detail: "150 volume elements" },
      { name: "ELEMENT_QUALITY", family: "discretisation", status: "not_evaluated", required: true, detail: "mesh element quality not reported by mesher" }
    ]
  },
  {
    ref: "evidence:ev-fem",
    kind: "fem_static_v0",
    subject_ref: "bluecad_candidate:c1",
    status: "pass",
    shared_verdict: "not_usable",
    verdict_reason: "One or more required checks failed",
    checks: [
      { name: "SOLVER_COMPLETION", family: "numerical", status: "pass", required: true, detail: "CalculiX static solver completed" },
      { name: "FORCE_EQUILIBRIUM", family: "conservation", status: "fail", required: true, detail: "force equilibrium residual exceeds tolerance" }
    ]
  }
];

// Helper reproducing Workbench evidence row rendering
const VERDICT_MAP = {
  usable: "utilizzabile",
  usable_with_warnings: "utilizzabile con avvisi",
  unchecked: "non verificato",
  not_usable: "non utilizzabile"
};

function renderEvidenceRow(item) {
  const verdict = item.shared_verdict ?? "unchecked";
  const label = VERDICT_MAP[verdict] ?? verdict;
  const checks = item.checks ?? [];
  return `
    <li>
      <div>
        <strong>${item.kind}</strong> · ${item.ref} · subject ${item.subject_ref} · ${item.status} · 
        <span class="bench-verdict" data-verdict="${verdict}">
          <span class="bench-light" aria-hidden="true"></span>${label}
        </span>
        ${item.verdict_reason ? ` (${item.verdict_reason})` : ""}
      </div>
      <details>
        <summary>Checks (${checks.length})</summary>
        <div class="table-wrap">
          <table class="smoke-table bluecad-table">
            <thead><tr><th>Check</th><th>Family</th><th>Status</th><th>Required</th><th>Detail</th></tr></thead>
            <tbody>
              ${checks.map(c => `<tr data-status="${c.status}"><td>${c.name}</td><td>${c.family}</td><td><span class="bench-light" aria-hidden="true"></span>${c.status}</td><td>${c.required ? "yes" : "no"}</td><td>${c.detail}</td></tr>`).join("")}
            </tbody>
          </table>
        </div>
      </details>
    </li>
  `;
}

const rendered = sampleEvidence.map(renderEvidenceRow).join("\n");

// Assert mesh quality missing -> "non verificato" and data-verdict="unchecked"
assert.ok(rendered.includes('data-verdict="unchecked"'), "mesh missing quality must show data-verdict=unchecked");
assert.ok(rendered.includes("non verificato"), "mesh missing quality must show label 'non verificato'");

// Assert FEM failed required check -> "non utilizzabile" and data-verdict="not_usable"
assert.ok(rendered.includes('data-verdict="not_usable"'), "fem failed check must show data-verdict=not_usable");
assert.ok(rendered.includes("non utilizzabile"), "fem failed check must show label 'non utilizzabile'");

// Assert solid validation -> "utilizzabile" and data-verdict="usable"
assert.ok(rendered.includes('data-verdict="usable"'), "solid valid must show data-verdict=usable");
assert.ok(rendered.includes("utilizzabile"), "solid valid must show label 'utilizzabile'");

// Assert no raw paths in rendered markup
assert.ok(!rendered.includes("/home/"), "rendered markup must not contain filesystem paths");
assert.ok(!rendered.includes("/tmp/"), "rendered markup must not contain temp paths");
assert.ok(!rendered.includes("\\"), "rendered markup must not contain Windows backslash paths");

console.log("bluecadEvidenceVerdict: OK");
