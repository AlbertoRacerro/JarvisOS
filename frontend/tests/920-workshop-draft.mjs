import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const fail = (message) => { console.error(`920 workshop draft failed: ${message}`); process.exit(1); };
const panel = read("src/components/studio/WorkshopDraftPanel.tsx");
const studio = read("src/pages/Studio.tsx");
const api = read("src/api/studio.ts");
for (const token of ['role="region"', 'aria-label="Workshop draft"', 'aria-live="polite"', "sequence.current === mine", "clearTimeout", "AI draft", "Deterministic scaffold — AI unavailable:", "Proposal only"]) if (!panel.includes(token)) fail(`panel missing ${token}`);
if (!api.includes("/workshop-draft")) fail("api call missing");
if (!studio.includes("<WorkshopDraftPanel") || !studio.includes("acceptedSource")) fail("Studio must render the panel and gate on explicit acceptance");
if (!/embedded && acceptedSource \? await saveStudioModel/.test(studio)) fail("accepted source must go through the normal draft save after creation");
console.log("920 workshop draft passed");
