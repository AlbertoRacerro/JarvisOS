import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = path => readFileSync(resolve(root, path), "utf8");

const page = read("src/pages/CodingWorkbench.tsx");
const api = read("src/api/coding.ts");

// The plan type on ControlRoom: optional-safe so an older backend does not crash the page.
assert.ok(api.includes("plan?: ControlRoomPlan"), "ControlRoom must carry an optional plan");
assert.ok(api.includes('Readonly<{ status: "absent" }>'), "plan type missing the absent case");
assert.ok(api.includes('Readonly<{ status: "invalid" }>'), "plan type missing the invalid case");
assert.ok(api.includes('status: "available"; goal: string; updated_at: string; steps: ControlRoomPlanStep[]'), "plan type missing the available case");
assert.ok(api.includes('state: "done" | "in_progress" | "next" | "later"'), "plan step states are not the four file states");
assert.ok(api.includes("note: string | null"), "plan step note must be nullable text");
assert.ok(api.includes("specs: ControlRoomSpecRef[]"), "plan steps join the registry through the shared spec ref");

// Il percorso: the heading, the four Italian state labels and the invalid line.
assert.ok(page.includes("function PlanPath("), "PlanPath component is missing");
assert.ok(page.includes('aria-label="Il percorso"'), "the plan block must be labelled Il percorso");
assert.ok(page.includes("<h4>Il percorso</h4>"), "the plan block must carry the Il percorso heading");
for (const [state, label] of [["done", "Fatto"], ["in_progress", "In corso"], ["next", "Prossimo"], ["later", "Dopo"]]) {
  assert.ok(page.includes(`${state}: "${label}"`), `state ${state} must render the label ${label}`);
}
assert.ok(page.includes("Piano non leggibile."), "invalid plan must show Piano non leggibile.");
assert.ok(page.includes('spec.title ?? "non nel registro"'), "a spec without a registry title shows non nel registro");
assert.ok(page.includes("<StateBadge status={spec.status} />"), "spec chips reuse the registry StateBadge");
assert.ok(page.includes("plan_invalid:"), "the warnings text map must name plan_invalid");

// Absent or missing plan renders nothing; the plan text stays plain text.
assert.ok(page.includes('if (!plan || plan.status === "absent") return null;'), "absent or missing plan must render nothing");
assert.ok(!page.includes("dangerouslySetInnerHTML"), "the plan is plain text, never injected HTML");

// The plan block sits inside the Up next section, right after its header and before the STATUS rows.
const section = page.indexOf('aria-label="Up next"');
assert.ok(section > -1, "the Up next section is missing");
const header = page.indexOf("<header>", section);
const planUse = page.indexOf("<PlanPath plan={room?.plan} />", section);
const authorized = page.indexOf("upcoming.authorized", section);
const sectionEnd = page.indexOf("</section>", section);
assert.ok(header > section, "the Up next header is missing");
assert.ok(planUse > header, "PlanPath must render after the Up next header");
assert.ok(authorized > planUse, "PlanPath must render before the authorized list");
assert.ok(sectionEnd > authorized, "the plan block and the STATUS rows must stay inside the Up next section");

console.log("336 up next plan: PASS");
