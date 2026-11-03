import {
  chooseThread,
  contextMatches,
  detailContext,
  initialThreadState,
  listContext,
  orderThreads,
  submitContext,
  withThread,
  withWorkspace
} from "./threadState";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const a = { id: "a", workspace_id: "w", title: null, created_at: "1", last_activity_at: "2" };
const b = { id: "b", workspace_id: "w", title: null, created_at: "1", last_activity_at: "3" };
assert(orderThreads([a, b])[0]?.id === "b", "newest activity must sort first");
assert(chooseThread([a, b], "a") === "a", "existing selection must be retained");
assert(chooseThread([a, b], "missing") === "b", "missing selection must fall back deterministically");

let state = withWorkspace(initialThreadState(), "A");
const firstA = listContext(state);
state = withWorkspace(state, "B");
state = withWorkspace(state, "A");
assert(!contextMatches(listContext(state), firstA), "A→B→A must reject the first A response");

state = withThread(state, "X");
const firstX = detailContext(state);
const submitX = submitContext(state);
state = withThread(state, "Y");
state = withThread(state, "X");
assert(!contextMatches(detailContext(state), firstX), "X→Y→X must reject the first X detail");
assert(!contextMatches(submitContext(state), submitX), "selection changes must invalidate submit ownership");

const beforeWorkspaceChange = submitContext(state);
state = withWorkspace(state, "B");
assert(!contextMatches(submitContext(state), beforeWorkspaceChange), "workspace changes must invalidate submit completion");

console.log("AI thread state harness: PASS");
