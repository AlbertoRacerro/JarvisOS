import type { ActionType, Num, ProcedureAction, ProcedureCase, ProcedureLink, ProcedureReport, ProcedureStep, RunResults, StepCondition } from "../../api/studioProcedure";
import type { StudioModel, StudioVariable } from "../../api/studio";

// Spec 191 procedure authoring helpers. Pure so the build-gating test can import them. Nothing here corrects a value:
// a field the operator leaves empty is left out and the backend refuses it with its typed code.
export const NOTICE = "Simulation only. Actions change modelled tank inventories. This does not command any equipment. Unqualified.";
export const TIME_UNITS = [["s", 1], ["min", 60], ["h", 3600], ["d", 86400]] as const;
export type TimeUnit = (typeof TIME_UNITS)[number][0];
export const toSeconds = (value: number, unit: TimeUnit): number => value * (TIME_UNITS.find(([name]) => name === unit)?.[1] ?? 1);
/** The largest unit that represents the seconds exactly, so 172800 reads as 2 d and 90 as 90 s. */
export function fromSeconds(seconds: number): { value: number; unit: TimeUnit } {
  for (const [unit, size] of [...TIME_UNITS].reverse()) if (seconds !== 0 && Number.isInteger(seconds / size)) return { value: seconds / size, unit };
  return { value: seconds, unit: "s" };
}
export const isRef = (value: unknown): value is { $param: string } => Boolean(value) && typeof value === "object" && Object.keys(value as object).length === 1 && typeof (value as { $param?: unknown }).$param === "string";
export const bindParam = (name: string): { $param: string } => ({ $param: name });

export type FieldDef = { path: string[]; label: string; unit: string; optional?: boolean };
export type ActionDef = { type: ActionType; label: string; tanks: string[]; choice?: FieldDef[]; fields: FieldDef[] };
const f = (path: string, label: string, unit: string, optional = false): FieldDef => ({ path: path.split("."), label, unit, optional });
// Exactly the 185 shapes. `tanks` lists the tag fields; `choice` fields are mutually exclusive (exactly one).
export const ACTIONS: Record<ActionType, ActionDef> = {
  draw: { type: "draw", label: "Draw", tanks: ["tank"], choice: [f("volume_m3", "Volume", "m3"), f("loop_fraction", "Loop fraction", "1")], fields: [] },
  separate: { type: "separate", label: "Separate", tanks: ["return_to"], fields: [f("recovery", "Recovery", "1"), f("concentration_factor", "Concentration factor", "1")] },
  refill: { type: "refill", label: "Refill", tanks: ["tank"], choice: [f("volume_m3", "Add volume", "m3"), f("to_volume_m3", "Refill to volume", "m3")], fields: [f("medium.N", "Medium N", "kg/m3"), f("medium.O2", "Medium O2", "kg/m3")] },
  dose: { type: "dose", label: "Dose", tanks: ["tank"], fields: [f("nitrogen_kg", "Nitrogen", "kg"), f("volume_m3", "Volume", "m3", true)] },
  inoculate: { type: "inoculate", label: "Inoculate", tanks: ["tank"], fields: [f("volume_m3", "Volume", "m3"), f("culture.X", "Culture X", "kg/m3"), f("culture.N", "Culture N", "kg/m3"), f("culture.O2", "Culture O2", "kg/m3")] }
};
export const ACTION_TYPES = Object.keys(ACTIONS) as ActionType[];
export const OBSERVED_CHANNELS = ["X", "N", "O2", "loop_X", "loop_N"] as const;
export const MAX_ACTIONS = 16;

export const getPath = (record: Record<string, unknown>, path: string[]): unknown => path.reduce<unknown>((value, key) => (value && typeof value === "object" ? (value as Record<string, unknown>)[key] : undefined), record);
/** Immutable set; undefined removes the key (and an emptied nested record), so no placeholder is ever sent. */
export function setPath<T extends Record<string, unknown>>(record: T, path: string[], value: unknown): T {
  const [head, ...rest] = path;
  const next: Record<string, unknown> = { ...record };
  if (rest.length) {
    const child = setPath((record[head] as Record<string, unknown> | undefined) ?? {}, rest, value);
    if (Object.keys(child).length) next[head] = child; else delete next[head];
  } else if (value === undefined) delete next[head]; else next[head] = value;
  return next as T;
}
export const blankAction = (type: ActionType): ProcedureAction => ({ type });
export const blankStep = (index: number): ProcedureStep => ({ label: `Step ${index + 1}`, time_s: 0, actions: [blankAction("draw")] });
export const stepKind = (step: ProcedureStep): "once" | "recurring" | "condition" => step.condition ? "condition" : step.every_s !== undefined || step.count !== undefined || step.end_s !== undefined ? "recurring" : "once";
export const blankCondition = (): StepCondition => ({ observed: "", direction: "below", threshold: 0, hysteresis: 0, cooldown_s: 0, max_fires: 1 });
/** Switching kind removes the fields of the other kind: the contract forbids both on one step. */
export function setStepKind(step: ProcedureStep, kind: "once" | "recurring" | "condition"): ProcedureStep {
  const { every_s: _a, count: _b, end_s: _c, condition: _d, ...rest } = step;
  if (kind === "recurring") return { ...rest, every_s: 86400 } as ProcedureStep;
  if (kind === "condition") return { ...rest, condition: blankCondition() };
  return rest;
}
/** Action type change keeps nothing but the tank, because field sets differ between types. */
export const changeActionType = (action: ProcedureAction, type: ActionType): ProcedureAction => (typeof action.tank === "string" && ACTIONS[type].tanks.includes("tank") ? { type, tank: action.tank } : { type });

export function splitObserved(observed: string): { unit: string; channel: string } {
  const index = observed.lastIndexOf(".");
  return index < 0 ? { unit: observed, channel: "loop_N" } : { unit: observed.slice(0, index), channel: observed.slice(index + 1) };
}
export const joinObserved = (unit: string, channel: string) => `${unit}.${channel}`;

const NUM_STEP_KEYS = ["time_s", "every_s", "count", "end_s"] as const;
/** Client-side hints for what the backend would refuse. They never block Save; the typed backend code remains the authority. */
export function stepIssues(steps: ProcedureStep[], parameters: StudioVariable[]): string[] {
  const issues: string[] = [];
  const names = new Set(parameters.map((item) => item.name));
  const refs = (value: unknown, where: string) => { if (isRef(value) && !names.has(value.$param)) issues.push(`${where}: parameter "${value.$param}" is not declared.`); };
  steps.forEach((step, index) => {
    const at = `Step ${index + 1}`;
    if (!step.label.trim()) issues.push(`${at} needs a label.`);
    if (step.condition && (step.every_s !== undefined || step.count !== undefined || step.end_s !== undefined)) issues.push(`${at}: a conditioned step cannot also recur.`);
    if (step.every_s === undefined && (step.count !== undefined || step.end_s !== undefined)) issues.push(`${at}: count and end need "every".`);
    if (step.actions.length < 1 || step.actions.length > MAX_ACTIONS) issues.push(`${at} needs 1 to ${MAX_ACTIONS} actions.`);
    for (const key of NUM_STEP_KEYS) refs(step[key], `${at} ${key}`);
    if (step.condition && !step.condition.observed.includes(".")) issues.push(`${at}: choose the observed unit and channel.`);
    step.actions.forEach((action, position) => {
      const def = ACTIONS[action.type]; const where = `${at} action ${position + 1} (${def.label})`;
      for (const tag of def.tanks) if (typeof action[tag] !== "string" || !action[tag]) issues.push(`${where}: choose ${tag === "return_to" ? "a return tank or none" : "a tank"}.`);
      if (def.choice) { const set = def.choice.filter((field) => getPath(action, field.path) !== undefined); if (set.length !== 1) issues.push(`${where}: give exactly one of ${def.choice.map((field) => field.label).join(" / ")}.`); }
      for (const field of [...(def.choice ?? []), ...def.fields]) { const value = getPath(action, field.path); refs(value, `${where} ${field.label}`); if (value === undefined && !field.optional && !def.choice?.includes(field)) issues.push(`${where}: ${field.label} is required.`); }
    });
  });
  return issues;
}

/** Parameter values: every declared parameter needs a number in its declared unit; bounds are checked by the backend at Test. */
export function parameterValuesIssues(parameters: StudioVariable[], values: Record<string, number | undefined>): string[] {
  return parameters.filter((item) => values[item.name] === undefined || !Number.isFinite(values[item.name])).map((item) => `Enter a value for ${item.name} [${item.unit}].`);
}
export const boundHint = (item: StudioVariable) => item.min != null || item.max != null ? ` (${item.min ?? "-inf"} to ${item.max ?? "inf"})` : "";

export function blankCase(parameters: StudioVariable[], draftId = "", scenarioId = ""): ProcedureCase {
  const start = (item: StudioVariable) => item.min != null && item.max != null ? (item.min + item.max) / 2 : item.min ?? item.max ?? 0;
  return { name: "base case", parameter_values: Object.fromEntries(parameters.map((item) => [item.name, start(item)])), draft_id: draftId, scenario_id: scenarioId, schedule_id: "procedure", simulate: false, expect: [] };
}
/** Publish needs a passing report for the exact current hashes and at least one case, as the backend demands. */
export function canPublishProcedure(model: StudioModel | null, dirty: boolean): boolean {
  if (!model || dirty || model.status === "published") return false;
  const report = model.last_test_report as ProcedureReport | null | undefined;
  return Boolean(report?.passed && report.cases.length > 0 && report.source_sha256 === model.source_sha256 && report.contract_sha256 === model.contract_sha256);
}

export type LinkBadge = { tone: "ok" | "warn" | "danger"; text: string };
/** Everything the bench says about a link: pin state, hand edit, newer version. Never hides a modification. */
export function linkBadges(link: ProcedureLink): LinkBadge[] {
  const out: LinkBadge[] = [];
  if (link.state === "applied") out.push({ tone: "ok", text: `Applied · pinned to ${link.version_label}` });
  else if (link.state === "modified") out.push({ tone: "danger", text: "Modified, no longer the pinned procedure." });
  else out.push({ tone: "danger", text: "Schedule no longer in the draft." });
  if (link.update_available && link.latest_version_label) out.push({ tone: "warn", text: `Superseded: ${link.latest_version_label} is published. This schedule stays on ${link.version_label} until you choose ${updateLabel(link)}.` });
  return out;
}
export const updateLabel = (link: ProcedureLink) => link.update_available && link.latest_version_label ? `Update to ${link.latest_version_label.replace(/^.*\b(v\d+)$/i, "$1")}` : null;
export const canRun = (link: ProcedureLink | null | undefined) => link?.state === "applied";

export type ChartSeries = { name: string; values: number[] };
export function chartPaths(t: number[], series: ChartSeries[], width: number, height: number): { name: string; d: string }[] {
  if (t.length < 2) return [];
  const lo = Math.min(...series.flatMap((item) => item.values)); const hi = Math.max(...series.flatMap((item) => item.values));
  const span = hi - lo || 1; const t0 = t[0]; const dt = t[t.length - 1] - t0 || 1;
  return series.map((item) => ({ name: item.name, d: item.values.map((v, i) => `${i ? "L" : "M"}${(((t[i] - t0) / dt) * width).toFixed(1)},${(hi === lo ? height / 2 : height - ((v - lo) / span) * height).toFixed(1)}`).join("") }));
}
export const KPI_COLUMNS: [string, string][] = [["index", "Cycle"], ["length_s", "Length [s]"], ["harvested_biomass_kg", "Harvest [kg]"], ["harvested_volume_m3", "Harvest vol [m3]"], ["concentrate_biomass_kg_m3", "Concentrate [kg/m3]"], ["purge_volume_m3", "Purge [m3]"], ["medium_volume_m3", "Medium [m3]"], ["nitrogen_added_kg", "N added [kg]"], ["x_mean_before_draw_kg_m3", "X before draw [kg/m3]"], ["net_volumetric_productivity_kg_m3_day", "Productivity [kg/m3/d]"]];
export const fmt = (value: unknown): string => typeof value === "number" ? String(Number(value.toPrecision(6))) : value === null || value === undefined ? "n/a" : String(value);
export function balanceStatus(results: RunResults): { closed: boolean; text: string } {
  const events = results.event_log.filter((entry) => entry.type === "actions");
  const open = events.filter((entry) => entry.balance?.closed === false).length;
  const aggregate = results.balances?.aggregate;
  const worst = aggregate ? Math.max(0, ...Object.values(aggregate).map((item) => item.residual_rel)) : null;
  const closed = open === 0 && results.status === "succeeded";
  return { closed, text: `${closed ? "Closed" : "Not closed"} · ${events.length - open} of ${events.length} event balances closed${worst !== null ? ` · worst aggregate residual ${fmt(worst)} (relative)` : ""}` };
}
export const bareNum = (value: Num | undefined): number | undefined => typeof value === "number" ? value : undefined;
