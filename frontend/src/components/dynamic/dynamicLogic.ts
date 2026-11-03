// Spec 173: pure logic of the Process dynamic studio. No React, no network. Every function depends only on its arguments.
// The backend (172 engine, draft ops) stays the authority; nothing here clips or corrects a value.

export const NOTICE = "Tier 1 lumped model. Unqualified. Simulation only. This does not command any equipment.";
export const LIMITS = {
  maxDurationS: 120 * 86400, maxOutputPoints: 20000, maxEvents: 1000, maxControllers: 16, minCadenceS: 60, maxActions: 16,
  maxUnits: 12, maxSeriesPoints: 5000, maxCompareRuns: 4,
} as const;
export const ID_PATTERN = "[a-z][a-z0-9_\\-]{0,39}";
const ID_RE = /^[a-z][a-z0-9_-]{0,39}$/;
export const isId = (value: string) => ID_RE.test(value);

export type EventType = "inoculation" | "feed_change" | "setpoint_change" | "harvest" | "dilution" | "actions";
export const EVENT_TYPES: EventType[] = ["actions", "inoculation", "feed_change", "dilution", "setpoint_change", "harvest"];
export const EVENT_TYPE_LABELS: Record<EventType, string> = {
  actions: "Culture actions", inoculation: "Inoculation", feed_change: "Feed change", dilution: "Dilution (splitter or feed)",
  setpoint_change: "Setpoint change", harvest: "Harvest fraction",
};
export type ActionType = "draw" | "separate" | "refill" | "dose" | "inoculate";
export const ACTION_TYPES: ActionType[] = ["draw", "separate", "refill", "dose", "inoculate"];
export type DynAction = { type: ActionType } & Record<string, unknown>;
export type DynCondition = { observed: string; direction: "above" | "below"; threshold: number; hysteresis?: number; cooldown_s?: number; max_fires?: number };
export type DynEvent = { type: EventType; time_s: number; actions?: DynAction[]; condition?: DynCondition } & Record<string, unknown>;
export type DynSchedule = { id: string; events: DynEvent[] };
export type DynController = {
  id: string; type: "onoff" | "pi" | "turbidostat"; measurement: string; unit: string; actuator: string; output_unit: "m3/s" | "1";
  cadence_s: number; setpoint: number; lower: number; upper: number; hysteresis: number; kp: number; ki: number; output: number; direction: "above" | "below";
} & Record<string, unknown>;
export type DynScenario = {
  id: string; units: string[]; profiles: { profile_id: string; digest: string }[]; forcing_profile_id?: string | null; schedule_id?: string | null;
  controllers: string[]; start_utc: string; end_utc: string; output_cadence_s: number; controller_cadence_s: number; fidelity: "T1";
  solver_method: "BDF" | "Adams"; rtol: number; atol: number; temperature_source: "sea_temperature" | "air_temperature" | "unit_mean";
  par_scale: number; par_from_ghi_factor?: number | null; downstream_cadence_s?: number | null; downstream_enabled: boolean;
  initial: Record<string, Record<string, number>>; feed_flows: Record<string, number>; circulation: Record<string, number>;
} & Record<string, unknown>;
export type DynamicContent = { scenarios: Record<string, DynScenario>; schedules: Record<string, DynSchedule>; controllers: Record<string, DynController> };
export const EMPTY_CONTENT: DynamicContent = { scenarios: {}, schedules: {}, controllers: {} };
// Stored content omits fields left at their 172 model default (dynamic_models.Scenario, Controller, Schedule). The editors read complete
// values, so those defaults are restored here; a value that is present is never changed.
const scenarioDefaults = () => ({ forcing_profile_id: null, schedule_id: null, controllers: [], controller_cadence_s: 300, fidelity: "T1", solver_method: "BDF",
  rtol: 1e-6, atol: 1e-9, par_scale: 1, par_from_ghi_factor: null, downstream_cadence_s: null, downstream_enabled: false, initial: {}, feed_flows: {}, circulation: {} });
const controllerDefaults = () => ({ lower: 0, upper: 1, hysteresis: 0, kp: 0, ki: 0, output: 0, direction: "above" });
const withDefaults = <T,>(items: Record<string, unknown> | undefined, defaults: () => Record<string, unknown>): Record<string, T> =>
  Object.fromEntries(Object.entries(items ?? {}).map(([id, value]) => [id, { ...defaults(), id, ...(value as Record<string, unknown>) } as T]));
export const contentOf = (dynamic: Partial<DynamicContent> | undefined | null): DynamicContent => ({
  scenarios: withDefaults<DynScenario>(dynamic?.scenarios, scenarioDefaults),
  schedules: withDefaults<DynSchedule>(dynamic?.schedules, () => ({ events: [] })),
  controllers: withDefaults<DynController>(dynamic?.controllers, controllerDefaults),
});

// ------------------------------------------------------------------ numbers and time

export const fmtNum = (value: unknown): string => {
  if (typeof value !== "number") return value === null || value === undefined ? "n/a" : String(value);
  if (!Number.isFinite(value)) return String(value);
  if (value === 0) return "0";
  const magnitude = Math.abs(value);
  if (magnitude < 1e-3 || magnitude >= 1e6) return Number(value.toPrecision(6)).toExponential().replace("e+", "e");
  return String(Number(value.toPrecision(6)));
};
const TIME_UNITS: [string, number][] = [["d", 86400], ["h", 3600], ["min", 60], ["s", 1]];
export const TIME_UNIT_FACTORS: Record<string, number> = { d: 86400, h: 3600, min: 60, s: 1 };
export function fmtDuration(seconds: number): string {
  if (!Number.isFinite(seconds)) return String(seconds);
  if (seconds === 0) return "0 s";
  for (const [unit, size] of TIME_UNITS) if (Math.abs(seconds) >= size && Math.abs(seconds % size) < 1e-9) return `${fmtNum(seconds / size)} ${unit}`;
  return Math.abs(seconds) >= 86400 ? `${fmtNum(seconds / 86400)} d` : `${fmtNum(seconds)} s`;
}
/** Largest unit that represents the seconds exactly, for editing. */
export function splitDuration(seconds: number): { value: number; unit: string } {
  for (const [unit, size] of TIME_UNITS) if (seconds !== 0 && Math.abs(seconds % size) < 1e-9) return { value: seconds / size, unit };
  return { value: seconds, unit: "s" };
}
export const toSeconds = (value: number, unit: string) => value * (TIME_UNIT_FACTORS[unit] ?? 1);
export const fmtDay = (seconds: number) => fmtNum(seconds / 86400);
const pct = (fraction: number) => `${fmtNum(fraction * 100)} %`;

// ------------------------------------------------------------------ descriptions (decision 6)

const KNOWN_ACTION_KEYS: Record<ActionType, string[]> = {
  draw: ["type", "tank", "volume_m3", "loop_fraction"], separate: ["type", "recovery", "concentration_factor", "return_to"],
  refill: ["type", "tank", "volume_m3", "to_volume_m3", "medium"], dose: ["type", "tank", "nitrogen_kg", "volume_m3"],
  inoculate: ["type", "tank", "volume_m3", "culture"],
};
const KNOWN_EVENT_KEYS = ["type", "time_s", "unit", "stream", "target", "value", "value_unit", "fraction", "medium", "observed", "threshold", "threshold_unit",
  "direction", "hysteresis", "every_s", "count", "end_s", "actions", "condition"];
const KNOWN_CONDITION_KEYS = ["observed", "direction", "threshold", "hysteresis", "cooldown_s", "max_fires"];
const raw = (value: unknown) => JSON.stringify(value);
const extras = (record: Record<string, unknown>, known: string[]) => {
  const unknown = Object.fromEntries(Object.entries(record).filter(([key]) => !known.includes(key)));
  return Object.keys(unknown).length ? ` [unrecognized fields, shown as raw JSON: ${raw(unknown)}]` : "";
};
const concentrations = (map: unknown) => Object.entries((map ?? {}) as Record<string, number>).map(([name, value]) => `${name} ${fmtNum(value)}`).join(", ");

export function describeAction(action: DynAction): string {
  const type = action.type as string;
  if (!(type in KNOWN_ACTION_KEYS)) return `Unrecognized action ${raw(action)}`;
  const known = KNOWN_ACTION_KEYS[type as ActionType];
  const tail = extras(action, known);
  const tank = String(action.tank ?? "?");
  let text: string;
  if (type === "draw") text = `draw ${action.loop_fraction != null ? `${pct(Number(action.loop_fraction))} of the loop` : `${fmtNum(action.volume_m3)} m3`} from tank ${tank}`;
  else if (type === "separate") text = `separate the drawn culture (recovery ${pct(Number(action.recovery))}, concentration factor ${fmtNum(action.concentration_factor)}) and return to ${String(action.return_to ?? "?")}`;
  else if (type === "refill") text = `refill tank ${tank} ${action.to_volume_m3 != null ? `up to ${fmtNum(action.to_volume_m3)} m3` : `with ${fmtNum(action.volume_m3)} m3`} of medium (${concentrations(action.medium)} kg/m3)`;
  else if (type === "dose") text = `dose ${fmtNum(action.nitrogen_kg)} kg N into ${tank}${Number(action.volume_m3 ?? 0) > 0 ? ` in ${fmtNum(action.volume_m3)} m3` : ""}`;
  else text = `inoculate ${tank} with ${fmtNum(action.volume_m3)} m3 of culture (${concentrations(action.culture)} kg/m3)`;
  return text + tail;
}

function describeValue(value: unknown, unit: unknown): string {
  if (value !== null && typeof value === "object") return raw(value);
  return `${fmtNum(value)}${unit ? ` ${String(unit)}` : ""}`;
}

/** The imperative clause of an event, without its timing. */
function clause(event: DynEvent): string {
  const unit = String(event.unit ?? "?");
  switch (event.type) {
    case "actions": return (event.actions ?? []).map(describeAction).join(", then ") || "no actions";
    case "inoculation": {
      const value = event.value;
      return value !== null && typeof value === "object" ? `inoculate ${unit} with culture ${concentrations(value)} kg/m3 (overwrites the listed concentrations)` : `inoculate ${unit} with biomass ${describeValue(value, event.value_unit ?? "kg/m3")} (overwrites the concentration)`;
    }
    case "harvest": return `harvest ${pct(Number(event.fraction))} of ${unit}, replaced by blank medium`;
    case "feed_change": {
      const stream = String(event.stream ?? String(event.target ?? "").replace(/^feed:/, "") ?? "?");
      return `set feed ${stream} to ${describeValue(event.value, event.value_unit ?? "m3/s")}`;
    }
    case "dilution": {
      const target = String(event.target ?? "");
      return target.startsWith("splitter:") ? `set splitter ${target.slice(9)} ratio to ${describeValue(event.value, event.value_unit ?? "1")}`
        : `set feed ${String(event.stream ?? target.replace(/^feed:/, ""))} to ${describeValue(event.value, event.value_unit ?? "m3/s")}`;
    }
    case "setpoint_change": return `set the setpoint of controller ${String(event.target ?? "?")} to ${describeValue(event.value, event.value_unit ?? "kg/m3")}`;
    default: return `unrecognized event type ${raw(event.type)}`;
  }
}

const direction = (value: unknown) => (value === "below" ? "falls below" : "rises above");

export function describeEvent(event: DynEvent): string {
  const body = clause(event);
  const tail = extras(event, KNOWN_EVENT_KEYS) + (event.condition ? extras(event.condition as unknown as Record<string, unknown>, KNOWN_CONDITION_KEYS) : "");
  const start = `day ${fmtDay(Number(event.time_s))}`;
  const condition = event.condition;
  if (condition) {
    const band = [condition.hysteresis !== undefined ? `${fmtNum(condition.hysteresis)} hysteresis` : "", condition.cooldown_s !== undefined ? `${fmtDuration(condition.cooldown_s)} cooldown` : ""].filter(Boolean).join(", ");
    const fires = condition.max_fires ?? 1;
    return `When ${condition.observed} ${direction(condition.direction)} ${fmtNum(condition.threshold)} kg/m3${band ? ` (${band})` : ""}, ${body}, at most ${fires} ${fires === 1 ? "time" : "times"}${Number(event.time_s) > 0 ? `, not before ${start}` : ""}.${tail}`;
  }
  if (event.observed !== undefined && event.observed !== null) {
    const band = event.hysteresis !== undefined && event.hysteresis !== null ? ` (${fmtNum(event.hysteresis)} hysteresis)` : "";
    return `When ${String(event.observed)} ${direction(event.direction)} ${fmtNum(event.threshold)} ${String(event.threshold_unit ?? "kg/m3")}${band}, ${body}${Number(event.time_s) > 0 ? `, not before ${start}` : ""}.${tail}`;
  }
  if (event.every_s !== undefined && event.every_s !== null) {
    const bound = [event.count !== undefined && event.count !== null ? `${event.count} times` : "", event.end_s !== undefined && event.end_s !== null ? `until day ${fmtDay(Number(event.end_s))}` : ""].filter(Boolean).join(", ");
    return `Every ${fmtDuration(Number(event.every_s))} from ${start}, ${body}${bound ? `, ${bound}` : ""}.${tail}`;
  }
  return `At ${start}, ${body}.${tail}`;
}

const stripChannel = (measurement: string) => measurement.split(".").pop() ?? measurement;
export function describeController(c: DynController): string {
  const actuatorKind = String(c.actuator).startsWith("splitter:") ? `harvest ratio on splitter ${String(c.actuator).slice(9)}` : `feed flow of ${String(c.actuator).replace(/^feed:/, "")}`;
  const what = `${stripChannel(c.measurement)} of ${c.unit}`;
  const out = `output ${fmtNum(c.lower)} to ${fmtNum(c.upper)} ${c.output_unit}`;
  const cadence = `every ${fmtDuration(c.cadence_s)}`;
  const tail = extras(c, ["id", "type", "measurement", "unit", "actuator", "output_unit", "cadence_s", "setpoint", "lower", "upper", "hysteresis", "kp", "ki", "output", "direction"]);
  if (c.type === "pi") return `PI keeps ${what} at ${fmtNum(c.setpoint)} kg/m3 by ${actuatorKind}, ${cadence}, ${out} (kp ${fmtNum(c.kp)}, ki ${fmtNum(c.ki)}).${tail}`;
  if (c.type === "onoff") return `On/off keeps ${what} ${c.direction === "below" ? "below" : "above"} ${fmtNum(c.setpoint)} kg/m3 by switching ${actuatorKind} between its lower and upper output, ${cadence}, ${out} (${fmtNum(c.hysteresis)} hysteresis).${tail}`;
  if (c.type === "turbidostat") return `Turbidostat holds ${what} at ${fmtNum(c.setpoint)} kg/m3 by ${actuatorKind}, ${cadence}, ${out}.${tail}`;
  return `Unrecognized controller ${raw(c)}`;
}

// ------------------------------------------------------------------ schedule timeline (decision 5)

export type TimelineRow = { key: string; eventIndex: number; time_s: number; repeat: number | null; conditional: boolean; text: string; type: EventType | string };
/** Events in time order. Repeats of a recurring event are listed read-only; conditional events appear at their earliest time. */
export function timelineRows(events: DynEvent[], horizonS: number): TimelineRow[] {
  const rows: TimelineRow[] = [];
  events.forEach((event, index) => {
    const base = { eventIndex: index, conditional: Boolean(event.condition || event.observed), text: describeEvent(event), type: event.type };
    rows.push({ ...base, key: `${index}:0`, time_s: Number(event.time_s), repeat: event.every_s ? 0 : null });
    if (event.every_s && Number(event.every_s) > 0 && !base.conditional) {
      const limit = Math.min(event.count != null ? Number(event.count) : Infinity, LIMITS.maxEvents);
      const end = Math.min(event.end_s != null ? Number(event.end_s) : Infinity, horizonS);
      for (let k = 1; k < limit; k += 1) {
        const time = Number(event.time_s) + k * Number(event.every_s);
        if (time > end + 1e-9) break;
        rows.push({ ...base, key: `${index}:${k}`, time_s: time, repeat: k });
      }
    }
  });
  return rows.sort((a, b) => a.time_s - b.time_s || a.eventIndex - b.eventIndex || (a.repeat ?? 0) - (b.repeat ?? 0));
}

export const scenarioDurationS = (scenario: Pick<DynScenario, "start_utc" | "end_utc">): number | null => {
  const start = Date.parse(scenario.start_utc), end = Date.parse(scenario.end_utc);
  return Number.isFinite(start) && Number.isFinite(end) ? (end - start) / 1000 : null;
};
export const hasUtcOffset = (value: string) => /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));

/** Hints that name what the backend would refuse. They never change a value and the server verdict stays authoritative. */
export function scenarioHints(scenario: DynScenario): string[] {
  const hints: string[] = [];
  const duration = scenarioDurationS(scenario);
  if (!hasUtcOffset(scenario.start_utc) || !hasUtcOffset(scenario.end_utc)) hints.push("Start and end must be ISO timestamps with a UTC offset, for example 2026-01-01T00:00:00Z.");
  else if (duration !== null && duration <= 0) hints.push("End must be after start.");
  else if (duration !== null && duration > LIMITS.maxDurationS) hints.push(`Duration ${fmtDuration(duration)} exceeds the limit of 120 d.`);
  if (scenario.output_cadence_s < LIMITS.minCadenceS) hints.push(`Output cadence must be at least ${LIMITS.minCadenceS} s.`);
  if (scenario.controller_cadence_s < LIMITS.minCadenceS) hints.push(`Controller cadence must be at least ${LIMITS.minCadenceS} s.`);
  if (duration && duration > 0 && scenario.output_cadence_s > 0 && duration / scenario.output_cadence_s > LIMITS.maxOutputPoints) hints.push(`${Math.ceil(duration / scenario.output_cadence_s)} output points exceed the limit of ${LIMITS.maxOutputPoints}.`);
  if (!scenario.units.length) hints.push("Choose at least one participating unit.");
  if (!scenario.profiles.length) hints.push("Choose an environment profile.");
  if (scenario.controllers.length > LIMITS.maxControllers) hints.push(`At most ${LIMITS.maxControllers} controllers.`);
  if (scenario.downstream_enabled && scenario.downstream_cadence_s != null && scenario.downstream_cadence_s < 3600) hints.push("Downstream cadence must be at least 3600 s.");
  return hints;
}
export const scenarioLimitText = (scenario: DynScenario): string => {
  const duration = scenarioDurationS(scenario);
  const points = duration && duration > 0 && scenario.output_cadence_s > 0 ? Math.floor(duration / scenario.output_cadence_s) + 1 : null;
  return `Limits: duration at most 120 d (now ${duration === null ? "n/a" : fmtDuration(duration)}), at most ${LIMITS.maxOutputPoints} output points (now ${points ?? "n/a"}), cadences at least ${LIMITS.minCadenceS} s, at most ${LIMITS.maxEvents} events and ${LIMITS.maxControllers} controllers. The server verdict is authoritative.`;
};

// ------------------------------------------------------------------ blank content and ops

/** Editable starting culture state filled in for a unit chosen for a scenario; the operator edits it, nothing is corrected. */
export const startingState = (): Record<string, number> => ({ X: 0.2, N: 0.05, O2: 0.008 });
export const blankScenario = (id: string, units: string[], profile: { profile_id: string; digest: string; start: string; end: string } | null): DynScenario => {
  const startMs = profile ? Date.parse(profile.start) : Date.parse("2026-01-01T00:00:00Z");
  const endMs = profile ? Math.min(Date.parse(profile.end), startMs + 7 * 86400000) : startMs + 7 * 86400000;
  const iso = (ms: number) => (Number.isFinite(ms) ? new Date(ms).toISOString().replace(".000Z", "Z") : "");
  return {
    id, units, profiles: profile ? [{ profile_id: profile.profile_id, digest: profile.digest }] : [], schedule_id: null, controllers: [],
    start_utc: iso(startMs), end_utc: iso(endMs), output_cadence_s: 3600, controller_cadence_s: 300, fidelity: "T1", solver_method: "BDF",
    rtol: 1e-6, atol: 1e-9, temperature_source: "unit_mean", par_scale: 1, downstream_enabled: false, initial: Object.fromEntries(units.map((tag) => [tag, startingState()])), feed_flows: {}, circulation: {},
  };
};
export const blankController = (id: string, unit: string, actuator: string): DynController => ({
  id, type: "pi", measurement: "X", unit, actuator, output_unit: actuator.startsWith("splitter:") ? "1" : "m3/s", cadence_s: 300, setpoint: 0.5,
  lower: 0, upper: actuator.startsWith("splitter:") ? 1 : 1e-5, hysteresis: 0, kp: 0, ki: 0, output: 0, direction: "above",
});
export function blankAction(type: ActionType, tank: string): DynAction {
  switch (type) {
    case "draw": return { type, tank, loop_fraction: 0.2 };
    case "separate": return { type, recovery: 0.9, concentration_factor: 5, return_to: tank };
    case "refill": return { type, tank, volume_m3: 0.1, medium: { N: 0.5, O2: 0.008 } };
    case "dose": return { type, tank, nitrogen_kg: 0.1 };
    default: return { type, tank, volume_m3: 0.1, culture: { X: 1, N: 0.05, O2: 0.008 } };
  }
}
export function blankEvent(type: EventType, tank: string): DynEvent {
  switch (type) {
    case "actions": return { type, time_s: 86400, actions: [blankAction("draw", tank)] };
    case "inoculation": return { type, time_s: 0, unit: tank, value: { X: 0.5 }, value_unit: "kg/m3" };
    case "feed_change": return { type, time_s: 86400, stream: "", value: 0, value_unit: "m3/s" };
    case "dilution": return { type, time_s: 86400, target: "splitter:", value: 0.5, value_unit: "1" };
    case "setpoint_change": return { type, time_s: 86400, target: "", value: 0.5, value_unit: "kg/m3" };
    default: return { type: "harvest", time_s: 86400, unit: tank, fraction: 0.2 };
  }
}
export function blankCondition(unit: string): DynCondition {
  return { observed: `${unit}.loop_N`, direction: "below", threshold: 0.05, hysteresis: 0, cooldown_s: 0, max_fires: 1 };
}
/** Sets a nested field and removes it (and an emptied parent record) when undefined, so no placeholder is left. */
export function setPath<T extends Record<string, unknown>>(target: T, path: string[], value: unknown): T {
  const [head, ...rest] = path;
  const next: Record<string, unknown> = { ...target };
  if (!rest.length) {
    if (value === undefined) delete next[head]; else next[head] = value;
    return next as T;
  }
  const child = setPath((target[head] ?? {}) as Record<string, unknown>, rest, value);
  if (Object.keys(child).length) next[head] = child; else delete next[head];
  return next as T;
}
export type EventShape = "once" | "recurring" | "condition";
export const eventShape = (event: DynEvent): EventShape => (event.condition || event.observed != null ? "condition" : event.every_s != null ? "recurring" : "once");
/** Changing the shape removes the other shapes' fields, since recurrence and condition are mutually exclusive. */
export function setEventShape(event: DynEvent, shape: EventShape, unit: string): DynEvent {
  const next: Record<string, unknown> = { ...event };
  for (const key of ["every_s", "count", "end_s", "condition", "observed", "threshold", "threshold_unit", "direction", "hysteresis"]) delete next[key];
  if (shape === "recurring") { next.every_s = event.every_s ?? 86400; next.count = event.count ?? 2; }
  if (shape === "condition") {
    if (event.type === "actions") next.condition = blankCondition(unit);
    else Object.assign(next, { observed: `${unit}.X`, threshold: 0.5, threshold_unit: "kg/m3", direction: "above", hysteresis: 0 });
  }
  return next as DynEvent;
}
export const changeEventType = (event: DynEvent, type: EventType, tank: string): DynEvent => {
  const fresh = blankEvent(type, tank);
  const kept: Record<string, unknown> = { time_s: event.time_s };
  for (const key of ["every_s", "count", "end_s"]) if (event[key] !== undefined) kept[key] = event[key];
  return { ...fresh, ...kept } as DynEvent;
};
/** Pure client hints before save; the backend remains the authority. */
export function eventIssues(event: DynEvent): string[] {
  const issues: string[] = [];
  if (!(Number(event.time_s) >= 0)) issues.push("Time must be zero or later.");
  if (event.every_s != null && event.count == null && event.end_s == null) issues.push("A recurring event needs a count or an end.");
  if (event.every_s != null && event.condition) issues.push("An event cannot both recur and carry a state condition.");
  if (event.type === "actions" && !(event.actions ?? []).length) issues.push("Add at least one action.");
  if (event.type !== "actions" && event.actions) issues.push("Only an actions event carries actions.");
  for (const action of event.actions ?? []) {
    if (action.type === "draw" && (action.volume_m3 == null) === (action.loop_fraction == null)) issues.push("A draw needs exactly one of volume or loop fraction.");
    if (action.type === "refill" && (action.volume_m3 == null) === (action.to_volume_m3 == null)) issues.push("A refill needs exactly one of volume or fill-to volume.");
  }
  return issues;
}
const without = (value: Record<string, unknown>, key = "id") => Object.fromEntries(Object.entries(value).filter(([name]) => name !== key));
export const setOp = (kind: "schedule" | "controller" | "scenario", value: { id: string }) => ({ op: `set_${kind}`, id: value.id, value: without(value as Record<string, unknown>) });
export const deleteOp = (kind: "schedule" | "controller" | "scenario", id: string) => ({ op: `delete_${kind}`, id });
/** Which scenarios and controllers still reference a schedule or controller. */
export const referencedBy = (content: DynamicContent, kind: "schedule" | "controller", id: string): string[] =>
  Object.values(content.scenarios).filter((s) => (kind === "schedule" ? s.schedule_id === id : s.controllers.includes(id))).map((s) => s.id);

// ------------------------------------------------------------------ 191 link (decision 8)

export type LinkInfo = { id: string; schedule_id: string; version_label: string; state: "applied" | "modified" | "missing"; state_text?: string; latest_version_label?: string | null; update_available?: boolean };
export const linkBadge = (link: LinkInfo | null | undefined): { text: string; tone: "ok" | "warn" | "danger" } | null => {
  if (!link) return null;
  if (link.state === "modified") return { text: `Studio procedure ${link.version_label}: modified, no longer the pinned procedure.`, tone: "warn" };
  if (link.state === "missing") return { text: `Studio procedure ${link.version_label}: the linked schedule is missing.`, tone: "danger" };
  return { text: `Studio procedure ${link.version_label}: applied, unmodified.`, tone: "ok" };
};
export const needsLinkConfirmation = (link: LinkInfo | null | undefined) => Boolean(link && link.state !== "missing");
export const linkConfirmation = (link: LinkInfo, action: "edit" | "delete"): string =>
  `Schedule "${link.schedule_id}" is the applied Studio procedure ${link.version_label}. ` +
  (action === "delete" ? "Deleting it removes the schedule from this draft; the link then reads as missing. " : link.state === "modified" ? "It already reads modified. " : "Saving this edit makes the link read \"modified, no longer the pinned procedure\". ") +
  "The editor never rewrites or removes the link, never publishes, and does not apply procedures. Apply and Update stay in Studio. Continue?";

// ------------------------------------------------------------------ runs, staleness (decision 12)

export type RunError = { code?: string; message?: string } | null;
export type DynRun = {
  job_id: string; status: string; progress?: number; error?: RunError; scenario_id: string; created_at?: string; finished_at?: string;
  snapshot_digest?: string | null; current?: boolean | null; cancel_requested?: boolean; cancel_outcome?: string;
  identity?: { draft_revision?: string | null; content_digest?: string | null; snapshot_digest?: string | null; scenario_id?: string;
    card_refs?: { tag?: string; card_id?: string; card_revision?: string; card_digest?: string; parameter_set_digest?: string }[]; profile_refs?: { profile_id?: string; digest?: string }[] };
};
export const TERMINAL = ["succeeded", "failed", "cancelled", "interrupted"];
export const isTerminal = (status: string) => TERMINAL.includes(status);
export const revisionSeq = (revision: string | null | undefined): number | null => {
  const head = String(revision ?? "").split(":")[0];
  const seq = Number(head);
  return head !== "" && Number.isInteger(seq) ? seq : null;
};
export type Staleness = { state: "current" | "stale" | "unbound"; label: string; detail: string };
export const SNAPSHOT_CAVEAT = "The current flag compares the draft content digest only. It does not cover a changed environment profile or model card; Start refuses those. The profile and card digests this run used are listed in its provenance.";
export function staleness(run: Pick<DynRun, "current" | "identity">, headSeq: number): Staleness {
  const runSeq = revisionSeq(run.identity?.draft_revision);
  if (run.current === null || run.current === undefined) return { state: "unbound", label: "Not bound", detail: "This run is not bound to a draft content digest, so it cannot be compared with the draft." };
  if (run.current) return { state: "current", label: "Current", detail: "The draft content is unchanged since this run (layout moves do not count)." };
  return { state: "stale", label: "Stale", detail: `Stale: this run used draft revision ${runSeq ?? "unknown"}; the draft is now at revision ${headSeq}. A scenario, schedule, controller or unit edit changed its content.` };
}
export const runTitle = (run: Pick<DynRun, "job_id" | "scenario_id">, state?: Staleness) => `${run.scenario_id} · ${run.job_id.slice(0, 8)}${state ? ` · ${state.label.toLowerCase()}` : ""}`;

// ------------------------------------------------------------------ guided path (Design › Dynamic)

export type DynamicTab = "plant" | "scenario" | "schedule" | "controller" | "runs" | "compare";
/** One numbered step; `sections` are the panel sections it opens, the first on click. */
export type DynamicStep = { n: 1 | 2 | 3 | 4; title: string; status: string; done: boolean; sections: readonly DynamicTab[] };
const counted = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;
const newest = (runs: readonly DynRun[]) => runs.reduce<DynRun | undefined>((best, run) => !best || (run.created_at ?? "") > (best.created_at ?? "") ? run : best, undefined);

/** Each step's state, read from the draft's saved dynamic content and its runs; `runs` is null while they are still being read. */
export function dynamicSteps(content: DynamicContent, runs: readonly DynRun[] | null): DynamicStep[] {
  const scenarios = Object.keys(content.scenarios).length, schedules = Object.keys(content.schedules).length, controllers = Object.keys(content.controllers).length;
  const active = runs?.filter((run) => !isTerminal(run.status)).length ?? 0;
  const succeeded = runs?.filter((run) => run.status === "succeeded").length ?? 0;
  const latest = runs ? newest(runs) : undefined;
  const runStatus = runs === null ? "Checking runs…" : active ? `${counted(active, "run")} in progress`
    : latest ? `${counted(runs.length, "run")} · latest ${latest.status}${latest.current === false ? ", stale" : ""}` : scenarios ? "No run yet" : "Needs a scenario first";
  return [
    { n: 1, title: "Scenario", status: scenarios ? `${counted(scenarios, "scenario")} saved` : "No scenario yet", done: scenarios > 0, sections: ["scenario"] },
    { n: 2, title: "Schedule & controllers", status: `Optional · ${schedules ? counted(schedules, "schedule") : "no schedule"}, ${controllers ? counted(controllers, "controller") : "no controller"}`,
      done: schedules + controllers > 0, sections: ["schedule", "controller"] },
    { n: 3, title: "Run", status: runStatus, done: Boolean(runs?.some((run) => isTerminal(run.status))), sections: ["runs"] },
    { n: 4, title: "Results & compare", status: runs === null ? "Checking runs…" : succeeded ? `${counted(succeeded, "succeeded run")} to read${succeeded > 1 ? " or compare" : ""}` : "No results yet",
      done: succeeded > 0, sections: ["plant", "compare"] },
  ];
}

/** Where the guided path opens until the operator picks a step: the first step still to do; undefined while runs are unread. */
export function suggestedTab(content: DynamicContent, runs: readonly DynRun[] | null): DynamicTab | undefined {
  if (!Object.keys(content.scenarios).length) return "scenario";
  if (runs === null) return undefined;
  return runs.length ? "plant" : "runs";
}

// ------------------------------------------------------------------ manifest views (decisions 9-11)

export type Manifest = {
  fidelity?: string; snapshot_digest?: string; draft_revision?: string; content_digest?: string; scenario_id?: string; channels?: Record<string, string>;
  profiles?: { profile_id: string; digest: string; resolution_minutes?: number | null; consumed_channels?: string[]; temporal_rule?: Record<string, string> }[];
  units?: { tag: string; model_card_qualification?: unknown; card_id?: string; card_revision?: string; card_digest?: string; parameter_set_digest?: string }[];
  solver?: { method?: string; rtol?: number; atol?: number; owner?: string };
  event_log?: Record<string, unknown>[]; controller_log?: Record<string, unknown>[]; downstream_outcomes?: Record<string, unknown>[];
  diagnostics?: { wall_time_s?: number };
  balances?: { aggregate?: Record<string, { residual_abs_kg: number; residual_rel: number }>; asserted?: boolean } | null;
  campaign?: { loops: Record<string, { cycles: Record<string, number | boolean | null>[]; summary: Record<string, unknown> }>; duration_s?: number } | null;
  productivity?: { harvest_kg?: number; volumetric_kg_m3_day?: number | null; areal_kg_m2_day?: number | null; areal_unavailable_reason?: string | null; energy_per_kg?: number | null; energy_per_kg_unavailable_reason?: string | null } | null;
};
export type SeriesData = { channels: Record<string, number[]>; returned_points: number; total_points: number; stride: number };

export type Chip = { id: string; label: string; tone: "ok" | "warn" | "danger" | "neutral"; text: string };
export function balanceTolerance(manifest: Manifest): number { return Math.max(1e-6, 100 * (manifest.solver?.rtol ?? 1e-6)); }
export function chips(manifest: Manifest | null, run: Pick<DynRun, "status" | "error">): Chip[] {
  const out: Chip[] = [];
  if (!manifest) {
    return [{ id: "convergence", label: "Convergence", tone: run.status === "succeeded" ? "ok" : run.status === "failed" ? "danger" : "neutral", text: run.status === "failed" ? `Run failed${run.error?.code ? ` (${run.error.code})` : ""}` : `Run ${run.status}` }];
  }
  const aggregate = manifest.balances?.aggregate ?? {};
  const worst = Math.max(0, ...Object.values(aggregate).map((entry) => entry.residual_rel));
  const tolerance = balanceTolerance(manifest);
  if (!Object.keys(aggregate).length) out.push({ id: "balance", label: "Balance", tone: "neutral", text: "Balance not available: the run recorded no aggregate balance." });
  else out.push({ id: "balance", label: "Balance", tone: worst <= tolerance ? "ok" : "danger", text: `Aggregate residual ${fmtNum(worst)} relative against tolerance ${fmtNum(tolerance)}; ${manifest.balances?.asserted ? "asserted by the engine (action events present)" : "not asserted (no action events)"}.` });
  const actionEvents = (manifest.event_log ?? []).filter((entry) => entry.type === "actions");
  const closed = actionEvents.filter((entry) => (entry.balance as { closed?: boolean } | undefined)?.closed !== false).length;
  out.push({ id: "events", label: "Event balances", tone: actionEvents.length === 0 ? "neutral" : closed === actionEvents.length ? "ok" : "danger", text: actionEvents.length ? `${closed} of ${actionEvents.length} action events closed their balance.` : "No action events." });
  const downstream = manifest.downstream_outcomes ?? [];
  const unconverged = downstream.filter((entry) => entry.status === "downstream_unconverged").length;
  const residuals = downstream.map((entry) => entry.residual ?? entry.max_residual).filter((value): value is number => typeof value === "number");
  out.push({ id: "convergence", label: "Convergence", tone: run.status === "failed" ? "danger" : unconverged ? "warn" : "ok",
    text: `Job ${run.status}${run.error?.code ? ` (${run.error.code})` : ""}. ${downstream.length ? `Downstream: ${downstream.length - unconverged} converged, ${unconverged} unconverged${residuals.length ? `, recorded residual up to ${fmtNum(Math.max(...residuals))}` : ""}.` : "No downstream."}` });
  return out;
}

export type Kpi = { id: string; group: string; label: string; value: number | null; unit: string; reason?: string };
const timeMean = (t: number[], y: number[]): number | null => {
  if (t.length < 2 || t[t.length - 1] <= t[0]) return null;
  let area = 0;
  for (let i = 1; i < t.length; i += 1) area += ((y[i] + y[i - 1]) / 2) * (t[i] - t[i - 1]);
  return area / (t[t.length - 1] - t[0]);
};
const na = (id: string, group: string, label: string, unit: string, reason: string): Kpi => ({ id, group, label, value: null, unit, reason });
export function kpis(manifest: Manifest | null, series: SeriesData | null): Kpi[] {
  const out: Kpi[] = [];
  const t = series?.channels.t_s ?? [];
  const units = (manifest?.units ?? []).map((unit) => unit.tag);
  for (const tag of units) {
    const y = series?.channels[`${tag}_X`];
    if (y?.length) {
      out.push({ id: `final_${tag}`, group: "Biomass", label: `Final biomass concentration, ${tag}`, value: y[y.length - 1], unit: "kg/m3" });
      const mean = timeMean(t, y);
      out.push(mean === null ? na(`mean_${tag}`, "Biomass", `Mean biomass concentration, ${tag}`, "kg/m3", "The run has fewer than two distinct time points.")
        : { id: `mean_${tag}`, group: "Biomass", label: `Mean biomass concentration, ${tag}`, value: mean, unit: "kg/m3" });
    } else out.push(na(`final_${tag}`, "Biomass", `Biomass concentration, ${tag}`, "kg/m3", "No biomass series was read for this unit."));
  }
  const harvest = series?.channels.harvest_X_kg;
  out.push(harvest?.length ? { id: "harvest", group: "Harvest", label: "Cumulative harvest", value: harvest[harvest.length - 1], unit: "kg" } : na("harvest", "Harvest", "Cumulative harvest", "kg", "No harvest series was read."));
  out.push({ id: "events", group: "Counts", label: "Events fired", value: manifest ? (manifest.event_log ?? []).length : null, unit: "1", reason: manifest ? undefined : "No manifest." });
  out.push({ id: "controller_fires", group: "Counts", label: "Controller samples", value: manifest ? (manifest.controller_log ?? []).length : null, unit: "1", reason: manifest ? undefined : "No manifest." });
  const productivity = manifest?.productivity;
  if (productivity) {
    out.push(productivity.volumetric_kg_m3_day != null ? { id: "vol_prod", group: "Productivity", label: "Volumetric productivity", value: productivity.volumetric_kg_m3_day, unit: "kg/m3/d" } : na("vol_prod", "Productivity", "Volumetric productivity", "kg/m3/d", "The manifest does not report it."));
    out.push(productivity.areal_kg_m2_day != null ? { id: "areal_prod", group: "Productivity", label: "Areal productivity", value: productivity.areal_kg_m2_day, unit: "kg/m2/d" } : na("areal_prod", "Productivity", "Areal productivity", "kg/m2/d", productivity.areal_unavailable_reason ?? "The manifest does not report it."));
  } else out.push(na("vol_prod", "Productivity", "Volumetric productivity", "kg/m3/d", "The manifest does not report productivity for this run."));
  for (const [loop, data] of Object.entries(manifest?.campaign?.loops ?? {})) {
    const summary = data.summary;
    const num = (key: string) => (typeof summary[key] === "number" ? (summary[key] as number) : null);
    out.push({ id: `campaign_draws_${loop}`, group: "Campaign", label: `Draws, loop ${loop}`, value: num("draws"), unit: "1" });
    out.push({ id: `campaign_harvest_${loop}`, group: "Campaign", label: `Harvested biomass, loop ${loop}`, value: num("harvested_biomass_kg"), unit: "kg" });
    const prod = num("mean_last_k_net_volumetric_productivity_kg_m3_day");
    out.push(prod === null ? na(`campaign_prod_${loop}`, "Campaign", `Mean last-${String(summary.last_k ?? "k")} productivity, loop ${loop}`, "kg/m3/d", "Fewer than one regular cycle.") : { id: `campaign_prod_${loop}`, group: "Campaign", label: `Mean last-${String(summary.last_k)} productivity, loop ${loop}`, value: prod, unit: "kg/m3/d" });
  }
  return out;
}
export const fmtKpi = (kpi: Kpi) => (kpi.value === null ? `not available${kpi.reason ? `: ${kpi.reason}` : ""}` : `${fmtNum(kpi.value)} ${kpi.unit === "1" ? "" : kpi.unit}`.trim());

// Spec 211: explicit metric ownership. A unit or culture-loop value must never be promoted to a plant total.
export type PlantMetric = Kpi & { scope: "plant" | "equipment" | "loop" | "unclassified"; owner: string | null; qualification: string };
export const comparablePlantMetrics = (source: PlantMetric, candidate: PlantMetric) =>
  source.id === candidate.id && source.label === candidate.label && source.unit === candidate.unit;
const PLANT_METRICS = new Set(["harvest", "events", "controller_fires", "vol_prod", "areal_prod"]);
export function plantMetrics(manifest: Manifest | null, series: SeriesData | null): PlantMetric[] {
  const unitTags = (manifest?.units ?? []).map((unit) => unit.tag).sort((a, b) => b.length - a.length);
  const loopTags = Object.keys(manifest?.campaign?.loops ?? {}).sort((a, b) => b.length - a.length);
  return kpis(manifest, series).map((candidate) => {
    const exactHarvest = manifest?.productivity?.harvest_kg;
    const metric = candidate.id === "harvest" && typeof exactHarvest === "number" && Number.isFinite(exactHarvest)
      ? { ...candidate, value: exactHarvest, reason: undefined } : candidate;
    let scope: PlantMetric["scope"] = "unclassified";
    let owner: string | null = null;
    if (PLANT_METRICS.has(metric.id)) scope = "plant";
    else {
      const unit = unitTags.find((tag) => metric.id === `final_${tag}` || metric.id === `mean_${tag}`);
      const loop = loopTags.find((tag) => metric.id === `campaign_draws_${tag}` || metric.id === `campaign_harvest_${tag}` || metric.id === `campaign_prod_${tag}`);
      if (unit) { scope = "equipment"; owner = unit; }
      else if (loop) { scope = "loop"; owner = loop; }
    }
    const card = scope === "equipment" ? manifest?.units?.find((unit) => unit.tag === owner)?.model_card_qualification : null;
    const sampled = metric.id.startsWith("mean_") && (series?.stride ?? 1) > 1;
    const qualification = ["Tier 1 · unqualified simulation", metric.id === "harvest" && typeof exactHarvest === "number" ? "engine manifest aggregate" : "",
      card == null ? "" : `model card: ${typeof card === "string" ? card : JSON.stringify(card)}`,
      sampled ? `mean of ${series?.returned_points ?? "?"} sampled points from ${series?.total_points ?? "?"} (stride ${series?.stride})` : ""].filter(Boolean).join(" · ");
    return { ...metric, scope, owner, qualification };
  });
}

export type PlantChoices = { runIds: string[]; metricIds: string[]; overviewIds: string[]; equipmentId: string | null; view: "table" | "charts" };
export const DEFAULT_PLANT_OVERVIEW = ["harvest", "vol_prod", "events"];
export const EMPTY_PLANT_CHOICES: PlantChoices = { runIds: [], metricIds: [], overviewIds: DEFAULT_PLANT_OVERVIEW, equipmentId: null, view: "table" };
export const plantChoiceKey = (workspaceId: string, draftId: string) => `jarvis.dynamic.plant.v1:${workspaceId}:${draftId}`;
/** Restored browser choices are display-only. Unknown IDs are removed and reported, never used as result authority. */
export function restorePlantChoices(raw: unknown, runIds: string[], metricIds: string[], equipmentIds: string[], plantIds: string[]): { choices: PlantChoices; removed: string[] } {
  const saved = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
  const removed: string[] = [];
  const keep = (value: unknown, allowed: string[], kind: string, cap: number) => {
    const ids = Array.isArray(value) ? value.filter((id): id is string => typeof id === "string") : [];
    const unique = [...new Set(ids)];
    for (const id of unique) if (!allowed.includes(id)) removed.push(`${kind} ${id}`);
    return unique.filter((id) => allowed.includes(id)).slice(0, cap);
  };
  const selectedRuns = keep(saved.runIds, runIds, "run", 4);
  const selectedMetrics = keep(saved.metricIds, metricIds, "metric", 50);
  const overviewIds = keep(saved.overviewIds ?? DEFAULT_PLANT_OVERVIEW, plantIds, "overview KPI", 5);
  const equipmentId = typeof saved.equipmentId === "string" && equipmentIds.includes(saved.equipmentId) ? saved.equipmentId : null;
  if (saved.equipmentId && !equipmentId) removed.push(`equipment ${String(saved.equipmentId)}`);
  return { choices: { runIds: selectedRuns, metricIds: selectedMetrics, overviewIds, equipmentId, view: saved.view === "charts" ? "charts" : "table" }, removed };
}

export function movePlantMetric(ids: string[], id: string, offset: -1 | 1): string[] {
  const index = ids.indexOf(id), next = index + offset;
  if (index < 0 || next < 0 || next >= ids.length) return ids;
  const moved = [...ids]; [moved[index], moved[next]] = [moved[next], moved[index]];
  return moved;
}
/** Provenance rows the results view lists: the digests this run used, never a claim they are unchanged. */
export function provenanceRows(manifest: Manifest | null, run: Pick<DynRun, "identity" | "snapshot_digest">): { label: string; value: string }[] {
  const rows: { label: string; value: string }[] = [];
  rows.push({ label: "Fidelity", value: manifest?.fidelity ?? "T1" });
  rows.push({ label: "Draft revision", value: run.identity?.draft_revision ?? "n/a" });
  rows.push({ label: "Snapshot digest", value: manifest?.snapshot_digest ?? run.snapshot_digest ?? run.identity?.snapshot_digest ?? "n/a" });
  for (const unit of manifest?.units ?? []) rows.push({ label: `Model card, ${unit.tag}`, value: `${typeof unit.model_card_qualification === "string" ? unit.model_card_qualification : raw(unit.model_card_qualification ?? "no qualification recorded")} · ${unit.card_id ?? "no card"} r${unit.card_revision ?? "?"} · card ${unit.card_digest ?? "n/a"} · parameters ${unit.parameter_set_digest ?? "n/a"}` });
  for (const profile of manifest?.profiles ?? []) rows.push({ label: `Profile ${profile.profile_id.slice(0, 19)}`, value: `digest ${profile.digest} · ${profile.resolution_minutes ?? "irregular"} min · ${profile.temporal_rule ? Object.entries(profile.temporal_rule).map(([k, v]) => `${k}: ${v}`).join("; ") : "no temporal rule recorded"}` });
  if (manifest?.solver) rows.push({ label: "Solver", value: `${manifest.solver.method ?? "?"} rtol ${fmtNum(manifest.solver.rtol)} atol ${fmtNum(manifest.solver.atol)}` });
  if (manifest?.diagnostics?.wall_time_s != null) rows.push({ label: "Wall time", value: `${fmtNum(manifest.diagnostics.wall_time_s)} s` });
  return rows;
}

// ------------------------------------------------------------------ channels

export const SKIP_CHANNELS = ["t_s"];
/** Groups channels by their manifest unit. A channel's unit is never dropped. */
export function groupChannelsByUnit(channels: Record<string, string>): { unit: string; names: string[] }[] {
  const groups = new Map<string, string[]>();
  for (const [name, unit] of Object.entries(channels)) {
    if (SKIP_CHANNELS.includes(name)) continue;
    groups.set(unit, [...(groups.get(unit) ?? []), name]);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([unit, names]) => ({ unit, names: names.sort() }));
}
export function defaultChannels(channels: Record<string, string>): string[] {
  const names = Object.keys(channels).filter((name) => !SKIP_CHANNELS.includes(name));
  const picked = [...names.filter((name) => name.endsWith("_X") && !name.includes("harvest")).slice(0, 3), ...names.filter((name) => name === "harvest_X_kg"), ...names.filter((name) => name.endsWith("_V_m3")).slice(0, 2)];
  return picked.length ? picked : names.slice(0, 3);
}
export const channelLabel = (name: string, unit: string | undefined) => `${name} [${unit ?? "unit not recorded"}]`;

// ------------------------------------------------------------------ series paging and CSV (decision 13)

export type SeriesFetch = (query: { channels: string[]; start_s?: number; end_s?: number; max_points: number }) => Promise<SeriesData>;
export class ExportRefused extends Error {}
const chunk = <T,>(items: T[], size: number): T[][] => Array.from({ length: Math.ceil(items.length / size) }, (_, i) => items.slice(i * size, (i + 1) * size));
/**
 * Reads every row of the requested channels by paging t_s windows of at most 5000 points. A window is inclusive on both ends, so a
 * page after the first drops the rows at or before the previous window's end. A strided or short result is refused, never padded.
 */
export async function collectSeries(fetchPage: SeriesFetch, channels: string[], options: { windowPoints?: number; channelsPerRequest?: number } = {}): Promise<{ columns: Record<string, number[]>; rows: number }> {
  const pageSize = options.windowPoints ?? 4000;
  const names = ["t_s", ...channels.filter((name) => name !== "t_s")];
  const overview = await fetchPage({ channels: ["t_s"], max_points: LIMITS.maxSeriesPoints });
  const total = overview.total_points;
  const ticks = overview.channels.t_s ?? [];
  const columns: Record<string, number[]> = Object.fromEntries(names.map((name) => [name, []]));
  if (total === 0) return { columns, rows: 0 };
  const step = Math.max(1, Math.floor(pageSize / Math.max(1, overview.stride)));
  const readWindow = async (first: number, last: number, final: boolean): Promise<Record<string, number[]>[]> => {
    const query = { start_s: ticks[first], ...(final ? {} : { end_s: ticks[last] }) };
    const pages: Record<string, number[]>[] = [];
    let strided = false;
    const rest = names.filter((name) => name !== "t_s");
    const groups = rest.length ? chunk(rest, options.channelsPerRequest ?? 20) : [["t_s"]];
    for (const group of groups) {
      const page = await fetchPage({ ...query, channels: group, max_points: LIMITS.maxSeriesPoints });
      if (page.stride > 1) { strided = true; break; }
      pages.push(page.channels);
    }
    if (strided) {
      if (last - first <= 1) throw new ExportRefused("A window of the series could not be read without striding, so the export was refused instead of dropping rows.");
      const middle = Math.floor((first + last) / 2);
      return [...(await readWindow(first, middle, false)), ...(await readWindow(middle, last, final))];
    }
    const merged: Record<string, number[]> = {};
    for (const part of pages) Object.assign(merged, part);
    return [merged];
  };
  let previousEnd = -Infinity;
  const append = (page: Record<string, number[]>) => {
    const t = page.t_s ?? [];
    for (let i = 0; i < t.length; i += 1) {
      if (t[i] <= previousEnd) continue;
      for (const name of names) columns[name].push(page[name]?.[i] as number);
    }
  };
  const ranges: [number, number, boolean][] = [];
  for (let first = 0; first < ticks.length; first += step) {
    const last = Math.min(first + step, ticks.length - 1);
    ranges.push([first, last, last >= ticks.length - 1]);
    if (last >= ticks.length - 1) break;
  }
  for (const [first, last, final] of ranges) {
    for (const page of await readWindow(first, last, final)) {
      const pageEnd = Math.max(...(page.t_s ?? [-Infinity]));
      append(page);
      previousEnd = Math.max(previousEnd, pageEnd);
    }
  }
  if (columns.t_s.length !== total) throw new ExportRefused(`Read ${columns.t_s.length} of ${total} rows. The export was refused because it would be truncated.`);
  return { columns, rows: total };
}

export type CsvInput = { jobId: string; snapshotDigest: string; fidelity: string; scenarioId?: string; channels: string[]; units: Record<string, string>; columns: Record<string, number[]> };
const csvCell = (value: unknown) => {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
};
export function buildCsv(input: CsvInput): string {
  const names = ["t_s", ...input.channels.filter((name) => name !== "t_s")];
  const rows = input.columns.t_s?.length ?? 0;
  for (const name of names) if ((input.columns[name]?.length ?? -1) !== rows) throw new ExportRefused(`Channel ${name} has ${input.columns[name]?.length ?? 0} rows, expected ${rows}. The export was refused.`);
  const lines = [`# job_id=${input.jobId}; snapshot_digest=${input.snapshotDigest}; fidelity=${input.fidelity}${input.scenarioId ? `; scenario=${input.scenarioId}` : ""}; rows=${rows}`, `# ${NOTICE}`,
    names.map((name) => csvCell(`${name} [${input.units[name] ?? (name === "t_s" ? "s" : "unit not recorded")}]`)).join(",")];
  for (let i = 0; i < rows; i += 1) lines.push(names.map((name) => csvCell(input.columns[name][i])).join(","));
  return `${lines.join("\n")}\n`;
}

// ------------------------------------------------------------------ comparison (decision 14)

export type CompareRun = { run: DynRun; scenarioLabel: string; stale: Staleness; manifest: Manifest | null; series: SeriesData | null };
export function compareSelection(selected: string[], next: string, draftIds: Record<string, string>): { ok: boolean; selection: string[]; reason?: string } {
  if (selected.includes(next)) return { ok: true, selection: selected.filter((id) => id !== next) };
  if (selected.length >= LIMITS.maxCompareRuns) return { ok: false, selection: selected, reason: `At most ${LIMITS.maxCompareRuns} runs can be compared.` };
  if (selected.length && draftIds[selected[0]] !== draftIds[next]) return { ok: false, selection: selected, reason: "Only runs of one draft can be compared." };
  return { ok: true, selection: [...selected, next] };
}
export type ChannelComparison = { name: string; unit: string; included: string[]; absent: string[]; mismatched: { jobId: string; unit: string }[] };
/** Per channel: which runs can be overlaid (same unit as the baseline's), which lack it, which carry a different unit. */
export function compareChannels(runs: { jobId: string; channels: Record<string, string> }[]): ChannelComparison[] {
  const names = new Set<string>();
  for (const run of runs) for (const name of Object.keys(run.channels)) if (!SKIP_CHANNELS.includes(name)) names.add(name);
  return [...names].sort().map((name) => {
    const reference = runs.find((run) => name in run.channels)!.channels[name];
    return {
      name, unit: reference,
      included: runs.filter((run) => run.channels[name] === reference).map((run) => run.jobId),
      absent: runs.filter((run) => !(name in run.channels)).map((run) => run.jobId),
      mismatched: runs.filter((run) => name in run.channels && run.channels[name] !== reference).map((run) => ({ jobId: run.jobId, unit: run.channels[name] })),
    };
  });
}
/** Aligns runs on exact elapsed t_s. A run without a point at a time gets null there; nothing is interpolated. */
export function alignOnTime(traces: { t: number[]; y: number[] }[]): { x: number[]; ys: (number | null)[][] } {
  const keyed = traces.map((trace) => {
    const seen = new Map<number, number>();
    return trace.t.map((time, i) => { const k = seen.get(time) ?? 0; seen.set(time, k + 1); return { time, k, value: trace.y[i] }; });
  });
  const all = new Map<string, { time: number; k: number }>();
  for (const rows of keyed) for (const row of rows) all.set(`${row.time}#${row.k}`, { time: row.time, k: row.k });
  const keys = [...all.entries()].sort(([, a], [, b]) => a.time - b.time || a.k - b.k);
  const lookups = keyed.map((rows) => new Map(rows.map((row) => [`${row.time}#${row.k}`, row.value])));
  return { x: keys.map(([, key]) => key.time), ys: lookups.map((lookup) => keys.map(([key]) => lookup.get(key) ?? null)) };
}
export type KpiDelta = { id: string; group: string; label: string; unit: string; baseline: number | null; values: { jobId: string; value: number | null; delta: number | null; relative: number | null; reason?: string }[] };
export function kpiDeltas(baselineId: string, runs: { jobId: string; kpis: Kpi[] }[]): KpiDelta[] {
  const baseline = runs.find((run) => run.jobId === baselineId);
  if (!baseline) return [];
  const ids = new Map<string, Kpi>();
  for (const run of runs) for (const kpi of run.kpis) if (!ids.has(kpi.id)) ids.set(kpi.id, kpi);
  return [...ids.values()].map((reference) => {
    const base = baseline.kpis.find((kpi) => kpi.id === reference.id)?.value ?? null;
    return {
      id: reference.id, group: reference.group, label: reference.label, unit: reference.unit, baseline: base,
      values: runs.filter((run) => run.jobId !== baselineId).map((run) => {
        const found = run.kpis.find((kpi) => kpi.id === reference.id);
        const value = found?.value ?? null;
        return { jobId: run.jobId, value, delta: value !== null && base !== null ? value - base : null, relative: value !== null && base !== null && base !== 0 ? (value - base) / Math.abs(base) : null,
          reason: value === null ? (found ? found.reason : "This run has no such quantity.") : undefined };
      }),
    };
  });
}

// ------------------------------------------------------------------ table windows

export function tableWindow(t: number[], range: [number, number] | null, limit = 200): { indices: number[]; total: number } {
  const inRange: number[] = [];
  for (let i = 0; i < t.length; i += 1) if (!range || (t[i] >= range[0] && t[i] <= range[1])) inRange.push(i);
  if (inRange.length <= limit) return { indices: inRange, total: inRange.length };
  const stride = Math.ceil(inRange.length / limit);
  return { indices: inRange.filter((_, i) => i % stride === 0), total: inRange.length };
}
export function panRange(range: [number, number], bounds: [number, number], fraction: number): [number, number] {
  const width = range[1] - range[0];
  let start = range[0] + width * fraction;
  start = Math.max(bounds[0], Math.min(start, bounds[1] - width));
  return [start, start + width];
}
export function zoomRange(range: [number, number], bounds: [number, number], factor: number): [number, number] {
  const mid = (range[0] + range[1]) / 2;
  const half = Math.max(((range[1] - range[0]) * factor) / 2, 1e-6);
  const start = Math.max(bounds[0], mid - half), end = Math.min(bounds[1], mid + half);
  return [start, end];
}
