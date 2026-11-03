import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { latestReply, pickImages, pickedMention, withPicked } from "../src/components/bubu/bubuExtras.ts";

// BUBU-2 wiring: the page is always described, screenshots only on request, picked elements reach the model first,
// and the newest finished reply drives the pointer.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");

const page = { route: "/design/project", title: "Design", targets: [{ id: "a", role: "button", name: "Run" }, { id: "b", role: "tab", name: "CFD" }] };
assert.deepEqual(withPicked(page, [{ id: "b", role: "tab", name: "CFD" }]).targets.map((t) => t.id), ["b", "a"], "picked first, no duplicate");
assert.equal(withPicked(page, [{ id: "z", role: "button", name: "Z" }], 2).targets.length, 2, "cap kept");

assert.equal(pickImages(null, null), undefined, "no images unless asked");
assert.deepEqual(pickImages("shot", "frame"), ["shot", "frame"]);
assert.deepEqual(pickImages(null, "frame"), ["frame"]);

const turns = [{ id: "1", role: "user", text: "where?" }, { id: "2", role: "jarvis", text: "Here [[point:a]]" }];
assert.equal(latestReply(turns)?.id, "2");
assert.equal(latestReply([...turns, { id: "3", role: "user", text: "and now?" }]), null, "a new question clears the pointer");
assert.equal(latestReply([...turns.slice(0, 1), { id: "2", role: "jarvis", text: "", pending: true }]), null);
assert.equal(pickedMention({ id: "a", role: "button", name: "Run" }), "“Run”");

const hook = read("src/components/bubu/useBubuIntegration.tsx");
for (const needle of ["collectPageContext()", "openTabStream()", "useDictation(", "useUserPointer(", "parsePointDirectives(", "<BubuPointer points={points}"]) assert.ok(hook.includes(needle), needle);
assert.match(hook, /const onSleep = \(\) => \{ stopVision\(\);/, "Sleep turns vision off");
const app = read("src/App.tsx");
assert.match(app, /bubu=\{bubu\.integration\}>\{bubu\.overlay\}/, "App wires Bubu into the shell");
const layout = read("src/components/Layout.tsx");
assert.match(layout, /draftInsert=\{bubu\?\.draftInsert\} status=\{bubu\?\.status\}/);
assert.match(layout, /bubu\?\.onSleep\?\.\(\)/);
assert.match(read("src/components/bubu/captureTab.ts"), /export async function openTabStream/);

console.log("bubu integration: ok");
