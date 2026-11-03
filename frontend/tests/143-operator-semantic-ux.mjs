import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

const coding = readFileSync(new URL("../src/pages/CodingWorkbench.tsx", import.meta.url), "utf8");
const models = readFileSync(new URL("../src/pages/ModelDossier.tsx", import.meta.url), "utf8");
const settings = readFileSync(new URL("../src/pages/Settings.tsx", import.meta.url), "utf8");
const semanticsSource = readFileSync(new URL("../src/operatorSemantics.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(semanticsSource, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
const semantics = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

assert.match(coding, /runtime\?\.semantic_delta/);
assert.match(coding, /canonical server-owned runtime alignment/);
assert.match(coding, /Remote ahead · \{semanticDelta\.aheadBy/);
assert.match(coding, /Remote behind · \{semanticDelta\.behindBy/);
assert.doesNotMatch(coding, /<span>Ahead · \{semanticDelta\.aheadBy/);
assert.doesNotMatch(coding, /<span>Behind · \{semanticDelta\.behindBy/);
assert.match(coding, /semanticDelta\.relationship/);
assert.match(coding, /function RawJson/);
assert.match(coding, /<details><summary>Technical details<\/summary>/);
assert.doesNotMatch(coding, /<pre className="final-fusion__searchbox">\{JSON\.stringify\(prEvidence/);
assert.doesNotMatch(coding, /<pre className="final-fusion__searchbox">\{JSON\.stringify\(pipeline/);
assert.doesNotMatch(coding, /<span>›<\/span>/);
assert.doesNotMatch(models, /<span>›<\/span>/);
assert.match(models, /plainDisclosureRowStyle/);
assert.match(settings, /providerName\(provider\.provider_id\)/);
assert.match(settings, /Provider code · \{provider\.provider_id\}/);
assert.match(settings, /<details><summary>Technical details<\/summary>/);
assert.doesNotMatch(settings, /<strong>\{provider\.provider_id\}<\/strong>/);
assert.match(coding, /value\.partial === true/);
assert.match(coding, /Array\.isArray\(value\.warnings\)/);
assert.doesNotMatch(coding, /const state = value\.state \?\? value\.status \?\? "available"/);
assert.match(coding, /changes requested/);
assert.doesNotMatch(coding, /summary\.reviews\.blocking/);

const runtime = semantics.runtimeDeltaSummary({ relation: "ahead", ahead_by: 3, behind_by: 1, files: [{ filename: "frontend/a.ts", status: "modified" }], status: "available", partial: true }, "local_behind");
assert.deepEqual(runtime, { relation: "local_behind", relationship: "Local runtime behind remote", aheadBy: 3, behindBy: 1, files: [{ name: "frontend/a.ts", status: "modified" }], status: "available", partial: true, explanation: null });
const aligned = semantics.runtimeDeltaSummary({ relation: "divergent", status: "unavailable", files: [] }, "aligned", null);
assert.equal(aligned.relation, "aligned");
assert.equal(aligned.relationship, "Local runtime matches remote");
const divergent = semantics.runtimeDeltaSummary({ relation: "ahead", ahead_by: 1, behind_by: 1, files: [] }, "divergent", null);
assert.equal(divergent.relation, "divergent");
assert.equal(divergent.relationship, "Local and remote diverge");
const unknown = semantics.runtimeDeltaSummary({ status: "unavailable", files: [] }, "unknown", "worktree_dirty");
assert.equal(unknown.relation, "unknown");
assert.equal(unknown.relationship, "Runtime relationship unavailable");
assert.match(unknown.explanation, /Worktree Dirty/);
const invalidatedDelta = semantics.runtimeDeltaSummary({ relation: "ahead", ahead_by: 4, behind_by: 0, files: [{ filename: "stale.ts", status: "modified" }], status: "available", partial: true }, "unknown", "target_moved");
assert.deepEqual(invalidatedDelta, { relation: "unknown", relationship: "Runtime relationship unavailable", aheadBy: null, behindBy: null, files: [], status: "unavailable", partial: false, explanation: "Target Moved" });
assert.doesNotMatch(semanticsSource, /ahead_count|changed_file_count|execution_class === "local"/);

const evidence = semantics.pullRequestEvidenceSummary({
  pr: { number: 589, title: "Semantic repair", state: "closed" },
  checks: { check_runs: [{ status: "completed", conclusion: "failure", stale: false }, { status: "completed", conclusion: "success", stale: true }] },
  reviews: { reviews: [{ state: "CHANGES_REQUESTED", stale: false }, { state: "APPROVED", stale: true }] }
});
assert.equal(evidence.title, "Semantic repair");
assert.equal(evidence.state, "closed");
assert.deepEqual(evidence.checks, { total: 2, passing: 0, failing: 1, pending: 0, stale: 1 });
assert.deepEqual(evidence.reviews, { total: 2, approved: 0, changesRequested: 1, stale: 1 });

assert.equal(semantics.providerLocation(false, "synthetic"), "Runs without an external service");
assert.equal(semantics.providerLocation(false, "local_compute"), "Runs locally");
assert.equal(semantics.providerLocation(true, "external_provider"), "Uses a network service");
assert.match(semantics.savedPaidAiSummary(false, 0), /saved settings/);
assert.match(settings, /Pending unsaved draft; current permission is unchanged until Save/);
const credentialCases = [
  ["environment", "corrupted", /Environment credential active.*Stored credential damaged/],
  ["invalid", "usable", /Environment credential invalid.*Stored credential ready/],
  ["secure_persisted", "usable", /Securely stored credential active.*Stored credential ready/],
  ["absent", "absent", /No effective credential.*No stored credential/],
  ["unknown", "unavailable", /Credential availability unknown.*Secure credential store unavailable/]
];
for (const [source, persisted, expected] of credentialCases) {
  const meaning = semantics.credentialMeaning(source, persisted);
  assert.match(meaning, expected);
  for (const code of ["secure_persisted", "not_supported"]) assert.doesNotMatch(meaning, new RegExp(code));
}
const rawJsonBody = coding.match(/function RawJson[\s\S]*?\n}/)?.[0] ?? "";
assert.match(rawJsonBody, /<TechnicalDetails>/);
assert.match(rawJsonBody, /JSON\.stringify/);

console.log("spec 143 operator semantic UX contract passed");
