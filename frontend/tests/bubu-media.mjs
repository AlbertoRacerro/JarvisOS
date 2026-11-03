import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { dictationErrorText, mergeResults } from "../src/components/bubu/useDictation.ts";
import { captureErrorText, fitSize } from "../src/components/bubu/captureTab.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dictationPath = path.join(root, "src/components/bubu/useDictation.ts");
const capturePath = path.join(root, "src/components/bubu/captureTab.ts");
const dictationSource = fs.readFileSync(dictationPath, "utf8");
const captureSource = fs.readFileSync(capturePath, "utf8");

const result = (text, isFinal) => ({ isFinal, 0: { transcript: text } });

assert.equal(dictationErrorText("not-allowed"), "Microphone blocked");
assert.equal(dictationErrorText("service-not-allowed"), "Microphone blocked");
assert.equal(dictationErrorText("no-speech"), "No speech heard");
assert.equal(dictationErrorText("audio-capture"), "No microphone");
assert.equal(dictationErrorText("network"), "Dictation needs a connection");
assert.equal(dictationErrorText("aborted"), "Dictation stopped");
assert.equal(dictationErrorText(""), "Dictation stopped");

assert.deepEqual(
  mergeResults([result(" ciao", true), result(" come stai", true), result("bene", false)]),
  { final: "ciao come stai", interim: "bene" },
);
assert.deepEqual(mergeResults([result("uno", true), result("due", true)], 1), { final: "due", interim: "" });
assert.deepEqual(mergeResults([result("ciao", false)]), { final: "", interim: "ciao" });
assert.deepEqual(mergeResults([result("   ", true), result("  ", false)]), { final: "", interim: "" });
assert.deepEqual(mergeResults([]), { final: "", interim: "" });

assert.deepEqual(fitSize(3000, 2000, 1600), { width: 1600, height: 1067 });
assert.deepEqual(fitSize(1600, 900, 1600), { width: 1600, height: 900 });
assert.deepEqual(fitSize(800, 600, 1600), { width: 800, height: 600 });
assert.deepEqual(fitSize(640, 480, 5000), { width: 640, height: 480 });
assert.deepEqual(fitSize(0, 0, 1600), { width: 0, height: 0 });
const scaled = fitSize(2560, 1440, 1600);
assert.equal(scaled.width, 1600);
assert.ok(Math.abs(scaled.width / scaled.height - 2560 / 1440) < 0.01, "aspect ratio kept");

assert.equal(captureErrorText({ name: "NotAllowedError" }), "Screenshot cancelled");
assert.equal(captureErrorText({ name: "AbortError" }), "Screenshot cancelled");
assert.equal(captureErrorText({ name: "NotSupportedError" }), "Screenshots aren't supported here");
assert.equal(captureErrorText(new Error("boom")), "Screenshot failed");
assert.equal(captureErrorText(null), "Screenshot failed");

assert.match(dictationSource, /w\.SpeechRecognition \|\| w\.webkitSpeechRecognition/);
assert.match(dictationSource, /continuous = true/);
assert.match(dictationSource, /interimResults = true/);
assert.doesNotMatch(dictationSource, /from\s+["']\.\.?\//, "dictation module must not import local files");

assert.match(captureSource, /getDisplayMedia\(/);
assert.match(captureSource, /preferCurrentTab: true/);
assert.match(captureSource, /selfBrowserSurface: "include"/);
assert.match(captureSource, /getTracks\(\)[\s\S]*?\.stop\(\)/, "capture must stop all tracks");
assert.match(captureSource, /maxWidth \?\? 1600/);
assert.match(captureSource, /quality \?\? 0\.85/);
assert.doesNotMatch(captureSource, /from\s+["']\.\.?\//, "capture module must not import local files");

console.log("BUBU-MEDIA dictation and screenshot acceptance: PASS");
