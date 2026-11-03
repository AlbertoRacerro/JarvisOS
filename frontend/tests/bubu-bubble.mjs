// BUBU-2 (bubble): the shared conversation store, pose selection, placement and the wiring that makes
// Bubu open a speech bubble while Expand opens the old Jarvis panel.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(join(root, path), "utf8");

const store = await import("../src/components/bubu/conversationStore.ts");
const { bubuPose, BUBU_POSES, bubuPoseSrc } = await import("../src/components/bubu/bubuPose.ts");
const { placeBubble } = await import("../src/components/bubu/bubblePlacement.ts");

// ---- store: shape, publish/subscribe, registered handlers -------------------------------------------------------
{
  const initial = store.getBubuConversation();
  assert.deepEqual(Object.keys(initial).sort(), ["error", "failedPrompt", "pending", "ready", "sleep", "submit", "turns"]);
  assert.equal(initial.ready, false);
  assert.deepEqual(initial.turns, []);
  assert.equal(initial.pending, false);
  assert.equal(initial.error, null);
  await initial.submit("ignored without an owner");
  initial.sleep();

  let notified = 0;
  const unsubscribe = store.subscribeBubuConversation(() => { notified += 1; });
  const calls = [];
  const unregister = store.registerBubuHandlers({
    submit: async (prompt, extras) => { calls.push(["submit", prompt, extras]); },
    sleep: () => { calls.push(["sleep"]); },
  });

  // The delegates are stable: the same functions work before and after registration.
  const before = store.getBubuConversation();
  await before.submit("hello", { images: ["data:image/png;base64,AAAA"] });
  before.sleep();
  assert.deepEqual(calls, [["submit", "hello", { images: ["data:image/png;base64,AAAA"] }], ["sleep"]]);

  const turns = Array.from({ length: 11 }, (_, index) => ({ id: `t${index}`, role: index % 2 ? "jarvis" : "user", text: `turn ${index}` }));
  store.publishBubuConversation({ ready: true, turns, pending: true, error: null });
  const published = store.getBubuConversation();
  assert.equal(notified, 1);
  assert.equal(published.ready, true);
  assert.equal(published.pending, true);
  assert.equal(published.turns.length, store.MAX_BUBU_TURNS);
  assert.equal(published.turns.at(-1).id, "t10", "the newest turns are kept");
  assert.equal(published.submit, before.submit, "submit identity is stable");

  // An identical publish does not notify (no render churn while polling).
  store.publishBubuConversation({ ready: true, turns: turns.map((turn) => ({ ...turn })), pending: true, error: null });
  assert.equal(notified, 1);
  store.publishBubuConversation({ ready: true, turns, pending: false, error: "Jarvis is not available right now." });
  assert.equal(notified, 2);
  assert.equal(store.getBubuConversation().error, "Jarvis is not available right now.");

  store.publishBubuFailedPrompt("keep me");
  assert.equal(store.getBubuConversation().failedPrompt, "keep me");
  store.publishBubuFailedPrompt(null);
  assert.equal(store.getBubuConversation().failedPrompt, null);

  unsubscribe();
  store.publishBubuConversation({ ready: false, turns: [], pending: false, error: null });
  assert.equal(notified, 4, "no notifications after unsubscribe");

  unregister();
  assert.equal(store.getBubuConversation().ready, false, "unregistering resets the snapshot");
  assert.equal(store.getBubuConversation().turns.length, 0);
}

// ---- pose is bound to real state ----------------------------------------------------------------------------------
{
  assert.equal(bubuPose({}), "listening");
  assert.equal(bubuPose({ pending: true }), "thinking");
  assert.equal(bubuPose({ asleep: true, pending: true, latestReply: "Really?" }), "sleeping", "sleeping wins until Bubu is clicked");
  assert.equal(bubuPose({ latestReply: "Which stage do you mean?" }), "clarifying");
  assert.equal(bubuPose({ latestReply: "Which stage do you mean?  \n" }), "clarifying");
  assert.equal(bubuPose({ latestReply: "Do you mean (the pump)?" }), "clarifying");
  assert.equal(bubuPose({ latestReply: "Done. The plant has 3 stages." }), "listening");
  assert.equal(bubuPose({ latestReply: "Which stage do you mean?", pending: true }), "thinking", "nothing may be pending for clarifying");
  assert.equal(bubuPose({ latestReply: null }), "listening");
  assert.equal(bubuPose({ dictating: true, pending: true, latestReply: "Why?" }), "listening", "active dictation shows the listening pose");
  assert.equal(bubuPose({ asleep: true, dictating: true }), "sleeping");
  assert.equal(bubuPose({ error: "The server did not answer." }), "listening", "an error is text in the bubble, not the issue-report pose");
  assert.deepEqual([...BUBU_POSES].sort(), ["clarifying", "listening", "sending-issue", "sleeping", "thinking"]);
  for (const pose of BUBU_POSES) assert.match(bubuPoseSrc(pose), /^\/mascots\/bubu\/bubu-[a-z-]+\.webp$/);
}

// ---- placement: above by default, flips, stays in the viewport -----------------------------------------------------
{
  const size = { width: 360, height: 300 };
  const screen = { width: 1440, height: 900 };
  const corner = placeBubble({ left: 1332, top: 792, width: 96, height: 96 }, size, screen);
  assert.equal(corner.placement, "top");
  assert.ok(corner.left + size.width <= screen.width - 12 && corner.left >= 12);
  assert.ok(corner.top + size.height < 792, "bubble sits above Bubu");

  const topLeft = placeBubble({ left: 12, top: 12, width: 96, height: 96 }, size, screen);
  assert.equal(topLeft.placement, "bottom", "flips below when there is no room above");
  assert.ok(topLeft.top >= 108);
  assert.ok(topLeft.left >= 12);

  const squeezed = placeBubble({ left: 600, top: 150, width: 96, height: 96 }, { width: 360, height: 700 }, { width: 1440, height: 900 });
  assert.ok(squeezed.placement === "left" || squeezed.placement === "right", "tall bubble goes beside Bubu");
  assert.ok(squeezed.top >= 12 && squeezed.top + 700 <= 900 - 12);

  for (const anchor of [{ left: 0, top: 0 }, { left: 1344, top: 0 }, { left: 0, top: 804 }, { left: 1344, top: 804 }, { left: 700, top: 400 }]) {
    for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
      const width = Math.min(360, viewport.width - 24);
      const result = placeBubble({ ...anchor, width: 96, height: 96 }, { width, height: 280 }, viewport);
      assert.ok(result.left >= 12 && result.left + width <= viewport.width - 12, `horizontal fit ${JSON.stringify([anchor, viewport])}`);
      assert.ok(result.top >= 12 && result.top + 280 <= viewport.height - 12, `vertical fit ${JSON.stringify([anchor, viewport])}`);
      assert.ok(result.tail >= 22 - 1e-9, "tail stays inside the rounded corners");
    }
  }
}

// ---- the bubble is portalled, on Bubu's layer, and drawn from tokens ---------------------------------------------------
{
  const bubble = read("src/components/bubu/BubuBubble.tsx");
  const css = read("src/components/bubu/BubuBubble.css");
  const launcherCss = read("src/components/shell/BubuLauncher.css");
  assert.match(bubble, /createPortal\([\s\S]*document\.body\)/, "the bubble is portalled to document.body");
  const launcherZ = Number(/z-index:\s*(\d+)/.exec(launcherCss)[1]);
  const bubbleZ = Number(/#bubu-bubble\s*\{[^}]*?z-index:\s*(\d+)/.exec(css)[1]);
  assert.equal(launcherZ, 40);
  assert.ok(bubbleZ >= 41 && bubbleZ <= 45, `bubble z-index ${bubbleZ} must sit between Bubu (40) and modals / solver console (60)`);
  assert.match(css, /backdrop-filter:\s*blur/, "green translucency with blur");
  assert.match(css, /--color-accent-surface/, "bubble uses the app's accent tokens");
  assert.match(css, /max-height:\s*55vh/);
  assert.match(css, /prefers-reduced-motion/);
  // Contract with the rest of BUBU-2.
  assert.match(bubble, /extraTools/);
  assert.match(bubble, /getExtras/);
  assert.match(bubble, /renderReply/);
  assert.match(bubble, /event\.key === "Escape"/, "Esc closes");
  assert.match(bubble, /event\.key === "Enter" && !event\.shiftKey/, "Enter sends, Shift+Enter is a newline");
  assert.match(bubble, /Select a project/);
  for (const label of ["Send", "Expand", "Sleep", "Close"]) assert.match(bubble, new RegExp(`aria-label="${label}"`), `${label} control`);
  assert.match(bubble, /data-bubu-ui="bubble"/, "page description can skip Bubu's own UI");
}

// ---- Layout: Bubu opens the bubble; Expand opens the Sidecar ------------------------------------------------------------
{
  const layout = read("src/components/Layout.tsx");
  assert.match(layout, /<BubuLauncher buttonRef=\{sidecarToggleRef\}[^>]*onActivate=\{toggleBubble\}/);
  assert.doesNotMatch(layout, /onActivate=\{activateJarvis\}/);
  const toggle = /const toggleBubble = \(\) => \{[\s\S]*?\n  \};/.exec(layout)?.[0] ?? "";
  assert.ok(toggle.length > 0, "toggleBubble exists");
  assert.doesNotMatch(toggle, /setSidecarOpen\(true\)/, "clicking Bubu no longer opens the Sidecar");
  assert.match(toggle, /setBubbleOpen/);
  assert.match(toggle, /setAsleep\(false\)/, "clicking a sleeping Bubu wakes it");
  const expand = /const expandJarvis = \(\) => \{[\s\S]*?\n  \};/.exec(layout)?.[0] ?? "";
  assert.match(expand, /setSidecarOpen\(true\)/, "Expand opens the Sidecar");
  assert.match(expand, /setBubbleOpen\(false\)/, "Expand closes the bubble");
  assert.match(expand, /getElementById\("jarvis-prompt"\)/, "pages that own their chat keep focusing the in-page composer");
  assert.match(layout, /<BubuBubble open=\{bubbleOpen\}[\s\S]*?onExpand=\{expandJarvis\}[\s\S]*?onSleep=\{sleepBubu\}/);
  assert.match(layout, /const sleepBubu = \(\) => \{ setBubbleOpen\(false\); setAsleep\(true\);/);
  assert.match(layout, /bubu\?\.listening/, "the integrator can show the listening pose");
}

// ---- the conversation owner publishes into the store and forwards the BUBU-2 fields -----------------------------------
{
  const hook = read("src/components/ai/useJarvisSidecar.tsx");
  assert.match(hook, /publishBubuConversation\(\{ ready: Boolean\(workspaceId\), turns: bubuTurns, pending: bubuPending, error \}\)/);
  assert.match(hook, /registerBubuHandlers\(/);
  assert.match(hook, /sleep: \(\) => \{[^}]*selectThread\(null\)/, "sleep starts the next request in a new thread");
  assert.match(hook, /submit: \(promptText, extras\) => sendPrompt\(promptText\.trim\(\), extras \?\? \{\}\)/, "the bubble reuses the panel's submit path");
  assert.match(hook, /ui_context: captured\.extras\?\.ui_context, images: captured\.extras\?\.images/);
  assert.match(hook, /if \(!fromBubble\) setPrompt\(""\)/, "a bubble submit leaves the panel's draft alone");
  const api = read("src/api/threads.ts");
  assert.match(api, /ui_context\?: ThreadUiContext; images\?: string\[\]/);
  assert.match(api, /\.\.\.\(options\?\.ui_context \? \{ ui_context: options\.ui_context \} : \{\}\)/);
  assert.match(api, /\.\.\.\(options\?\.images\?\.length \? \{ images: options\.images \} : \{\}\)/);
  const launcher = read("src/components/shell/BubuLauncher.tsx");
  assert.match(launcher, /bubuPoseSrc\(pose\)/, "Bubu's artwork follows the pose");
  assert.match(launcher, /publishBubuRect\(/, "the bubble can follow Bubu while it is dragged");
  assert.match(launcher, /listening\?: boolean/);
}

console.log("BUBU-2 bubble checks passed");
