import assert from "node:assert/strict";
import fs from "node:fs";
import {
  branchExplanation, entrySi, failureTouchesUnit, nSourceNote, pinDescription, formatReported, formatSig, gasTransferWords, growthVersusDilution, liquidVolumeM3, missingSetSymbols,
  nSourceLabel, pbrCardRefusal, pbrFailureHeading, pbrFieldError, pinStatus, setMatchesCard, cardOnLatestSet, severityWord, verificationChip,
} from "../src/components/process/pbrLogic.ts";
import { ownerLabel, ownerShort } from "../src/components/process/processOwners.ts";
import { actionSummaryText, describeProcessAction, isToolCallShaped } from "../src/components/ai/workspaceActionPresentation.ts";

const read = (path) => fs.readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
const editor = read("stages/ProcessDraftEditor.tsx");
const editorCss = read("stages/ProcessDraftEditor.css");
const inspector = read("components/process/PbrInspector.tsx");
const picker = read("components/process/PbrModelPicker.tsx");
const resultsView = read("components/process/PbrResults.tsx");
const library = read("components/process/BiologyModelLibrary.tsx");
const cards = read("components/ai/WorkspaceActionCards.tsx");
const api = read("api/processDraft.ts");
const pbrCss = read("components/process/PbrInspector.css");
const pkg = JSON.parse(fs.readFileSync(new URL("../package.json", import.meta.url), "utf8"));

// ---- palette and owner-driven badges: no owner words are hard-coded in the editor
assert.match(editor, /registry\.units\.some\(\(unit\) => unit\.owner === "jarvis_bio"\)/);
assert.match(editor, /<h3>Jarvis units<\/h3>/);
assert.match(editor, /registry\.units\.filter\(\(unit\) => unit\.owner === "jarvis_bio"\)/);
assert.match(editor, /PhotobioreactorT1: "PBR"/);
assert.doesNotMatch(editor, /\?\s*"Jarvis"\s*:\s*"DWSIM"/, "owner badge ternary must be gone");
assert.doesNotMatch(editor, />\s*DWSIM units\s*</, "palette heading must come from the registry owner");
assert.doesNotMatch(editor, /\?\s*"DWSIM"/);
assert.match(editor, /ownerShort\(spec\?\.owner\)/);
assert.match(editor, /ownerLabel\(spec\.owner\)/);
assert.equal(ownerLabel("jarvis_bio"), "Jarvis · biology");
assert.equal(ownerShort("jarvis_bio"), "Jarvis");
assert.equal(ownerLabel("dwsim"), "DWSIM");
assert.equal(ownerLabel("somebody_else"), "Somebody else");
assert.equal(ownerLabel(undefined), "Unknown owner");
assert.match(editor, /<PbrInspector[\s\S]*findings=\{draft\.findings\}/);
assert.match(editor, /unitFailure\(unit\.tag\)/);
assert.match(editor, /failureTouchesUnit\(solve, tag\)/);
// 168 records a Jarvis-unit failure under failed_segment with an empty failed_units; DWSIM failures list tags.
assert.equal(failureTouchesUnit({ failed_segment: "PBR-1", failed_units: [] }, "PBR-1"), true);
assert.equal(failureTouchesUnit({ failed_segment: "mixed-1-0", failed_units: ["PBR-1"] }, "PBR-1"), true);
assert.equal(failureTouchesUnit({ failed_segment: "Separator", failed_units: [] }, "PBR-1"), false);
assert.equal(failureTouchesUnit({}, "PBR-1"), false);
assert.equal(failureTouchesUnit(undefined, "PBR-1"), false);
// provenance in words, ids only in tooltips
assert.equal(nSourceNote("NH3"), "N source assumed: NH3; inlet N speciation unverified");
assert.equal(nSourceNote("HNO3"), "N source assumed: HNO3; inlet N speciation unverified");
assert.equal(pinDescription({ card_name: "N. gaditana", card_revision: "r-0123456789abcdef", set_name: "Set A", set_revision: "r-fedcba9876543210" }),
  "N. gaditana, revision 01234567 · parameter set Set A, revision fedcba98");
assert.match(resultsView, /nSourceNote\(result\.model_pin\.n_source\)/);
assert.match(resultsView, /pinDescription\(result\.model_pin\)/);
assert.match(inspector, /feed basis/);
// Before a Run (and without a feed basis) Q and HRT read the one word `empty`, never an invented value.
assert.match(inspector, /\(feed basis\)` : "empty"/);
assert.match(editor, /feedBasis=\{draft\.pbr_feed_basis\?\.\[unit\.tag\]\}/);
assert.match(editor, /Culture · Jarvis/, "outlet stream keeps its Culture · Jarvis fieldset");
assert.match(editor, /!feed && cultureResult/, "culture fieldset is owner-independent");
assert.match(editorCss, /draft-body--wide-inspector[\s\S]*380px/);

// ---- tablist semantics
for (const text of ['role="tablist"', 'role="tab"', 'role="tabpanel"', "aria-selected", "aria-controls", "aria-labelledby", "tabIndex={tab === name ? 0 : -1}", '"ArrowRight"', '"ArrowLeft"', '"Home"', '"End"', "hidden={tab !== name}"])
  assert.ok(inspector.includes(text), `tab pattern missing ${text}`);
for (const name of ["Overview", "Geometry", "Biology", "Operation", "Light & environment", "Results"]) assert.ok(inspector.includes(`"${name}"`), name);
assert.match(inspector, /TABS = \["Overview", "Geometry", "Biology", "Operation", "Light & environment", "Results"\]/);
assert.match(inspector, /param\.group === name/, "parameters come from the registry group");
assert.match(inspector, /set_unit_model/);
assert.match(inspector, /model: card \? \{ card_id: card\.id, card_revision: card\.revision, card_digest: card\.digest \} : null/);
assert.match(inspector, /set_unit_params/);
assert.match(inspector, /result\?\.branch \?[^\n]*: "empty"/, "the branch reads empty before a Run");
assert.match(inspector, /Stale · inputs changed since this Run/);
assert.match(inspector, /QuantityInput/);

// ---- picker: no raw HTML, shared allowlisted MathML, human revisions, explicit adoption
for (const source of [picker, inspector, resultsView]) assert.doesNotMatch(source, /dangerouslySetInnerHTML|innerHTML\s*=/);
assert.match(picker, /MathTree/);
assert.match(library, /allowedMathTags/);
for (const text of ["Use model card", "Clear model card", "A newer revision is available", "Adopt newer revision", "No model cards", "Browse models…", "Factors and equations", "Verification of"])
  assert.ok(picker.includes(text), `picker missing ${text}`);
assert.match(picker, /setCurrent \? <Chips set=\{set\} \/>/, "latest set chips must not be presented as pinned values");
assert.match(picker, /onClick=\{\(\) => onPin\(pinnedCard\)\}/);
assert.match(picker, /title=\{`card \$\{pin\.card_id\}/, "raw ids stay in a tooltip, not the primary text");
assert.match(picker, /disabled=\{Boolean\(refusal\) \|\| current\}/);
assert.doesNotMatch(picker, /useEffect/, "adoption is never triggered by an effect");
assert.match(inspector, /if \(libraryWasOpen\.current && !libraryOpen\) reload\(\)/, "closing the library refreshes the picker");
assert.match(editor, /libraryOpen=\{biologyOpen\}/);

// ---- results groups and labels
for (const text of ["Results · Jarvis", "Steady state", "Residence time", "Culture", "Nitrogen, oxygen and light", "Hydraulics", "Unit balances", "Numerics", "Findings", "Caveats", "Generation allowance", "Tolerance", "Residual", "Fidelity", "Inputs changed since this Run"])
  assert.ok(resultsView.includes(text), `results missing ${text}`);
assert.match(resultsView, /<details className="pbr-numerics">/);
assert.match(resultsView, /result\.caveats!\.map/);
assert.match(resultsView, /result\.findings!\.map/);
assert.match(resultsView, /pbrFailureHeading\(failure\.code\)/);
assert.match(api, /findings\?: Finding\[\]/);
assert.match(api, /generation_allowance/);
assert.match(pbrCss, /\.pbr-results\.is-stale/);
assert.doesNotMatch(pbrCss + editorCss, /overflow-x:\s*scroll/);

// ---- set editor and cylinder form
assert.match(library, /k_X: "Specific light extinction k_X — PBR input"/);
for (const key of ["a", "b", "c", "d", "w_ash"]) assert.match(library, new RegExp(`${key}: "[^"]*PBR input"`), key);
assert.match(library, /selected\.set\("k_X"/);
assert.match(library, /for \(const key of \["a", "b", "c", "d"\]\) selected\.set\(key/);
assert.match(library, /selected\.set\("w_ash"/);
assert.match(library, /optics\.cylinder_beam_diffuse_response_average/);
assert.match(library, /evaluated inside Photobioreactor \(T1\)/i);
assert.doesNotMatch(library, /\["light\.monod", "light\.haldane"[^\]]*cylinder/);
assert.doesNotMatch(library.slice(library.indexOf("Model card builder")), /optics\.cylinder/);

// ---- agent presentation
assert.equal(describeProcessAction({ op: "set_unit_model", unit: "PBR-1", card: "N. gaditana T1" }), "Use model card N. gaditana T1 for PBR-1");
assert.equal(describeProcessAction({ op: "set_unit_model", unit: "PBR-1" }), "Clear the model card for PBR-1");
assert.equal(actionSummaryText({ summary: "", request: { actions: [{ op: "set_unit_model", unit: "PBR-1", card: "Card A" }] } }), "Use model card Card A for PBR-1");
assert.equal(actionSummaryText({ summary: "Use model card B for PBR-2", request: {} }), "Use model card B for PBR-2");
assert.equal(isToolCallShaped('{"op":"set_unit_model","unit":"PBR-1","card":"Card A"}'), true, "never show a raw set_unit_model tool call");
assert.match(cards, /actionSummaryText\(action\)/);

// ---- logic: geometry, formatting, validation
const volume = liquidVolumeM3({ tube_count: { value: 4, unit: "dimensionless", si: 4 }, tube_inner_diameter: { value: 50, unit: "mm", si: 0.05 }, tube_length: { value: 10, unit: "m", si: 10 } });
assert.ok(Math.abs(volume - 4 * Math.PI / 4 * 0.05 ** 2 * 10) < 1e-12);
assert.equal(liquidVolumeM3({}), null);
assert.equal(liquidVolumeM3({ tube_count: { value: 0, unit: "dimensionless", si: 0 }, tube_inner_diameter: { value: 1, unit: "m", si: 1 }, tube_length: { value: 1, unit: "m", si: 1 } }), null);
assert.equal(formatSig(0.123456), "0.1235");
assert.equal(formatSig(1234.5678), "1235");
assert.equal(formatSig(12345678), "1.235×10⁷");
assert.equal(formatSig(0.0000123), "1.23×10⁻⁵");
assert.equal(formatSig(0), "0");
assert.equal(formatSig(NaN), "—");
assert.equal(formatReported("pressure_drop", { value: 45678, units: "Pa" }), "45.68 kPa");
assert.equal(formatReported("lambda_h", { value: 0.05, units: "1/h" }), "0.05 h⁻¹");
assert.equal(formatReported("pumping_power", { value: 12.3456, units: "W" }), "12.35 W");
const noneYet = () => null;
assert.equal(pbrFieldError("tube_inner_diameter", { text: "1", unit: "mm" }, noneYet), "Tube inner diameter must be between 5 mm and 0.5 m");
assert.equal(pbrFieldError("tube_inner_diameter", { text: "0.6", unit: "m" }, noneYet), "Tube inner diameter must be between 5 mm and 0.5 m");
assert.equal(pbrFieldError("tube_inner_diameter", { text: "50", unit: "mm" }, noneYet), "");
assert.equal(pbrFieldError("tube_count", { text: "2.5", unit: "dimensionless" }, noneYet), "Number of tubes must be a whole number of at least 1");
assert.equal(pbrFieldError("pump_efficiency", { text: "101", unit: "percent" }, noneYet), "Pump efficiency must be above 0 % and at most 100 %");
assert.equal(pbrFieldError("photoperiod", { text: "25", unit: "h" }, noneYet), "Photoperiod must be more than 0 and at most 24 h");
assert.equal(pbrFieldError("photoperiod", { text: "1", unit: "d" }, noneYet), "");
assert.equal(pbrFieldError("diffuse_fraction", { text: "1.5", unit: "dimensionless" }, noneYet), "Diffuse fraction must be between 0 and 1");
assert.equal(pbrFieldError("tube_length", { text: "abc", unit: "m" }, noneYet), "Enter a number");
assert.equal(pbrFieldError("tube_length", { text: "", unit: "m" }, noneYet), "");
// amplitude is a difference: degC and K are the same size, no offset
assert.equal(entrySi("temperature_difference", { text: "5", unit: "degC" }), 5);
assert.equal(entrySi("temperature_difference", { text: "5", unit: "K" }), 5);
assert.equal(entrySi("temperature", { text: "25", unit: "degC" }), 298.15);
assert.equal(pbrFieldError("temperature_amplitude", { text: "10", unit: "degC" }, (key) => (key === "temperature_mean" ? 300 : null)), "");
assert.match(pbrFieldError("temperature_amplitude", { text: "301", unit: "K" }, (key) => (key === "temperature_mean" ? 300 : null)), /above 0 K/);
assert.match(pbrFieldError("temperature_mean", { text: "-300", unit: "degC" }, noneYet), /above 0 K/);

// ---- logic: cards, pins and revisions
const card = (over = {}) => ({ id: "c1", name: "Card", revision: "r-aaaaaaaaaaaaaaaa", digest: "d1", parameter_set_id: "s1", n_source: "NH3", parameter_set_revision: "r-1111111111111111",
  parameter_set_digest: "sd1", history: [{ revision: "r-aaaaaaaaaaaaaaaa", created_at: "2026-10-03T08:00:00Z" }],
  factors: { light: "light.monod", optics: "optics.slab_response_average", temperature: "temperature.ctmi", nutrients: ["nutrient.monod"], combination: "combine.liebig", loss: "loss.first_order", stoichiometry: "stoich.photoautotrophic" }, ...over });
const set = (over = {}) => ({ id: "s1", name: "Set one", revision: "r-1111111111111111", digest: "sd1", history: [{ revision: "r-1111111111111111", created_at: "2026-10-02T08:00:00Z" }], values: {}, ...over });
assert.equal(pbrCardRefusal(card()), null);
assert.match(pbrCardRefusal(card({ factors: { ...card().factors, light: "light.haldane" } }), (id) => id), /only Monod light response/);
assert.match(pbrCardRefusal(card({ factors: { ...card().factors, nutrients: ["nutrient.monod", "nutrient.droop"] } })), /Droop/);
assert.match(pbrCardRefusal(card({ factors: { ...card().factors, nutrients: ["nutrient.monod", "nutrient.monod"] } })), /exactly one Monod/);
assert.match(pbrCardRefusal(card({ factors: { ...card().factors, light: undefined } })), /no light response factor/);
assert.match(pbrCardRefusal(card({ factors: { ...card().factors, stoichiometry: undefined } })), /stoichiometry/);
const pin = { card_id: "c1", card_revision: "r-aaaaaaaaaaaaaaaa", card_digest: "d1" };
assert.deepEqual(pinStatus(pin, card(), set()), { missing: false, cardNewer: false, setNewer: false, changes: [] });
const newerCard = pinStatus(pin, card({ revision: "r-bbbbbbbbbbbbbbbb", digest: "d2", history: [{ revision: "r-bbbbbbbbbbbbbbbb", created_at: "2026-10-04T08:00:00Z" }] }), set());
assert.equal(newerCard.cardNewer, true);
assert.match(newerCard.changes[0], /pinned aaaaaaaa → now revision bbbbbbbb · 4 Oct 2026/);
const newerSet = pinStatus(pin, card(), set({ revision: "r-2222222222222222", digest: "sd2", history: [{ revision: "r-2222222222222222", created_at: "2026-10-05T08:00:00Z" }] }));
assert.equal(newerSet.setNewer, true);
assert.equal(newerSet.cardNewer, false);
assert.match(newerSet.changes[0], /built on 11111111 → now revision 22222222 · 5 Oct 2026/);
assert.equal(pinStatus(pin, undefined, undefined).missing, true);
assert.equal(pinStatus(null, card(), set()).cardNewer, false);
assert.deepEqual(missingSetSymbols(set({ values: { k_X: {}, a: {} } })), ["b", "c", "d", "w_ash"]);
assert.equal(setMatchesCard(card(), set()), true);
assert.equal(setMatchesCard(card(), set({ revision: "r-2222222222222222" })), false);
assert.equal(setMatchesCard(card(), set({ digest: "sd2" })), false);
// adopting a newer set revision reuses an identical card already built on it, never a different model
const latest = set({ revision: "r-2222222222222222", digest: "sd2" });
const onLatest = card({ id: "c2", parameter_set_revision: "r-2222222222222222", parameter_set_digest: "sd2", mu_max: { value: 0.06, unit: "1/hour" } });
const pinned = card({ mu_max: { value: 0.06, unit: "1/hour" } });
assert.equal(cardOnLatestSet([pinned, onLatest], pinned, latest)?.id, "c2");
assert.equal(cardOnLatestSet([pinned, { ...onLatest, mu_max: { value: 0.07, unit: "1/hour" } }], pinned, latest), undefined);
assert.equal(cardOnLatestSet([pinned, { ...onLatest, n_source: "HNO3" }], pinned, latest), undefined);
assert.equal(cardOnLatestSet([pinned, { ...onLatest, factors: { ...onLatest.factors, loss: "loss.light_dark" } }], pinned, latest), undefined);
assert.equal(cardOnLatestSet([pinned, { ...onLatest, parameter_set_digest: "sd1" }], pinned, latest), undefined);
assert.equal(cardOnLatestSet([pinned], pinned, set()), undefined, "the pinned card itself is never a newer-set copy");
assert.match(picker, /onClick=\{\(\) => onAdoptSet\(pinnedCard, setOf\(pinnedCard\)!\)\}>\{adopting \? "Adopting…" : "Adopt newer set revision"\}/);
assert.equal(nSourceLabel("NH3"), "N source assumed: NH₃ — inlet N speciation unverified");
assert.deepEqual(verificationChip("source_changed_since_verification"), { text: "source changed", tone: "warn" });
assert.deepEqual(verificationChip("expert_reviewed"), { text: "expert reviewed", tone: "good" });
assert.equal(verificationChip("candidate").text, "candidate");

// ---- logic: result wording
assert.equal(pbrFailureHeading("PBR_NONPHYSICAL_STATE"), "Non-physical state");
assert.equal(pbrFailureHeading("PBR_SOMETHING_NEW"), "Something new");
assert.equal(pbrFailureHeading(null), "Unit failed");
assert.match(branchExplanation("washout"), /Washout/);
assert.match(branchExplanation("productive"), /Productive/);
assert.match(growthVersusDilution(0.05, 0.1), /smaller than D/);
assert.match(growthVersusDilution(0.2, 0.1), /greater than D/);
assert.match(gasTransferWords(1.5), /Degassing/);
assert.match(gasTransferWords(-1.5), /Absorption/);
assert.equal(severityWord("blocker").tone, "danger");
assert.equal(severityWord("info").tone, "info");

// ---- registered and wired like the other spec tests
assert.equal(pkg.scripts["test:170"], "node tests/170-pbr-unit.mjs");
assert.match(pkg.scripts.build, /npm run test:170/);

console.log("170 PBR unit frontend contract passed");
