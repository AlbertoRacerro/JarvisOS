import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadTrustedPlan } from "./plan-lib.mjs";

const planId = process.argv[2];
if (!planId) throw new Error("usage: node validate-plan.mjs <plan-id> [--fixture]");
const root = dirname(fileURLToPath(import.meta.url));
const plan = await loadTrustedPlan(planId, join(root, "plans"));
if (process.argv[3] === "--fixture") process.stdout.write(`${plan.fixture ?? "none"}\n`);
else if (process.argv.length > 3) throw new Error("unsupported validator argument");
else process.stdout.write(`${plan.id}\n`);
