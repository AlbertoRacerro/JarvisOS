// Source-level guard regression only; this is not a browser concurrency test.
import assert from "node:assert/strict";
import fs from "node:fs";

const source = fs.readFileSync(new URL("../src/components/ai/useJarvisSidecar.tsx", import.meta.url), "utf8");
const submit = source.slice(source.indexOf("const submit = async"), source.indexOf("const pendingRetryReady"));
assert.match(submit, /if \([^\n]*loadingThreads[^\n]*\) return;/,
  "submit must wait for the initial thread list or explicit thread creation");
assert.match(source, /<button type="submit" disabled=\{[^}]*loadingThreads[^}]*\}/,
  "composer must expose the same loading gate as the handler");
assert.match(submit, /submitOwner\.current !== creationToken/,
  "auto-create must discard completion after conversation ownership changes");
assert.match(source, /const selectThread =[\s\S]*?submitOwner\.current \+= 1;/,
  "conversation selection must invalidate pending submit callbacks");
console.log("144 sidecar race source-level guards passed (not runtime proof).");
