import { acceptsResponse, chooseSelection, projectPayload, visibleRuns } from "./state";

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

assert(!acceptsResponse({ generation: 1, identity: "A" }, 3, "A"), "accepted stale A→B→A response");
assert(!acceptsResponse({ generation: 7, identity: "A:X" }, 9, "A:X"), "accepted stale X→Y→X response");
assert(acceptsResponse({ generation: 9, identity: "A:X" }, 9, "A:X"), "rejected exact current response");
assert(chooseSelection("x", [{ id: "x" }, { id: "y" }]) === "x", "did not preserve present selection");
assert(chooseSelection("x", [{ id: "y" }]) === "y", "did not replace disappeared selection");
assert(chooseSelection("x", []) === null, "did not clear empty selection");

const rows = [{ id: "x", run_label: "Alpha", status: "succeeded" }, { id: "y", run_label: "Beta", status: "odd-history" }];
assert(visibleRuns(rows, "beta", "")[0]?.id === "y", "query filter failed");
assert(visibleRuns(rows, "", "odd-history")[0]?.id === "y", "exact unknown-status filter failed");

assert(projectPayload(null, "input").state === "empty", "null payload state failed");
assert(projectPayload("{", "input").state === "malformed", "malformed payload state failed");
const long = projectPayload(JSON.stringify({ value: "x".repeat(2500) }), "input");
assert(long.state === "ok" && long.truncated, "string truncation failed");
const wide = projectPayload(JSON.stringify({ values: Array.from({ length: 70 }, (_, index) => index) }), "input");
assert(wide.state === "ok" && wide.truncated, "item truncation failed");

console.log("RUNS-WORKBENCH-1 state harness passed");
