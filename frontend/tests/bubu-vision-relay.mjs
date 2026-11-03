import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Bubu screenshots reach Claude only through the reviewed Relay escalation: the approval panel offers them
// (ticked, not screened like text), the API carries the approved digests, and the bubble offers "Ask Claude".
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const store = await import("../src/components/bubu/conversationStore.ts");

// ---- API: digests are sent only when there are some (old backends keep working) -------------------------------------------
const api = read("src/api/threads.ts");
assert.match(api, /image_count\?: number;\s*image_digests\?: string\[\];/, "the draft type carries the screenshots");
assert.match(api, /textDigest: string, imageDigests: string\[\] = \[\]/);
assert.match(api, /\.\.\.\(imageDigests\.length \? \{ image_digests: imageDigests \} : \{\}\)/);

// ---- approval panel: explicit, default on, honest about screening --------------------------------------------------------
const hook = read("src/components/ai/useJarvisSidecar.tsx");
assert.match(hook, /const \[relaySendImages, setRelaySendImages\] = useState\(true\)/, "ticked by default");
assert.match(hook, /\(relayDraft\.image_count \?\? 0\) > 0/, "the checkbox exists only when the draft has screenshots");
assert.match(hook, /Send \{relayDraft\.image_count\} screenshot\{relayDraft\.image_count === 1 \? "" : "s"\} to Claude/);
assert.match(hook, /Screenshots are not screened like text\./);
assert.match(hook, /relaySendImages \? relayDraft\.image_digests \?\? \[\] : \[\]/, "only ticked digests are approved");
assert.match(hook, /setRelayDraft\(null\); setRelaySendImages\(true\)/, "a fresh review starts ticked again");

// ---- Bubu: "Ask Claude" starts the same escalation, only when Relay is on -----------------------------------------------
assert.match(hook, /askClaude: turnId => \{ void prepareRelayEscalation\(turnId\); \}/);
assert.match(hook, /publishBubuRelayEnabled\(Boolean\(relayStatus\?\.enabled\)\)/);
const bubble = read("src/components/bubu/BubuBubble.tsx");
assert.match(bubble, /turn\.note && relayEnabled \?/, "no Relay, no button: the note stays alone");
assert.match(bubble, /askClaudeAboutTurn\(turn\.id\); onExpand\(\);/, "it opens the full panel where the approval is shown");
assert.match(bubble, />Ask Claude</);

// ---- store: shape unchanged, Relay flag and handler are additive ----------------------------------------------------------
{
  assert.deepEqual(Object.keys(store.getBubuConversation()).sort(), ["error", "failedPrompt", "pending", "ready", "sleep", "submit", "turns"]);
  store.askClaudeAboutTurn("ignored without an owner");
  const asked = [];
  const unregister = store.registerBubuHandlers({ submit: async () => {}, sleep: () => {}, askClaude: (id) => asked.push(id) });
  store.askClaudeAboutTurn("t1");
  assert.deepEqual(asked, ["t1"]);
  let notified = 0;
  const unsubscribe = store.subscribeBubuConversation(() => { notified += 1; });
  store.publishBubuRelayEnabled(true);
  store.publishBubuRelayEnabled(true);
  assert.equal(notified, 1, "only a change notifies");
  unsubscribe();
  unregister();
  const withoutAsk = store.registerBubuHandlers({ submit: async () => {}, sleep: () => {} });
  store.askClaudeAboutTurn("t2");
  withoutAsk();
}
{
  // A note arriving later republishes the turn.
  let notified = 0;
  const unsubscribe = store.subscribeBubuConversation(() => { notified += 1; });
  const turn = { id: "a", role: "jarvis", text: "Here." };
  store.publishBubuConversation({ ready: true, turns: [turn], pending: false, error: null });
  store.publishBubuConversation({ ready: true, turns: [{ ...turn, note: "n" }], pending: false, error: null });
  assert.equal(notified, 2);
  unsubscribe();
  store.publishBubuConversation({ ready: false, turns: [], pending: false, error: null });
}

console.log("bubu vision relay: ok");
