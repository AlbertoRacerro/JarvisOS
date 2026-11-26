import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const consultCardSource = fs.readFileSync(path.join(root, "src/components/ai/ConsultCard.tsx"), "utf8");
const consultApiSource = fs.readFileSync(path.join(root, "src/api/consult.ts"), "utf8");
const threadsApiSource = fs.readFileSync(path.join(root, "src/api/threads.ts"), "utf8");
const cssSource = fs.readFileSync(path.join(root, "src/components/ai/JarvisSidecar.css"), "utf8");

// Spec 383: ConsultCard feedback buttons
assert.ok(consultCardSource.includes("Utile"), "ConsultCard must contain 'Utile' button");
assert.ok(consultCardSource.includes("Non utile"), "ConsultCard must contain 'Non utile' button");
assert.ok(consultCardSource.includes("submitConsultFeedback"), "ConsultCard must call submitConsultFeedback");
assert.ok(consultCardSource.includes("data-testid=\"consult-feedback\""), "ConsultCard must render consult-feedback container");
assert.ok(consultCardSource.includes("data-testid=\"consult-feedback-useful\""), "ConsultCard must have useful button test id");
assert.ok(consultCardSource.includes("data-testid=\"consult-feedback-not-useful\""), "ConsultCard must have not-useful button test id");

// Spec 383: consult.ts API functions
assert.ok(consultApiSource.includes("submitConsultFeedback"), "consult.ts must export submitConsultFeedback");
assert.ok(consultApiSource.includes("fetchConsultFeedback"), "consult.ts must export fetchConsultFeedback");
assert.ok(consultApiSource.includes("/feedback"), "consult.ts must post to /feedback endpoint");

// Spec 383: threads.ts ConsultBlock type
assert.ok(threadsApiSource.includes("feedback_verdict"), "threads.ts ConsultBlock must include feedback_verdict");

// Spec 383: CSS styles
assert.ok(cssSource.includes(".consult-card__feedback"), "JarvisSidecar.css must include .consult-card__feedback");
assert.ok(cssSource.includes(".consult-btn--feedback"), "JarvisSidecar.css must include .consult-btn--feedback");
assert.ok(cssSource.includes(".consult-btn--useful"), "JarvisSidecar.css must include .consult-btn--useful");
assert.ok(cssSource.includes(".consult-btn--not-useful"), "JarvisSidecar.css must include .consult-btn--not-useful");

console.log("383 consult feedback UI checks passed");
