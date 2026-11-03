import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

// Spec 173: Process dynamic studio. Pure logic (dynamicLogic.ts) plus source wiring of the panel, forms and the shared results view.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFileSync(resolve(root, path), "utf8");
const js = ts.transpileModule(read("src/components/dynamic/dynamicLogic.ts"), { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
const L = await import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);

assert.equal(L.NOTICE, "Tier 1 lumped model. Unqualified. Simulation only. This does not command any equipment.");

// Stored content omits fields at their model default; contentOf restores them and keeps every stored value.
{
  const stored = { scenarios: { run: { id: "run", units: ["PBR"], profiles: [{ profile_id: "p", digest: "d" }], start_utc: "2026-01-01T00:00:00Z", end_utc: "2026-01-02T00:00:00Z", output_cadence_s: 3600, temperature_source: "unit_mean", rtol: 1e-9 } },
    controllers: { c: { id: "c", type: "onoff", measurement: "X", unit: "PBR", actuator: "feed:Feed", output_unit: "m3/s", cadence_s: 60, setpoint: 0.5 } }, schedules: { s: { id: "s" } } };
  const content = L.contentOf(stored);
  assert.deepEqual(content.scenarios.run.controllers, []);
  assert.equal(content.scenarios.run.schedule_id, null);
  assert.equal(content.scenarios.run.controller_cadence_s, 300);
  assert.equal(content.scenarios.run.solver_method, "BDF");
  assert.equal(content.scenarios.run.rtol, 1e-9);
  assert.equal(content.scenarios.run.atol, 1e-9);
  assert.equal(content.scenarios.run.downstream_enabled, false);
  assert.deepEqual(content.scenarios.run.initial, {});
  assert.equal(content.controllers.c.direction, "above");
  assert.equal(content.controllers.c.upper, 1);
  assert.deepEqual(content.schedules.s.events, []);
  assert.deepEqual(L.contentOf(undefined), L.EMPTY_CONTENT);
  assert.notEqual(L.contentOf(stored).scenarios.run.controllers, content.scenarios.run.controllers);
}

// Numbers and time.
assert.equal(L.fmtNum(2e-5), "2e-5");
assert.equal(L.fmtNum(0.05), "0.05");
assert.equal(L.fmtNum(0), "0");
assert.equal(L.fmtNum(20), "20");
assert.equal(L.fmtNum(null), "n/a");
assert.equal(L.fmtDuration(172800), "2 d");
assert.equal(L.fmtDuration(3600), "1 h");
assert.equal(L.fmtDuration(300), "5 min");
assert.equal(L.fmtDuration(90), "1.5 min".replace("1.5 min", "90 s"));
assert.equal(L.fmtDuration(129600), "36 h");
assert.deepEqual(L.splitDuration(172800), { value: 2, unit: "d" });
assert.deepEqual(L.splitDuration(5400), { value: 90, unit: "min" });
assert.equal(L.toSeconds(2, "d"), 172800);

// Descriptions: every event type, recurrence form, condition and action maps to its sentence (acceptance 2).
const draw = { type: "draw", tank: "T1", loop_fraction: 0.2 };
const recurring = { type: "actions", time_s: 86400, every_s: 172800, end_s: 2592000, actions: [draw] };
assert.equal(L.describeEvent(recurring), "Every 2 d from day 1, draw 20 % of the loop from tank T1, until day 30.");
assert.equal(L.describeEvent({ type: "actions", time_s: 0, every_s: 86400, count: 5, actions: [{ type: "draw", tank: "T1", volume_m3: 0.5 }] }), "Every 1 d from day 0, draw 0.5 m3 from tank T1, 5 times.");
assert.equal(L.describeEvent({ type: "actions", time_s: 0, every_s: 86400, count: 5, end_s: 864000, actions: [draw] }), "Every 1 d from day 0, draw 20 % of the loop from tank T1, 5 times, until day 10.");
assert.equal(L.describeEvent({ type: "actions", time_s: 259200, actions: [{ type: "dose", tank: "T1", nitrogen_kg: 0.4 }] }), "At day 3, dose 0.4 kg N into T1.");
assert.equal(L.describeEvent({ type: "actions", time_s: 0, condition: { observed: "loop_N", direction: "below", threshold: 0.05, hysteresis: 0.01, cooldown_s: 3600, max_fires: 3 }, actions: [{ type: "dose", tank: "T1", nitrogen_kg: 0.4 }] }),
  "When loop_N falls below 0.05 kg/m3 (0.01 hysteresis, 1 h cooldown), dose 0.4 kg N into T1, at most 3 times.");
assert.equal(L.describeEvent({ type: "actions", time_s: 86400, condition: { observed: "PBR.X", direction: "above", threshold: 1 }, actions: [draw] }), "When PBR.X rises above 1 kg/m3, draw 20 % of the loop from tank T1, at most 1 time, not before day 1.");
assert.equal(L.describeAction({ type: "separate", recovery: 0.9, concentration_factor: 5, return_to: "T2" }), "separate the drawn culture (recovery 90 %, concentration factor 5) and return to T2");
assert.equal(L.describeAction({ type: "refill", tank: "T1", volume_m3: 0.5, medium: { N: 0.5, O2: 0.008 } }), "refill tank T1 with 0.5 m3 of medium (N 0.5, O2 0.008 kg/m3)");
assert.equal(L.describeAction({ type: "refill", tank: "T1", to_volume_m3: 1, medium: { N: 0.5, O2: 0.008 } }), "refill tank T1 up to 1 m3 of medium (N 0.5, O2 0.008 kg/m3)");
assert.equal(L.describeAction({ type: "dose", tank: "T1", nitrogen_kg: 0.4, volume_m3: 0.01 }), "dose 0.4 kg N into T1 in 0.01 m3");
assert.equal(L.describeAction({ type: "inoculate", tank: "T1", volume_m3: 0.1, culture: { X: 1, N: 0.05, O2: 0.008 } }), "inoculate T1 with 0.1 m3 of culture (X 1, N 0.05, O2 0.008 kg/m3)");
assert.equal(L.describeEvent({ type: "inoculation", time_s: 0, unit: "PBR", value: { X: 0.5, N: 0.05 }, value_unit: "kg/m3" }), "At day 0, inoculate PBR with culture X 0.5, N 0.05 kg/m3 (overwrites the listed concentrations).");
assert.equal(L.describeEvent({ type: "inoculation", time_s: 0, unit: "PBR", value: 0.4, value_unit: "kg/m3" }), "At day 0, inoculate PBR with biomass 0.4 kg/m3 (overwrites the concentration).");
assert.equal(L.describeEvent({ type: "harvest", time_s: 172800, unit: "PBR", fraction: 0.3 }), "At day 2, harvest 30 % of PBR, replaced by blank medium.");
assert.equal(L.describeEvent({ type: "feed_change", time_s: 0, stream: "Feed", value: 1e-5, value_unit: "m3/s" }), "At day 0, set feed Feed to 1e-5 m3/s.");
assert.equal(L.describeEvent({ type: "dilution", time_s: 0, target: "splitter:S1", value: 0.25, value_unit: "1" }), "At day 0, set splitter S1 ratio to 0.25 1.");
assert.equal(L.describeEvent({ type: "setpoint_change", time_s: 86400, target: "ctrl", value: 0.8, value_unit: "kg/m3" }), "At day 1, set the setpoint of controller ctrl to 0.8 kg/m3.");
assert.equal(L.describeEvent({ type: "harvest", time_s: 0, unit: "PBR", fraction: 0.5, observed: "PBR.X", direction: "above", threshold: 2, threshold_unit: "kg/m3", hysteresis: 0.1 }), "When PBR.X rises above 2 kg/m3 (0.1 hysteresis), harvest 50 % of PBR, replaced by blank medium.");
assert.equal(L.describeEvent({ type: "harvest", time_s: 0, every_s: 3600, end_s: 86400, unit: "PBR", fraction: 0.5 }), "Every 1 h from day 0, harvest 50 % of PBR, replaced by blank medium, until day 1.");
// Unknown future fields render as raw JSON, never as a guess.
assert.match(L.describeEvent({ type: "actions", time_s: 0, future: { a: 1 }, actions: [draw] }), /\[unrecognized fields, shown as raw JSON: \{"future":\{"a":1\}\}\]/);
assert.match(L.describeAction({ type: "draw", tank: "T1", loop_fraction: 0.2, mystery: 3 }), /raw JSON: \{"mystery":3\}/);
assert.match(L.describeAction({ type: "teleport", tank: "T1" }), /^Unrecognized action \{/);
// A sample covers every ScheduleEvent field (acceptance 2): each known field changes or is shown in some sentence.
const allFields = { type: "actions", time_s: 0, unit: "u", stream: "s", target: "t", value: 1, value_unit: "m3/s", fraction: 0.1, medium: { N: 1 }, observed: "u.X", threshold: 1, threshold_unit: "kg/m3", direction: "above", hysteresis: 0, every_s: 60, count: 2, end_s: 600, actions: [draw] };
assert.equal(L.describeEvent(allFields).includes("unrecognized"), false, "no ScheduleEvent field is treated as unknown");

// Controllers.
const pi = { id: "c", type: "pi", measurement: "X", unit: "PBR", actuator: "feed:Feed", output_unit: "m3/s", cadence_s: 300, setpoint: 0.8, lower: 0, upper: 2e-5, hysteresis: 0, kp: 1e-5, ki: 0, output: 0, direction: "above" };
assert.equal(L.describeController(pi), "PI keeps X of PBR at 0.8 kg/m3 by feed flow of Feed, every 5 min, output 0 to 2e-5 m3/s (kp 1e-5, ki 0).");
assert.match(L.describeController({ ...pi, type: "onoff", hysteresis: 0.02, direction: "below", measurement: "PBR.N" }), /^On\/off keeps N of PBR below 0.8 kg\/m3 by switching feed flow of Feed between its lower and upper output, every 5 min, output 0 to 2e-5 m3\/s \(0.02 hysteresis\)\.$/);
assert.match(L.describeController({ ...pi, type: "turbidostat", actuator: "splitter:S1", output_unit: "1", upper: 1 }), /^Turbidostat holds X of PBR at 0.8 kg\/m3 by harvest ratio on splitter S1, every 5 min, output 0 to 1 1\.$/);
assert.match(L.describeController({ ...pi, extra: 1 }), /raw JSON: \{"extra":1\}/);

// Timeline: time order, to-scale rows, expanded repeats are read-only (repeat index), conditional events appear once.
const rows = L.timelineRows([recurring, { type: "actions", time_s: 100, actions: [draw] }, { type: "actions", time_s: 0, condition: L.blankCondition("T1"), actions: [draw] }], 10 * 86400);
assert.deepEqual(rows.map((row) => row.time_s), [0, 100, 86400, 86400 + 172800, 86400 + 4 * 86400, 86400 + 6 * 86400, 86400 + 8 * 86400].slice(0, rows.length));
assert.equal(rows.filter((row) => row.eventIndex === 0).length, 5, "repeats within the horizon are expanded");
assert.equal(rows.filter((row) => row.eventIndex === 2).length, 1);
assert.equal(rows.find((row) => row.eventIndex === 2).conditional, true);
assert.equal(rows.find((row) => row.eventIndex === 0 && row.repeat === 2).repeat, 2);
assert.equal(L.timelineRows([{ type: "actions", time_s: 0, every_s: 60, count: 5000, actions: [draw] }], 1e9).length <= 1000, true, "expansion is bounded by the event limit");

// Event shapes: recurrence and condition are mutually exclusive.
let event = { type: "actions", time_s: 0, every_s: 60, count: 3, end_s: 1000, actions: [draw] };
assert.equal(L.eventShape(event), "recurring");
const conditioned = L.setEventShape(event, "condition", "T1");
assert.equal(L.eventShape(conditioned), "condition");
for (const key of ["every_s", "count", "end_s"]) assert.equal(key in conditioned, false);
assert.equal(conditioned.condition.max_fires, 1);
assert.equal("condition" in L.setEventShape(conditioned, "recurring", "T1"), false);
assert.equal(L.eventShape(L.setEventShape(L.setEventShape(conditioned, "recurring", "T1"), "once", "T1")), "once");
const legacyCond = L.setEventShape({ type: "harvest", time_s: 0, unit: "PBR", fraction: 0.1 }, "condition", "PBR");
assert.equal(legacyCond.observed, "PBR.X");
assert.equal(legacyCond.threshold_unit, "kg/m3");
assert.deepEqual(L.eventIssues(L.setEventShape({ ...event, actions: [draw] }, "recurring", "T1")), []);
assert.ok(L.eventIssues({ type: "actions", time_s: 0, every_s: 60, actions: [draw] }).some((x) => /count or an end/.test(x)));
assert.ok(L.eventIssues({ type: "actions", time_s: 0, actions: [{ type: "draw", tank: "T1", volume_m3: 1, loop_fraction: 0.2 }] }).some((x) => /exactly one/.test(x)));
assert.ok(L.eventIssues({ type: "actions", time_s: 0, actions: [] }).length);
assert.ok(L.eventIssues({ type: "actions", time_s: 0, every_s: 60, count: 2, condition: L.blankCondition("T1"), actions: [draw] }).some((x) => /both recur/.test(x)));
const dose = L.setPath({ type: "dose", tank: "T1" }, ["volume_m3"], 0.1);
assert.deepEqual(L.setPath(dose, ["volume_m3"], undefined), { type: "dose", tank: "T1" });
assert.deepEqual(L.setPath(L.setPath({ type: "refill" }, ["medium", "N"], 1), ["medium", "N"], undefined), { type: "refill" });
for (const type of L.EVENT_TYPES) assert.equal(L.blankEvent(type, "T1").type, type);
for (const type of L.ACTION_TYPES) assert.equal(L.blankAction(type, "T1").type, type);
assert.deepEqual(Object.keys(L.changeEventType(recurring, "harvest", "T1")).sort(), ["end_s", "every_s", "fraction", "time_s", "type", "unit"]);

// Ops carry the id outside the value and never a link.
assert.deepEqual(L.setOp("schedule", { id: "s1", events: [] }), { op: "set_schedule", id: "s1", value: { events: [] } });
assert.deepEqual(L.deleteOp("controller", "c1"), { op: "delete_controller", id: "c1" });
const content = { scenarios: { run: { id: "run", schedule_id: "s1", controllers: ["c1"] } }, schedules: {}, controllers: {} };
assert.deepEqual(L.referencedBy(content, "schedule", "s1"), ["run"]);
assert.deepEqual(L.referencedBy(content, "controller", "c2"), []);

// Scenario hints name what the backend would refuse; limits are shown and nothing is corrected.
const scenario = L.blankScenario("run", ["PBR"], { profile_id: "sha256:a", digest: "sha256:a", start: "2026-01-01T00:00:00Z", end: "2026-03-01T00:00:00Z" });
assert.equal(scenario.start_utc, "2026-01-01T00:00:00Z");
assert.equal(scenario.end_utc, "2026-01-08T00:00:00Z");
assert.equal(scenario.fidelity, "T1");
// A unit preselected for a new scenario gets the same editable starting state as one chosen later (the engine requires X, N and O2).
assert.deepEqual(scenario.initial, { PBR: { X: 0.2, N: 0.05, O2: 0.008 } });
assert.deepEqual(L.scenarioHints(scenario), []);
assert.ok(L.scenarioHints({ ...scenario, output_cadence_s: 30 }).some((x) => /at least 60 s/.test(x)));
assert.ok(L.scenarioHints({ ...scenario, end_utc: "2026-09-01T00:00:00Z" }).some((x) => /exceeds the limit of 120 d/.test(x)));
assert.ok(L.scenarioHints({ ...scenario, end_utc: "2026-01-20T00:00:00Z", output_cadence_s: 60 }).some((x) => /output points exceed/.test(x)));
assert.ok(L.scenarioHints({ ...scenario, start_utc: "2026-01-01T00:00:00" }).some((x) => /UTC offset/.test(x)));
assert.ok(L.scenarioHints({ ...scenario, profiles: [] }).some((x) => /profile/.test(x)));
assert.equal(scenario.output_cadence_s, 3600, "hints never edit the scenario");
assert.match(L.scenarioLimitText(scenario), /duration at most 120 d \(now 7 d\), at most 20000 output points \(now 169\)/);

// 191 link: badge, confirmation on edit, and the editor never writes a link.
const link = { id: "l1", schedule_id: "s1", version_label: "Cycle v1", state: "applied" };
assert.match(L.linkBadge(link).text, /Cycle v1: applied, unmodified/);
assert.match(L.linkBadge({ ...link, state: "modified" }).text, /modified, no longer the pinned procedure/);
assert.equal(L.linkBadge(null), null);
assert.equal(L.needsLinkConfirmation(link), true);
assert.equal(L.needsLinkConfirmation(null), false);
assert.equal(L.needsLinkConfirmation({ ...link, state: "missing" }), false);
assert.match(L.linkConfirmation(link, "edit"), /makes the link read "modified, no longer the pinned procedure"/);
assert.match(L.linkConfirmation(link, "edit"), /never rewrites or removes the link/);

// Staleness mirrors the backend flag exactly (acceptance 3).
const identity = { draft_revision: "3:abc" };
assert.equal(L.staleness({ current: true, identity }, 7).state, "current");
assert.equal(L.staleness({ current: false, identity }, 7).state, "stale");
assert.match(L.staleness({ current: false, identity }, 7).detail, /revision 3; the draft is now at revision 7/);
assert.equal(L.staleness({ current: null, identity }, 7).state, "unbound");
assert.equal(L.staleness({ current: null, identity }, 7).label, "Not bound");
assert.equal(L.staleness({ identity }, 7).state, "unbound");
assert.match(L.SNAPSHOT_CAVEAT, /does not cover a changed environment profile or model card/);
assert.equal(L.runTitle({ job_id: "abcdef123456", scenario_id: "run" }, { label: "Stale" }), "run · abcdef12 · stale");
assert.equal(L.revisionSeq("12:ff"), 12);
assert.equal(L.revisionSeq(null), null);

// Chips, KPIs and provenance only from existing data; absent data says why.
const manifest = {
  fidelity: "T1", snapshot_digest: "snap", solver: { method: "BDF", rtol: 1e-6, atol: 1e-9 }, units: [{ tag: "PBR", model_card_qualification: "unqualified", card_id: "c", card_revision: "1", card_digest: "cd", parameter_set_digest: "pd" }],
  profiles: [{ profile_id: "sha256:aaaaaaaaaaaaaaaaaaaaaaaa", digest: "sha256:aaaa", resolution_minutes: 60, temporal_rule: { par: "held" } }],
  balances: { aggregate: { biomass: { residual_abs_kg: 0, residual_rel: 1e-9 }, oxygen: { residual_abs_kg: 0, residual_rel: 5e-8 } }, asserted: true },
  event_log: [{ type: "actions", balance: { closed: true } }, { type: "actions", balance: { closed: true } }, { type: "feed_change" }], controller_log: [{}, {}, {}, {}],
  downstream_outcomes: [{ status: "converged", residual: 1e-8 }, { status: "downstream_unconverged" }], diagnostics: { wall_time_s: 1.25 },
  productivity: { volumetric_kg_m3_day: 0.12, areal_kg_m2_day: null, areal_unavailable_reason: "Illuminated tube geometry is unavailable.", energy_per_kg_unavailable_reason: "No dynamic pump or aeration energy input is declared." },
  campaign: { loops: { main: { cycles: [], summary: { draws: 4, harvested_biomass_kg: 1.5, last_k: 3, mean_last_k_net_volumetric_productivity_kg_m3_day: 0.2 } } } },
};
const series = { channels: { t_s: [0, 100, 200], PBR_X: [1, 2, 3], harvest_X_kg: [0, 0.5, 0.9] }, returned_points: 3, total_points: 3, stride: 1 };
const chipsOk = L.chips(manifest, { status: "succeeded" });
assert.equal(chipsOk.find((c) => c.id === "balance").tone, "ok");
assert.match(chipsOk.find((c) => c.id === "balance").text, /asserted by the engine/);
assert.match(chipsOk.find((c) => c.id === "events").text, /2 of 2 action events closed/);
assert.match(chipsOk.find((c) => c.id === "convergence").text, /1 converged, 1 unconverged, recorded residual up to 1e-8/);
assert.equal(chipsOk.find((c) => c.id === "convergence").tone, "warn");
assert.match(L.chips({ ...manifest, downstream_outcomes: [] }, { status: "succeeded" }).find((c) => c.id === "convergence").text, /No downstream\./);
assert.equal(L.chips({ ...manifest, balances: { aggregate: { biomass: { residual_abs_kg: 1, residual_rel: 0.5 } }, asserted: true } }, { status: "succeeded" }).find((c) => c.id === "balance").tone, "danger");
assert.match(L.chips(null, { status: "failed", error: { code: "BALANCE_NOT_CLOSED" } })[0].text, /BALANCE_NOT_CLOSED/);
assert.ok(Math.abs(L.balanceTolerance(manifest) - 1e-4) < 1e-12);
const cards = L.kpis(manifest, series);
const byId = Object.fromEntries(cards.map((k) => [k.id, k]));
assert.equal(byId.final_PBR.value, 3);
assert.equal(byId.mean_PBR.value, 2, "time-weighted mean");
assert.equal(byId.harvest.value, 0.9);
assert.equal(byId.events.value, 3);
assert.equal(byId.controller_fires.value, 4);
assert.equal(byId.vol_prod.value, 0.12);
assert.equal(byId.areal_prod.value, null);
assert.equal(byId.areal_prod.reason, "Illuminated tube geometry is unavailable.");
assert.equal(byId.campaign_draws_main.value, 4);
assert.match(L.fmtKpi(byId.areal_prod), /^not available: Illuminated/);
assert.equal(L.fmtKpi(byId.final_PBR), "3 kg/m3");
assert.equal(L.kpis(null, null).find((k) => k.id === "vol_prod").value, null);
assert.match(L.kpis(manifest, null).find((k) => k.id === "final_PBR").reason, /No biomass series/);
const prov = L.provenanceRows(manifest, { identity: { draft_revision: "2:x" } });
assert.ok(prov.some((r) => r.label === "Model card, PBR" && /unqualified/.test(r.value) && /card cd/.test(r.value) && /parameters pd/.test(r.value)));
assert.ok(prov.some((r) => /^Profile/.test(r.label) && /digest sha256:aaaa/.test(r.value)));
assert.ok(prov.some((r) => r.label === "Wall time" && r.value === "1.25 s"));

// Channel grouping keeps units.
const channels = { t_s: "s", PBR_X: "kg/m3", PBR_N: "kg/m3", T1_V_m3: "m3", harvest_X_kg: "kg", temperature: "K" };
assert.deepEqual(L.groupChannelsByUnit(channels).map((g) => g.unit), ["K", "kg", "kg/m3", "m3"]);
assert.deepEqual(L.groupChannelsByUnit(channels).find((g) => g.unit === "kg/m3").names, ["PBR_N", "PBR_X"]);
assert.deepEqual(L.defaultChannels(channels), ["PBR_X", "harvest_X_kg", "T1_V_m3"]);
assert.equal(L.channelLabel("X", undefined), "X [unit not recorded]");

// Series paging and CSV: a run above 5000 points exports every row, with rows at a repeated time kept (acceptance 4).
function backend(rows) {
  const t = Array.from({ length: rows }, (_, i) => (i > 0 && i % 997 === 0 ? i - 1 : i)); // repeated t_s rows like event phases
  const data = { t_s: t, X: t.map((v, i) => v * 2 + (i % 2)), Y: t.map((_, i) => i / 3) };
  const calls = [];
  const fetchPage = async ({ channels, start_s, end_s, max_points }) => {
    calls.push({ channels, start_s, end_s, max_points });
    const picked = channels.includes("t_s") ? channels : ["t_s", ...channels];
    const idx = t.map((_, i) => i).filter((i) => (start_s === undefined || t[i] >= start_s) && (end_s === undefined || t[i] <= end_s));
    const stride = Math.max(1, Math.ceil(idx.length / Math.min(max_points, 5000)));
    const kept = idx.filter((_, i) => i % stride === 0);
    return { channels: Object.fromEntries(picked.map((n) => [n, kept.map((i) => data[n][i])])), returned_points: kept.length, total_points: idx.length, stride };
  };
  return { data, fetchPage, calls };
}
for (const rows of [1, 40, 5000, 5001, 12001, 20001]) {
  const { data, fetchPage, calls } = backend(rows);
  const got = await L.collectSeries(fetchPage, ["X", "Y"]);
  assert.equal(got.rows, rows, `rows ${rows}`);
  assert.deepEqual(got.columns.t_s, data.t_s);
  assert.deepEqual(got.columns.X, data.X);
  assert.deepEqual(got.columns.Y, data.Y);
  assert.ok(calls.every((c) => c.max_points <= 5000));
}
{
  const { fetchPage } = backend(12001);
  const chunked = await L.collectSeries(fetchPage, ["X", "Y"], { channelsPerRequest: 1 });
  assert.equal(chunked.rows, 12001);
}
// A page that stays strided is refused, never padded; a short result is refused.
await assert.rejects(L.collectSeries(async ({ channels }) => ({ channels: Object.fromEntries(["t_s", ...channels].map((n) => [n, [0, 2]])), returned_points: 2, total_points: 4, stride: 2 }), ["X"]), L.ExportRefused);
await assert.rejects(L.collectSeries(async ({ channels, start_s }) => (start_s === undefined && channels.length === 1 && channels[0] === "t_s" ? { channels: { t_s: [0, 1, 2] }, returned_points: 3, total_points: 5, stride: 1 } : { channels: Object.fromEntries(["t_s", ...channels].map((n) => [n, [0, 1, 2]])), returned_points: 3, total_points: 3, stride: 1 }), ["X"]), /truncated/);
const empty = await L.collectSeries(async () => ({ channels: { t_s: [] }, returned_points: 0, total_points: 0, stride: 1 }), ["X"]);
assert.equal(empty.rows, 0);

const csv = L.buildCsv({ jobId: "job-1", snapshotDigest: "snap", fidelity: "T1", scenarioId: "run", channels: ["PBR_X", "note"], units: { t_s: "s", PBR_X: "kg/m3", note: "1" }, columns: { t_s: [0, 60], PBR_X: [0.5, 0.25], note: [0, null] } });
const lines = csv.trimEnd().split("\n");
assert.equal(lines[0], "# job_id=job-1; snapshot_digest=snap; fidelity=T1; scenario=run; rows=2");
assert.equal(lines[1], `# ${L.NOTICE}`);
assert.match(lines[1], /Unqualified/);
assert.equal(lines[2], "t_s [s],PBR_X [kg/m3],note [1]");
assert.equal(lines[3], "0,0.5,0");
assert.equal(lines[4], "60,0.25,");
assert.equal(lines.length, 5);
assert.throws(() => L.buildCsv({ jobId: "j", snapshotDigest: "s", fidelity: "T1", channels: ["A"], units: {}, columns: { t_s: [0, 1], A: [1] } }), L.ExportRefused);
assert.match(L.buildCsv({ jobId: "j", snapshotDigest: "s", fidelity: "T1", channels: ["a,b"], units: { "a,b": "x" }, columns: { t_s: [0], "a,b": [1] } }), /"a,b \[x\]"/);

// Comparison: at most 4 runs of one draft; units must match to overlay; nothing is interpolated (acceptance 5).
let selected = [];
const drafts = { a: "d1", b: "d1", c: "d1", d: "d1", e: "d1", z: "d2" };
for (const id of ["a", "b", "c", "d"]) { const next = L.compareSelection(selected, id, drafts); assert.equal(next.ok, true); selected = next.selection; }
assert.equal(L.compareSelection(selected, "e", drafts).ok, false);
assert.match(L.compareSelection(selected, "e", drafts).reason, /At most 4/);
assert.deepEqual(L.compareSelection(selected, "a", drafts).selection, ["b", "c", "d"], "choosing a selected run unselects it");
assert.match(L.compareSelection(["a"], "z", drafts).reason, /one draft/);
const cmp = L.compareChannels([{ jobId: "r1", channels: { t_s: "s", X: "kg/m3", V: "m3" } }, { jobId: "r2", channels: { t_s: "s", X: "g/L" } }, { jobId: "r3", channels: { t_s: "s", X: "kg/m3", V: "m3", Q: "m3/s" } }]);
const X = cmp.find((c) => c.name === "X");
assert.deepEqual([X.included, X.mismatched, X.absent], [["r1", "r3"], [{ jobId: "r2", unit: "g/L" }], []]);
const V = cmp.find((c) => c.name === "V");
assert.deepEqual([V.included, V.absent], [["r1", "r3"], ["r2"]]);
assert.deepEqual(cmp.find((c) => c.name === "Q").absent, ["r1", "r2"]);
assert.equal(cmp.some((c) => c.name === "t_s"), false);
const aligned = L.alignOnTime([{ t: [0, 10, 20, 20], y: [1, 2, 3, 4] }, { t: [0, 15, 20], y: [5, 6, 7] }]);
assert.deepEqual(aligned.x, [0, 10, 15, 20, 20]);
assert.deepEqual(aligned.ys[0], [1, 2, null, 3, 4]);
assert.deepEqual(aligned.ys[1], [5, null, 6, 7, null], "a run is absent where it has no point; no interpolation");
const kA = [{ id: "harvest", group: "Harvest", label: "Cumulative harvest", value: 1, unit: "kg" }, { id: "areal", group: "P", label: "Areal", value: null, unit: "x", reason: "no geometry" }];
const kB = [{ id: "harvest", group: "Harvest", label: "Cumulative harvest", value: 1.5, unit: "kg" }, { id: "areal", group: "P", label: "Areal", value: 2, unit: "x" }];
const deltas = L.kpiDeltas("A", [{ jobId: "A", kpis: kA }, { jobId: "B", kpis: kB }]);
assert.equal(deltas.find((d) => d.id === "harvest").values[0].delta, 0.5);
assert.equal(deltas.find((d) => d.id === "harvest").values[0].relative, 0.5);
assert.equal(deltas.find((d) => d.id === "areal").values[0].delta, null);
assert.deepEqual(L.kpiDeltas("missing", [{ jobId: "A", kpis: kA }]), []);
const missing = L.kpiDeltas("A", [{ jobId: "A", kpis: kA }, { jobId: "B", kpis: [] }]);
assert.equal(missing.find((d) => d.id === "harvest").values[0].reason, "This run has no such quantity.");

// Table windows and keyboard range control.
assert.deepEqual(L.tableWindow([0, 1, 2, 3, 4], [1, 3]).indices, [1, 2, 3]);
assert.equal(L.tableWindow(Array.from({ length: 1000 }, (_, i) => i), null, 200).indices.length, 200);
assert.equal(L.tableWindow(Array.from({ length: 1000 }, (_, i) => i), null, 200).total, 1000);
assert.deepEqual(L.panRange([10, 20], [0, 100], 0.5), [15, 25]);
assert.deepEqual(L.panRange([90, 100], [0, 100], 0.5), [90, 100], "pan stops at the run end");
assert.deepEqual(L.panRange([0, 10], [0, 100], -1), [0, 10]);
assert.deepEqual(L.zoomRange([0, 100], [0, 100], 0.5), [25, 75]);
assert.deepEqual(L.zoomRange([0, 100], [0, 100], 4), [0, 100], "zoom out stops at the run bounds");

// Design › Dynamic guided path: each step's state comes from the saved content and the runs; null runs means not read yet.
{
  const empty = L.contentOf(null);
  const unread = L.dynamicSteps(empty, null);
  assert.deepEqual(unread.map((step) => step.title), ["Scenario", "Schedule & controllers", "Run", "Results & compare"]);
  assert.deepEqual(unread.map((step) => step.status), ["No scenario yet", "Optional · no schedule, no controller", "Checking runs…", "Checking runs…"]);
  assert.deepEqual(unread.map((step) => step.sections[0]), ["scenario", "schedule", "runs", "plant"], "each step opens its first section");
  assert.deepEqual(L.dynamicSteps(empty, []).map((step) => step.status).slice(2), ["Needs a scenario first", "No results yet"]);
  assert.equal(L.suggestedTab(empty, null), "scenario", "without a scenario the path opens on step 1 even before runs are read");
  const content = L.contentOf({ scenarios: { a: { id: "a" }, b: { id: "b" } }, schedules: { s: { id: "s" } }, controllers: {} });
  assert.equal(L.suggestedTab(content, null), undefined, "with scenarios it waits for the runs");
  assert.equal(L.suggestedTab(content, []), "runs");
  const none = L.dynamicSteps(content, []);
  assert.deepEqual(none.map((step) => step.status), ["2 scenarios saved", "Optional · 1 schedule, no controller", "No run yet", "No results yet"]);
  assert.deepEqual(none.map((step) => step.done), [true, true, false, false]);
  const runs = [
    { job_id: "old", status: "succeeded", scenario_id: "a", created_at: "2026-01-01T00:00:00Z", current: true },
    { job_id: "new", status: "failed", scenario_id: "b", created_at: "2026-01-02T00:00:00Z", current: false },
  ];
  assert.equal(L.suggestedTab(content, runs), "plant", "with runs the path opens on the results");
  const done = L.dynamicSteps(content, runs);
  assert.equal(done[2].status, "2 runs · latest failed, stale", "the latest run is the newest by creation time");
  assert.equal(done[3].status, "1 succeeded run to read");
  assert.deepEqual(done.map((step) => step.done), [true, true, true, true]);
  const running = L.dynamicSteps(content, [...runs, { job_id: "live", status: "running", scenario_id: "a", created_at: "2026-01-03T00:00:00Z" }]);
  assert.equal(running[2].status, "1 run in progress");
  assert.equal(L.dynamicSteps(content, [runs[0], { ...runs[0], job_id: "again" }])[3].status, "2 succeeded runs to read or compare");
}

// Source wiring keeps the tested logic reachable through the operator UI.
const dir = "src/components/dynamic/";
const panel = read(dir + "DynamicPanel.tsx");
for (const name of ["ScenarioEditor", "ScheduleEditor", "ControllerEditor", "RunsPanel", "Comparison", "patchDraft", "SimulationNotice"]) assert.ok(panel.includes(name), name);
for (const file of ["DynamicPanel", "ScenarioEditor", "ScheduleEditor", "ControllerEditor", "RunsPanel", "RunResultsView", "SyncCharts", "Comparison"]) {
  const source = read(`${dir}${file}.tsx`);
  assert.doesNotMatch(source, /dangerouslySetInnerHTML|eval\(/, file);
  assert.doesNotMatch(source, /set_link|write_link|"PATCH"/, `${file} never writes a procedure link`);
}
const scheduleEditor = read(dir + "ScheduleEditor.tsx");
for (const name of ["timelineRows", "EventFields", "ActionFields", "describeEvent", "linkConfirmation", "window.confirm", "setOp", "deleteOp"]) assert.ok(scheduleEditor.includes(name), name);
const controllerEditor = read(dir + "ControllerEditor.tsx");
for (const name of ["onoff", "pi", "turbidostat", "measurement", "actuator", "cadence_s", "setpoint", "lower", "upper", "hysteresis", "kp", "ki", "direction", "describeController"]) assert.ok(controllerEditor.includes(name), name);
assert.doesNotMatch(controllerEditor, /["'](?:pH|CO2|thermal)["']/i);
const comparison = read(dir + "Comparison.tsx");
for (const name of ["compareSelection", "compareChannels", "alignOnTime", "kpiDeltas", "Baseline run", "ChartPanel"]) assert.ok(comparison.includes(name), name);
const charts = read(dir + "SyncCharts.tsx");
for (const name of ["import(\"uplot\")", "Values table", "tabIndex={0}", "ArrowLeft", "ArrowRight", "cursor", "sync"]) assert.ok(charts.includes(name), name);
// Dynamic Studio is reached as Design › Dynamic (guided), still from Optics' prediction, and the standalone Process editor points there.
const designDynamic = read("src/pages/DesignDynamicPanel.tsx");
assert.ok(designDynamic.includes("<DynamicPanel") && /<DynamicPanel[^>]*\bguided\b/.test(designDynamic), "Design › Dynamic renders the guided panel");
assert.ok(read("src/pages/DesignOpticsPanel.tsx").includes("<DynamicPanel"), "the Optics prediction keeps the panel");
const processEditor = read("src/stages/ProcessDraftEditor.tsx");
assert.ok(!processEditor.includes("<DynamicPanel") && !processEditor.includes("draft-dynamic-studio"), "no Dynamic Studio fold at the bottom of the Process editor");
assert.ok(processEditor.includes('stage: "Dynamic"') && processEditor.includes("draftId: draft.draft_id") && processEditor.includes("Design › Dynamic</AppLink>"), "the Process editor links to Design › Dynamic with its draft");
// One notice per screen: the panel header shows it and the sections inside leave theirs out; elsewhere (Studio bench) a section still shows its own.
assert.ok(panel.includes("<NoticeShown.Provider value={true}>") && read(dir + "SimulationNotice.tsx").includes("useContext(NoticeShown) ? null"));
assert.equal((panel.match(/<SimulationNotice \/>/g) ?? []).length, 2, "guided and tabbed headers each carry the notice once");
// The guided steps open the panel's sections and take the suggested step only once.
assert.ok(panel.includes("<DynamicSteps") && panel.includes("onOpen={setTab}") && panel.includes("if (suggested) setTab(suggested)"));
assert.ok(read("src/api/processDraft.ts").includes("dynamic?:"));
assert.ok(read("src/components/studio/ProcedureBench.tsx").includes("<RunResultsView"));
assert.doesNotMatch(read("src/components/studio/ProcedureBench.tsx"), /<svg|chartPaths|SeriesChart/);
assert.match(read("package.json"), /npm run test:173/);
// The charts carry their own uPlot stylesheet; the Environment panel that also imports it is lazy-loaded.
assert.match(charts, /import "uplot\/dist\/uPlot.min.css";/);
// Draft patch refusals keep their backend code (decision 3): failureText reads DraftApiError as well as StudioApiError.
assert.match(read(dir + "dynamicApi.ts"), /cause instanceof StudioApiError \|\| cause instanceof DraftApiError/);
console.log("173 Process dynamic studio frontend ok");
