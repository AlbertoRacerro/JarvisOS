import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/components/ai/useJarvisSidecar.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("../src/components/ai/JarvisSidecar.css", import.meta.url), "utf8");
assert.match(source, /interaction\.verification_summary\?\.warning_count/);
assert.match(source, /jarvis-verification-warning/);
assert.match(css, /\.jarvis-verification-warning/);
console.log("366 Hermes verification badge source checks passed");
