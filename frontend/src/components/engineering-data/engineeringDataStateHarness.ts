import { acceptsWorkspaceResponse, chooseEngineeringSelection, ENGINEERING_KINDS, projectEngineeringData, recordKey, visibleEngineeringRecords } from "./engineeringDataState";

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

const rows = projectEngineeringData({
  modelSpecs: [{ id: "m2", workspace_id: "w", title: "zeta", engineering_question: "Q", status: "odd-status", maturity_status: "draft", schema_version: 1, created_at: "", updated_at: "" }, { id: "m1", workspace_id: "w", title: "Alpha", engineering_question: "Pump sizing", status: "active", maturity_status: "draft", schema_version: 1, created_at: "", updated_at: "" }],
  assumptions: [{ id: "a1", workspace_id: "w", statement: "Sea water density", confidence: 0.7, status: "accepted" }],
  parameters: [{ id: "p1", workspace_id: "w", name: "Tube diameter", symbol: "D", value: "0.05", unit: "m", value_status: "accepted", lifecycle_state: "active", status: "accepted", created_at: "", updated_at: "" }],
  decisions: [{ id: "d1", workspace_id: "w", title: "Material", decision_text: "Use HDPE", status: "recorded" }],
});
assert(rows.map(recordKey).join(",") === "model-spec:m1,model-spec:m2,assumption:a1,parameter:p1,decision:d1", "kind/primary ordering drift");
assert(visibleEngineeringRecords(rows, "pump sizing", ENGINEERING_KINDS).map(recordKey).join(",") === "model-spec:m1", "explicit field search failed");
assert(visibleEngineeringRecords(rows, "0.05", ["parameter"]).map(recordKey).join(",") === "parameter:p1", "parameter value search failed");
assert(visibleEngineeringRecords(rows, "odd-status", ["model-spec"]).length === 1, "unknown status became hidden");
assert(chooseEngineeringSelection("decision:d1", visibleEngineeringRecords(rows, "", ["model-spec"])) === "model-spec:m1", "selection recovery failed");
assert(chooseEngineeringSelection("model-spec:m2", rows) === "model-spec:m2", "visible selection not preserved");
assert(!acceptsWorkspaceResponse(1, "w-a", 3, "w-a"), "stale A-B-A response accepted");
assert(acceptsWorkspaceResponse(3, "w-a", 3, "w-a"), "current workspace response rejected");
console.log("engineering-data state harness passed");
