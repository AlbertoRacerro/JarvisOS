import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { studioProposalView } from "../src/components/ai/studioProposalPresentation.ts";

const proposal = {
  surface: "studio", state: "proposed", draft_id: "draft-1", changes: [
    { label: "Source", before: "def evaluate(inputs):\n    return {'dose': 1}", after: "def evaluate(inputs):\n    return {'dose': 2}" }
  ],
  request: { actions: [{
    op: "studio_draft", mode: "update", category: "calculation", name: "Dose calculator",
    engineering_question: "How much nutrient is needed?",
    contract: { inputs: [{ name: "flow", label: "Flow", unit: "kg/s", min: 0, description: "Feed rate" }], outputs: [{ name: "dose", label: "Dose", unit: "kg/s" }] },
    source: "def evaluate(inputs):\n    return {'dose': 2}", assumptions: "Steady flow"
  }] }
};
const view = studioProposalView(proposal);
assert.equal(view?.mode, "update");
assert.equal(view?.question, "How much nutrient is needed?");
assert.equal(view?.groups[0].rows[0], "Flow (flow) [kg/s] — Feed rate · bounds 0…+∞");
assert.equal(view?.groups[1].rows[0], "Dose (dose) [kg/s]");
assert.equal(view?.assumptions, "Steady flow");
assert.match(view?.sourceDiff ?? "", /-     return \{'dose': 1\}/);
assert.match(view?.sourceDiff ?? "", /\+     return \{'dose': 2\}/);
assert.deepEqual(studioProposalView({ ...proposal, request: { actions: [{ ...proposal.request.actions[0], category: "process_unit", contract: { culture_generation: ["biomass"] } }] } })?.groups[0].rows, ["biomass"]);
assert.equal(studioProposalView({ ...proposal, surface: "process" }), null);
assert.equal(studioProposalView({ ...proposal, request: { actions: [{ op: "add_unit" }] } }), null);

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const app = read("src/App.tsx");
const sidecar = read("src/components/ai/useJarvisSidecar.tsx");
const card = read("src/components/ai/WorkspaceActionCards.tsx");
const studio = read("src/pages/Studio.tsx");
const api = read("src/api/studio.ts");
assert.match(app, /<Studio [^>]*onModelSelectionChange=\{setStudioModelId\}/);
assert.doesNotMatch(app, /route\.id === "design-studio" \? \{ sidecar: undefined \}/);
assert.match(sidecar, /const applyAction = async \(actionId: string\) => runActionRequest\(actionId, applyWorkspaceAction\)/);
assert.match(card, /action\.state === "proposed"[\s\S]*onApply/);
assert.match(studio, /detail\.surface !== "studio"[\s\S]*getStudioModel\(workspaceId, detail\.draftId\)/);
assert.match(studio, /<DerivedDocumentation derived=\{liveMath\}[\s\S]*<PythonEditor value=\{source\}/);
assert.match(studio, /model\.last_test_report && \(sourceDirty \|\| model\.last_test_report\.source_sha256 !== model\.source_sha256\)[\s\S]*older source/);
assert.match(studio, /previewStudioDocumentation\(source\)/);
assert.match(api, /"\/studio\/documentation\/preview", "POST", \{ source \}/);
assert.match(read("src/components/process/StudioUnitInspector.tsx"), /<AppLink href=\{href\}[^>]*>Open model in Studio<\/AppLink>/);
console.log("193 Studio Sidecar proposal and math workspace checks passed");
