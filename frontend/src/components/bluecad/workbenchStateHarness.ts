import { acceptsMutation, acceptsRequest, duplicateBrief, mutationConflicts, revalidateSelection, type MutationContext, type RequestContext } from "./workbenchState";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const currentB: RequestContext = { generation: 2, workspaceId: "B", candidateId: "b1" };
assert(!acceptsRequest(currentB, { generation: 1, workspaceId: "A", candidateId: "a1" }), "stale workspace response accepted");
assert(!acceptsRequest(currentB, { generation: 1, workspaceId: "B", candidateId: "b1" }), "stale same-context generation accepted");
assert(acceptsRequest(currentB, currentB), "current request rejected");
assert(!acceptsRequest(currentB, { generation: 2, workspaceId: "B", candidateId: "b2" }), "stale candidate response accepted");

const validationCurrent: RequestContext = { generation: 8, workspaceId: "B", candidateId: "b2", artifactId: "report-b2" };
const validationStale: RequestContext = { generation: 7, workspaceId: "B", candidateId: "b1", artifactId: "report-b1" };
let visibleValidation = "candidate-b";
if (acceptsRequest(validationCurrent, validationStale)) visibleValidation = "candidate-a";
assert(!acceptsRequest(validationCurrent, validationStale), "stale validation response accepted");
assert(visibleValidation === "candidate-b", "stale validation response mutated visible state");
assert(acceptsRequest(validationCurrent, validationCurrent), "current validation response rejected");

const candidates = [{ id: "a", status: "archived" }, { id: "b", status: "valid" }, { id: "c", status: "parked" }];
assert(revalidateSelection(candidates, "b", false) === "b", "refresh did not preserve visible selection");
assert(revalidateSelection(candidates, "a", false) === "b", "hidden archived selection did not reselect deterministically");
assert(revalidateSelection([{ id: "a", status: "archived" }], "a", false) === null, "empty visible list did not clear selection");
assert(revalidateSelection([{ id: "a", status: "archived" }], "a", true) === "a", "show archived did not preserve archived selection");

assert(!mutationConflicts(null, "archive"), "idle mutation incorrectly blocked");
assert(mutationConflicts("archive", "promote"), "archive/promote conflict not blocked");
assert(mutationConflicts("promote", "archive"), "promote/archive conflict not blocked");
assert(mutationConflicts("create", "archive"), "concurrent create/candidate mutation not serialized");
assert(mutationConflicts("archive", "create"), "concurrent candidate/create mutation not serialized");

const createStart: MutationContext = { generation: 4, workspaceId: "A", candidateId: null, kind: "create" };
assert(!acceptsMutation({ generation: 5, workspaceId: "B", candidateId: null }, createStart), "stale create completion accepted after workspace switch");
assert(acceptsMutation({ generation: 4, workspaceId: "A", candidateId: null }, createStart), "current create completion rejected");

const archiveStart: MutationContext = { generation: 10, workspaceId: "A", candidateId: "a1", kind: "archive" };
assert(!acceptsMutation({ generation: 11, workspaceId: "A", candidateId: "a2" }, archiveStart), "stale archive completion accepted after selection change");
assert(acceptsMutation({ generation: 10, workspaceId: "A", candidateId: "a1" }, archiveStart), "current archive completion rejected");

const duplicate = duplicateBrief("copy this brief");
assert(duplicate.briefText === "copy this brief", "duplicate brief lost source text");
assert(duplicate.backendMutation === false, "duplicate brief became backend mutation");

console.log("BLUECAD workbench state harness PASS");
