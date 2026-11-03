import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const foundation = fs.readFileSync(path.join(root, "src/styles/foundation.css"), "utf8");
const eyebrowRule = foundation.match(/\.eyebrow\s*\{[\s\S]*?\}/)?.[0] ?? "";

if (!eyebrowRule.includes("color: var(--color-accent-foreground)")) {
  console.error("VISUAL-IDENTITY-1 eyebrow contrast acceptance failed: generic eyebrow text must use the contrast-safe accent foreground role");
  process.exit(1);
}

if (eyebrowRule.includes("--color-accent-hover") || eyebrowRule.includes("--accent-seed")) {
  console.error("VISUAL-IDENTITY-1 eyebrow contrast acceptance failed: generic eyebrow text must not consume raw Custom accent fill roles");
  process.exit(1);
}

console.log("VISUAL-IDENTITY-1 eyebrow contrast acceptance: PASS");
