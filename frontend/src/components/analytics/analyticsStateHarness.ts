import type { ModelImplementation } from "../../api/client";
import type { SimulationRunSummary } from "../../api/runs";
import {
  MAX_CONFIGURATION_VARIABLES,
  MAX_OUTPUT_KEYS,
  MAX_SELECTED_RUNS,
  acceptsWorkspaceResponse,
  compareAnalyticsRuns,
  compareEngineeringConfigurations,
  normalizeBaselineRunId,
  projectAnalyticsRun,
  retainExistingSelection,
  toggleRunSelection,
  type AnalyticsRun,
} from "./analyticsState";
import { parseSourceRunTarget, sourceRunHref } from "./variantComparisonNavigation";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const envelope = (outputs: Record<string, unknown>) => JSON.stringify({ schema_version: 1, status: "succeeded", outputs, diagnostics: [] });
const run = (id: string, outputs: Record<string, unknown>, patch: Partial<AnalyticsRun> = {}): AnalyticsRun => ({ id, model_version_id: "model-v1", run_label: id, status: "succeeded", output_payload: envelope(outputs), ...patch });
const metric = (value: unknown, unit: unknown) => ({ value, unit });

const valid = projectAnalyticsRun(run("a", { pressure: metric(10, "Pa") }));
assert(valid.state === "ok" && valid.observations.pressure.value === 10, "valid schema-v1 observation was not extracted");
for (const [name, value] of Object.entries({ text: "10", bool: true, nil: null, object: {}, array: [] })) {
  const projected = projectAnalyticsRun(run(name, { pressure: metric(value, "Pa") }));
  assert(projected.state === "ok" && projected.rejectedKeys.pressure, `${name} value was not rejected`);
}
assert(projectAnalyticsRun(run("bad-status", { p: metric(1, "Pa") }, { status: "failed" })).state === "rejected", "non-succeeded run was accepted");
assert(projectAnalyticsRun(run("null-payload", {}, { output_payload: null })).state === "rejected", "null payload was accepted");
assert(projectAnalyticsRun(run("oversized-payload", {}, { output_payload: "x".repeat(1_048_577) })).state === "rejected", "oversized payload was accepted");
assert(projectAnalyticsRun(run("bad-json", {}, { output_payload: "{" })).state === "rejected", "malformed payload was accepted");
assert(projectAnalyticsRun(run("bad-array", {}, { output_payload: "[]" })).state === "rejected", "array payload was accepted");
assert(projectAnalyticsRun(run("wrong-schema", {}, { output_payload: JSON.stringify({ schema_version: 2, status: "succeeded", outputs: {} }) })).state === "rejected", "wrong schema was accepted");
assert(projectAnalyticsRun(run("wrong-envelope-status", {}, { output_payload: JSON.stringify({ schema_version: 1, status: "failed", outputs: {} }) })).state === "rejected", "wrong envelope status was accepted");
assert(projectAnalyticsRun(run("wrong-outputs-shape", {}, { output_payload: JSON.stringify({ schema_version: 1, status: "succeeded", outputs: [] }) })).state === "rejected", "array outputs shape was accepted");
assert(projectAnalyticsRun(run("empty-key", { "": metric(1, "Pa") })).state === "rejected", "empty key did not fail closed");
assert(projectAnalyticsRun(run("blank-unit", { p: metric(1, " ") })).rejectedKeys.p, "blank unit was accepted");
assert(projectAnalyticsRun(run("oversized-unit", { p: metric(1, "u".repeat(65)) })).rejectedKeys.p, "oversized unit was accepted");
assert(projectAnalyticsRun(run("oversized-key", { ["x".repeat(161)]: metric(1, "Pa") })).state === "rejected", "oversized key did not fail closed");
const tooMany = Object.fromEntries(Array.from({ length: MAX_OUTPUT_KEYS + 1 }, (_, index) => [`k${index}`, metric(index, "Pa")]));
assert(projectAnalyticsRun(run("many", tooMany)).state === "rejected", "output-key cap was not enforced");

const same = compareAnalyticsRuns([run("a", { p: metric(10, "Pa") }), run("b", { p: metric(14, "Pa") })]);
const sameGroup = same.groups[0];
assert(same.state === "ready" && sameGroup?.state === "valid" && sameGroup.min === 10 && sameGroup.max === 14 && sameGroup.range === 4, "same metric/unit comparison failed");
assert(compareAnalyticsRuns([run("a", { p: metric(10, "Pa") }), run("b", { p: metric(14, "Pa") }, { model_version_id: "model-v2" })]).state === "rejected", "mixed model versions were accepted");
const unitMismatch = compareAnalyticsRuns([run("a", { p: metric(10, "Pa") }), run("b", { p: metric(0.014, "kPa") })]);
assert(unitMismatch.groups[0]?.state === "rejected", "Pa/kPa mismatch was converted or accepted");
const differentKeys = compareAnalyticsRuns([run("a", { p: metric(10, "Pa") }), run("b", { q: metric(10, "Pa") })]);
assert(differentKeys.groups.every((group) => group.state === "rejected"), "same-unit different metrics were grouped");
const missingMetric = compareAnalyticsRuns([run("a", { p: metric(10, "Pa") }), run("b", { p: metric("bad", "Pa") })]);
assert(missingMetric.groups[0]?.state === "rejected", "invalid/missing selected metric was silently dropped");

let selected: string[] = [];
for (let index = 0; index < MAX_SELECTED_RUNS; index += 1) selected = toggleRunSelection(selected, `r${index}`);
assert(toggleRunSelection(selected, "overflow").length === MAX_SELECTED_RUNS, "six-run cap was not enforced");
assert(toggleRunSelection(selected, "r0").length === MAX_SELECTED_RUNS - 1, "selected run could not be removed");
assert(retainExistingSelection(["a", "gone", "b"], [run("a", {}), run("b", {})]).join(",") === "a,b", "disappeared run was not removed");
assert(!acceptsWorkspaceResponse({ generation: 1, identity: "A" }, 3, "A"), "stale A-B-A response accepted");
assert(acceptsWorkspaceResponse({ generation: 3, identity: "A" }, 3, "A"), "current workspace response rejected");
assert(normalizeBaselineRunId(["a", "b"], null) === "a", "first selected run did not become baseline");
assert(normalizeBaselineRunId(["a", "b"], "b") === "b", "explicit selected baseline was not preserved");
assert(normalizeBaselineRunId(["a"], "b") === "a", "removed baseline did not fall back to first remaining run");

const implementation = (
  variables: Array<{ name: string; label?: string; unit: string; required: boolean }>,
  id = "model-v1",
  semanticContext?: unknown
): ModelImplementation => ({
  id,
  workspace_id: "ws",
  model_spec_id: "spec",
  version_label: id,
  implementation_artifact_id: "artifact",
  status: "active",
  script_sha256: "a".repeat(64),
  script_path: "model.py",
  created_at: "2026-08-24T00:00:00Z",
  input_contract_sha256: "b".repeat(64),
  input_contract: {
    schema_version: semanticContext === undefined ? 1 : 3,
    evaluation_mode: "forward",
    ...(semanticContext === undefined ? {} : { semantic_context: semanticContext }),
    variables: variables.map((item) => ({
      name: item.name,
      label: item.label ?? item.name,
      unit: item.unit,
      required: item.required,
      category: "operating",
      description: "fixture"
    }))
  } as unknown as ModelImplementation["input_contract"]
});
const inputRun = (
  id: string,
  inputs: Record<string, unknown>,
  patch: Partial<SimulationRunSummary> = {}
): SimulationRunSummary => ({
  id,
  workspace_id: "ws",
  model_version_id: "model-v1",
  run_label: id,
  status: "succeeded",
  input_payload: JSON.stringify(inputs),
  parameter_payload: null,
  output_payload: envelope({ p: metric(1, "Pa") }),
  started_at: null,
  completed_at: null,
  created_at: "2026-08-24T00:00:00Z",
  notes: null,
  ...patch
});
const binding = (value: unknown, unit: string, source_parameter_id?: string) => ({ value, unit, ...(source_parameter_id ? { source_parameter_id } : {}) });
const variables = [
  { name: "temperature", label: "Temperature", unit: "K", required: true },
  { name: "pressure", label: "Pressure", unit: "Pa", required: true },
  { name: "optional", label: "Optional input", unit: "kg", required: false }
];
const contract = implementation(variables);
const semanticContract = implementation(variables, "model-v1", {
  applicable_part_kinds: ["tube_run"],
  model_family_key: "geometry_hydraulics",
  model_family_label: "Geometry & hydraulics",
  model_option_label: "Reviewed tubular-loop V0"
});
const malformedSemanticContract = implementation(variables, "model-v1", {
  applicable_part_kinds: ["tube_run"],
  model_family_key: "geometry_hydraulics",
  model_family_label: "Geometry & hydraulics",
  model_option_label: 42
});
const configA = inputRun("config-a", { temperature: binding(300, "K", "historical-param"), pressure: binding(10, "Pa") });
const configB = inputRun("config-b", { temperature: binding(330, "K"), pressure: binding(10, "Pa") });
const configComparison = compareEngineeringConfigurations("ws", [configA, configB], [contract], null);
assert(configComparison.state === "ready" && configComparison.baselineRunId === "config-a", "compatible configuration did not compare with deterministic baseline");
assert(configComparison.modelChoice === null, "model choice was fabricated without authoritative schema-v3 metadata");
const semanticComparison = compareEngineeringConfigurations("ws", [configA, configB], [semanticContract], null);
assert(semanticComparison.state === "ready" && semanticComparison.modelChoice?.familyKey === "geometry_hydraulics" && semanticComparison.modelChoice.optionLabel === "Reviewed tubular-loop V0", "valid schema-v3 model choice was omitted");
const malformedSemanticComparison = compareEngineeringConfigurations("ws", [configA, configB], [malformedSemanticContract], null);
assert(malformedSemanticComparison.state === "ready" && malformedSemanticComparison.modelChoice === null, "malformed schema-v3 model choice was rendered");
const temperatureRow = configComparison.rows.find((row) => row.name === "temperature");
assert(temperatureRow?.cells[0].displayValue === "300" && temperatureRow.cells[1].delta === 30, "configuration value/delta projection failed");
const optionalRow = configComparison.rows.find((row) => row.name === "optional");
assert(optionalRow?.cells.every((cell) => cell.value === null && cell.displayValue === "Empty"), "missing optional input did not remain explicit Empty");
const rebased = compareEngineeringConfigurations("ws", [configA, configB], [contract], "config-b");
assert(rebased.state === "ready" && rebased.rows.find((row) => row.name === "temperature")?.cells[0].delta === -30, "explicit baseline did not produce deterministic absolute delta");
const unavailableWithBaseline = compareEngineeringConfigurations("ws", [configA, configB], [], "config-b");
assert(unavailableWithBaseline.state === "rejected" && unavailableWithBaseline.baselineRunId === "config-b", "configuration-unavailable state discarded the selected comparison baseline");
assert(compareEngineeringConfigurations("ws", [configA, { ...configB, model_version_id: "model-v2" }], [contract, implementation([{ name: "temperature", unit: "K", required: true }], "model-v2")], null).state === "rejected", "different model versions were configuration-aligned");
assert(compareEngineeringConfigurations("ws", [configA, inputRun("bad-unit", { temperature: binding(300, "C"), pressure: binding(10, "Pa") })], [contract], null).state === "rejected", "configuration unit mismatch was accepted");
assert(compareEngineeringConfigurations("ws", [configA, inputRun("unknown", { temperature: binding(300, "K"), pressure: binding(10, "Pa"), surprise: binding(1, "Pa") })], [contract], null).state === "rejected", "unknown configuration field was accepted");
assert(compareEngineeringConfigurations("ws", [configA, inputRun("required-missing", { temperature: binding(300, "K") })], [contract], null).state === "rejected", "missing required configuration field was accepted");
assert(compareEngineeringConfigurations("ws", [configA, inputRun("nonfinite", { temperature: binding(Number.NaN, "K"), pressure: binding(10, "Pa") })], [contract], null).state === "rejected", "non-finite configuration field was accepted");
const oversizedContract = implementation(Array.from({ length: MAX_CONFIGURATION_VARIABLES + 1 }, (_, index) => ({ name: `v${index}`, unit: "Pa", required: false })));
assert(compareEngineeringConfigurations("ws", [configA, configB], [oversizedContract], null).state === "rejected", "oversized configuration contract was accepted");
assert(compareEngineeringConfigurations("ws", [configA, { ...configB, input_payload: "x".repeat(1_048_577) }], [contract], null).state === "rejected", "oversized input payload was accepted");

const sourceHref = sourceRunHref("workspace A", "run/42");
const sourceTarget = parseSourceRunTarget(sourceHref.slice(sourceHref.indexOf("?")));
assert(sourceTarget?.workspaceId === "workspace A" && sourceTarget.runId === "run/42", "source-run deep link did not preserve exact workspace and run identity");
assert(parseSourceRunTarget("?workspace=ws") === null, "partial source-run deep link was accepted");
assert(parseSourceRunTarget("?workspace=ws&run=") === null, "blank source-run identity was accepted");
assert(parseSourceRunTarget(`?workspace=${"w".repeat(257)}&run=r`) === null, "oversized source-run identity was accepted");
assert(parseSourceRunTarget("?workspace=ws&workspace=other&run=r") === null, "ambiguous source-run workspace was accepted");

console.log("analytics state harness passed");
