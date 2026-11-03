// Pure helpers for the Photobioreactor (T1) inspector (spec 170). No React and no I/O so the
// contract test can execute them directly.

export type PbrQuantity = { value: number; unit: string; si?: number };
export type PbrParams = Record<string, PbrQuantity | undefined>;

// ------------------------------------------------------------------ number formatting
const SUPERSCRIPT: Record<string, string> = { "-": "⁻", "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹" };

/** Three to four significant digits; scientific notation outside [1e-3, 1e6). */
export function formatSig(value: unknown, digits = 4): string {
  if (typeof value === "string") return value;
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (value === 0) return "0";
  const magnitude = Math.abs(value);
  if (magnitude >= 1e-3 && magnitude < 1e6) return String(Number(value.toPrecision(digits)));
  const [mantissa, exponent] = value.toExponential(digits - 1).split("e");
  const power = String(Number(exponent)).split("").map((char) => SUPERSCRIPT[char] ?? char).join("");
  return `${Number(mantissa)}×10${power}`;
}

const UNIT_WORDS: Record<string, string> = {
  "1/h": "h⁻¹", "1/s": "s⁻¹", "1/d": "d⁻¹", "m3": "m³", "m2": "m²", "m3/h": "m³ h⁻¹", "kg/m3": "kg m⁻³", "kg/d": "kg d⁻¹",
  "kg/m3/d": "kg m⁻³ d⁻¹", "kg/(m3.d)": "kg m⁻³ d⁻¹", "umol/(m2.s)": "µmol m⁻² s⁻¹", "degC": "°C", "percent": "%", "dimensionless": "",
};
export const prettyUnits = (units?: string): string => (units === undefined ? "" : UNIT_WORDS[units] ?? units.replace(/(\w)3\b/g, "$1³").replace(/(\w)2\b/g, "$1²"));

export type ReportedRow = { value: number | string; units?: string; label?: string };

/** Value and units as the operator reads them; pressure drop is shown in kPa. */
export function formatReported(key: string, row: ReportedRow | undefined): string {
  if (!row) return "—";
  if (typeof row.value !== "number") return String(row.value);
  if (key === "pressure_drop" && (row.units ?? "Pa") === "Pa") return `${formatSig(row.value / 1000)} kPa`;
  const unit = prettyUnits(row.units);
  return unit ? `${formatSig(row.value)} ${unit}` : formatSig(row.value);
}

// ------------------------------------------------------------------ derived geometry
const finite = (value: number | undefined): value is number => typeof value === "number" && Number.isFinite(value);

/** V = n·π/4·D²·L in m³, from the stored SI values; null while any input is missing. */
export function liquidVolumeM3(params: PbrParams | undefined): number | null {
  const n = params?.tube_count?.si ?? params?.tube_count?.value;
  const d = params?.tube_inner_diameter?.si;
  const l = params?.tube_length?.si;
  if (!finite(n) || !finite(d) || !finite(l) || n < 1 || d <= 0 || l <= 0) return null;
  return n * (Math.PI / 4) * d * d * l;
}

// ------------------------------------------------------------------ domain validation
const TO_SI: Record<string, Record<string, (value: number) => number>> = {
  length: { m: (v) => v, mm: (v) => v / 1000 },
  inverse_length: { "1/m": (v) => v, "1/cm": (v) => v * 100 },
  temperature: { K: (v) => v, degC: (v) => v + 273.15 },
  temperature_difference: { K: (v) => v, degC: (v) => v },
  time: { s: (v) => v, h: (v) => v * 3600, d: (v) => v * 86400 },
  mass_concentration: { "kg/m3": (v) => v, "g/L": (v) => v, "mg/L": (v) => v / 1000 },
  velocity: { "m/s": (v) => v },
  specific_rate: { "1/s": (v) => v, "1/h": (v) => v / 3600, "1/d": (v) => v / 86400 },
  photon_flux_density: { "umol/(m2.s)": (v) => v },
  percent: { percent: (v) => v },
  dimensionless: { dimensionless: (v) => v },
};

export function toSi(kind: string, unit: string, value: number): number | null {
  const convert = TO_SI[kind]?.[unit];
  return convert ? convert(value) : null;
}

export type PbrEntry = { text: string; unit: string };
export type PbrFormValues = Record<string, PbrEntry | undefined>;
export const OPTICS_V2_KEYS: string[] = ["tube_outer_diameter", "wall_absorption"];
export const T2_OPTICS_KEYS = new Set(["tubes_across", "layers", "pitch_h", "pitch_v", "stagger_offset", "axis_azimuth", "centre_height_above_sea", "n_wall", "n_water", "sea_albedo", "tube_outer_diameter", "wall_absorption"]);

/** Parse an operator entry into SI; null when empty, not a finite number or in an unknown unit. */
export function entrySi(kind: string, entry: PbrEntry | undefined): number | null {
  if (!entry || !entry.text.trim()) return null;
  const value = Number(entry.text);
  return Number.isFinite(value) ? toSi(kind, entry.unit, value) : null;
}

type Rule = { kind: string; ok(si: number): boolean; message: string };
export const PBR_RULES: Record<string, Rule> = {
  tube_inner_diameter: { kind: "length", ok: (v) => v >= 0.005 && v <= 0.5, message: "Tube inner diameter must be between 5 mm and 0.5 m" },
  tube_length: { kind: "length", ok: (v) => v > 0, message: "Tube length must be greater than zero" },
  tube_count: { kind: "dimensionless", ok: (v) => Number.isInteger(v) && v >= 1, message: "Number of tubes must be a whole number of at least 1" },
  liquid_velocity: { kind: "velocity", ok: (v) => v > 0, message: "Liquid velocity must be greater than zero" },
  pump_efficiency: { kind: "percent", ok: (v) => v > 0 && v <= 100, message: "Pump efficiency must be above 0 % and at most 100 %" },
  baffle_friction_multiplier: { kind: "dimensionless", ok: (v) => v >= 1, message: "Baffle friction multiplier must be at least 1" },
  oxygen_kla: { kind: "specific_rate", ok: (v) => v >= 0, message: "Oxygen mass-transfer coefficient kLa must be zero or greater" },
  oxygen_saturation: { kind: "mass_concentration", ok: (v) => v > 0, message: "Dissolved oxygen saturation must be greater than zero" },
  peak_par: { kind: "photon_flux_density", ok: (v) => v >= 0, message: "Peak PAR must be zero or greater" },
  photoperiod: { kind: "time", ok: (v) => v > 0 && v <= 86400, message: "Photoperiod must be more than 0 and at most 24 h" },
  diffuse_fraction: { kind: "dimensionless", ok: (v) => v >= 0 && v <= 1, message: "Diffuse fraction must be between 0 and 1" },
  temperature_mean: { kind: "temperature", ok: (v) => v > 0, message: "Mean temperature must be above 0 K" },
  temperature_amplitude: { kind: "temperature_difference", ok: (v) => v >= 0, message: "Temperature amplitude must be zero or greater" },
  tubes_across: { kind: "dimensionless", ok: (v) => Number.isInteger(v) && v >= 1 && v <= 64, message: "Tubes across must be a whole number from 1 to 64" },
  layers: { kind: "dimensionless", ok: (v) => Number.isInteger(v) && v >= 1 && v <= 64, message: "Layers must be a whole number from 1 to 64" },
  pitch_h: { kind: "length", ok: (v) => v > 0, message: "Horizontal pitch must be positive" },
  pitch_v: { kind: "length", ok: (v) => v > 0, message: "Vertical pitch must be positive" },
  stagger_offset: { kind: "length", ok: (v) => Number.isFinite(v), message: "Stagger must be finite" },
  axis_azimuth: { kind: "dimensionless", ok: (v) => v >= 0 && v <= 360, message: "Axis azimuth must be 0–360°" },
  centre_height_above_sea: { kind: "length", ok: (v) => v >= 0, message: "Tube centre height cannot be negative" },
  n_wall: { kind: "dimensionless", ok: (v) => v >= 1 && v <= 5, message: "Wall index must be 1–5" },
  n_water: { kind: "dimensionless", ok: (v) => v >= 1 && v <= 5, message: "Water index must be 1–5" },
  sea_albedo: { kind: "dimensionless", ok: (v) => v >= 0 && v <= 1, message: "Sea albedo must be 0–1" },
  tube_outer_diameter: { kind: "length", ok: (v) => v > 0, message: "Tube outer diameter must be positive" },
  wall_absorption: { kind: "inverse_length", ok: (v) => v >= 0, message: "Wall absorption must be zero or greater" },
};
const TEMPERATURE_FLOOR = "Mean temperature minus the amplitude must stay above 0 K";

/**
 * Inline domain message for one parameter, or "" when acceptable or not yet checkable. `current` resolves
 * the value shown for any other parameter (edit in progress, else stored) for the T_mean − ΔT coupling.
 */
export function pbrFieldError(key: string, entry: PbrEntry | undefined, current: (other: string) => number | null): string {
  const rule = PBR_RULES[key];
  if (!rule || !entry || !entry.text.trim()) return "";
  const value = Number(entry.text);
  if (!Number.isFinite(value)) return "Enter a number";
  const si = toSi(rule.kind, entry.unit, value);
  if (si === null) return "";
  if (!rule.ok(si)) return rule.message;
  const diameter = current("tube_inner_diameter");
  // With optics v2 the outer diameter is the tube silhouette for spacing checks.
  const outer = key === "tube_outer_diameter" ? null : current("tube_outer_diameter");
  const silhouette = outer !== null && (diameter === null || outer > diameter) ? outer : diameter;
  if ((key === "pitch_h" || key === "pitch_v") && silhouette !== null && si < silhouette) return "Pitch must be at least the tube diameter";
  if (key === "tube_outer_diameter" && diameter !== null && si <= diameter) return "Outer diameter must exceed the inner diameter";
  if (key === "centre_height_above_sea" && silhouette !== null && si < silhouette / 2) return "Lowest tube centre must be above the sea by at least one radius";
  if (key === "stagger_offset" && current("pitch_h") !== null && Math.abs(si) > current("pitch_h")!) return "Stagger must not exceed horizontal pitch";
  if (key === "tubes_across" || key === "layers") {
    const other = current(key === "tubes_across" ? "layers" : "tubes_across");
    if (other !== null && si * other > 64) return "The representative module supports at most 64 tubes";
    const count = current("tube_count");
    if (other !== null && count !== null && si * other > count) return "Module tube count must not exceed the unit tube count";
  }
  if (key === "temperature_mean" || key === "temperature_amplitude") {
    const mean = key === "temperature_mean" ? si : current("temperature_mean");
    const amplitude = key === "temperature_amplitude" ? si : current("temperature_amplitude");
    if (mean !== null && amplitude !== null && mean - amplitude <= 0) return TEMPERATURE_FLOOR;
  }
  return "";
}

// ------------------------------------------------------------------ model cards
export type CardLike = {
  id: string; name: string; revision: string; digest: string; parameter_set_id: string; n_source?: string;
  factors: Record<string, unknown>; parameter_set_revision?: string; parameter_set_digest?: string;
  history?: Array<Record<string, unknown>>;
};
export type SetLike = { id: string; name: string; revision: string; digest: string; history?: Array<Record<string, unknown>>; values: Record<string, unknown> };
export type Pin = { card_id: string; card_revision: string; card_digest: string };

export const PBR_SET_SYMBOLS = ["k_X", "a", "b", "c", "d", "w_ash"] as const;

const asList = (value: unknown): string[] => (Array.isArray(value) ? value.map(String) : value ? [String(value)] : []);

/** Why the PBR cannot use this card, or null when its factor forms are supported. */
export function pbrCardRefusal(card: CardLike, formLabel: (id: string) => string = (id) => id): string | null {
  const light = asList(card.factors.light);
  if (light.length !== 1 || light[0] !== "light.monod") return light.length
    ? `Uses ${formLabel(light.find((id) => id !== "light.monod") ?? light[0])}; the photobioreactor supports only Monod light response`
    : "Has no light response factor; the photobioreactor needs Monod light response";
  const temperature = asList(card.factors.temperature);
  if (temperature.length !== 1 || !["temperature.isothermal", "temperature.ctmi", "temperature.arrhenius_ref"].includes(temperature[0]))
    return "Has an unsupported temperature factor";
  const nutrients = asList(card.factors.nutrients);
  if (nutrients.includes("nutrient.droop")) return "Uses Droop quota limitation, which the photobioreactor does not support";
  if (nutrients.length !== 1 || nutrients[0] !== "nutrient.monod") return "Needs exactly one Monod nutrient factor, bound to dissolved nitrogen";
  const combination = asList(card.factors.combination);
  if (combination.length !== 1 || !["combine.multiplicative", "combine.liebig"].includes(combination[0]))
    return "Has an unsupported nutrient combination";
  const loss = asList(card.factors.loss);
  if (loss.length !== 1 || !["loss.first_order", "loss.light_dark"].includes(loss[0])) return "Has an unsupported biomass loss factor";
  if (asList(card.factors.stoichiometry).join() !== "stoich.photoautotrophic") return "Needs the photoautotrophic stoichiometry factor for elemental balance";
  return null;
}

/** Set symbols the PBR reads that the card's set has no value for (non-blocking hint here; Validate reports it). */
export const missingSetSymbols = (set: SetLike | undefined): string[] =>
  set ? PBR_SET_SYMBOLS.filter((symbol) => !(symbol in set.values)) : [];

/** The library lists only each set's head; never attribute its values to an older card pin. */
export const setMatchesCard = (card: CardLike, set: SetLike | undefined): boolean => Boolean(set
  && set.id === card.parameter_set_id
  && set.revision === card.parameter_set_revision
  && set.digest === card.parameter_set_digest);

export const shortRevision = (revision?: string): string => (revision ? revision.replace(/^r-/, "").slice(0, 8) : "unknown");

const historyDate = (history: Array<Record<string, unknown>> | undefined, revision: string | undefined): string | null => {
  const entry = history?.find((item) => item.revision === revision);
  return typeof entry?.created_at === "string" ? entry.created_at : null;
};

/** "3 Oct 2026" from an ISO timestamp, in UTC so the label is stable. */
export function humanDate(iso: string | null | undefined): string {
  if (!iso) return "date unknown";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "date unknown";
  return `${date.getUTCDate()} ${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][date.getUTCMonth()]} ${date.getUTCFullYear()}`;
}

export const revisionLabel = (history: Array<Record<string, unknown>> | undefined, revision: string | undefined): string =>
  `revision ${shortRevision(revision)} · ${humanDate(historyDate(history, revision))}`;

export type PinStatus = {
  /** The pinned card no longer resolves in the library. */
  missing: boolean;
  /** The card's head revision or digest differs from the pin (re-pin possible). */
  cardNewer: boolean;
  /** The card's parameter set moved past the revision the card was built on. */
  setNewer: boolean;
  changes: string[];
};

export function pinStatus(pin: Pin | null | undefined, card: CardLike | undefined, set: SetLike | undefined): PinStatus {
  if (!pin) return { missing: false, cardNewer: false, setNewer: false, changes: [] };
  if (!card) return { missing: true, cardNewer: false, setNewer: false, changes: [] };
  const cardNewer = card.revision !== pin.card_revision || card.digest !== pin.card_digest;
  const setNewer = Boolean(set && card.parameter_set_revision && (set.revision !== card.parameter_set_revision
    || (card.parameter_set_digest && set.digest !== card.parameter_set_digest)));
  const changes: string[] = [];
  if (cardNewer) changes.push(`Card ${card.name}: pinned ${shortRevision(pin.card_revision)} → now ${revisionLabel(card.history, card.revision)}`);
  if (setNewer && set) changes.push(`Parameter set ${set.name}: built on ${shortRevision(card.parameter_set_revision)} → now ${revisionLabel(set.history, set.revision)}`);
  return { missing: false, cardNewer, setNewer, changes };
}

/**
 * An existing card that already carries `card`'s model on the latest revision of `set`; adopting a
 * newer set revision pins it instead of creating a duplicate. mu_max is compared by value.
 */
export function cardOnLatestSet<C extends CardLike & { mu_max?: { value: number; unit: string } }>(
  cards: C[], card: C, set: SetLike,
): C | undefined {
  const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
  return cards.find((other) => other.id !== card.id && other.parameter_set_id === set.id
    && other.parameter_set_revision === set.revision && other.parameter_set_digest === set.digest
    && other.n_source === card.n_source && same(other.factors, card.factors) && same(other.mu_max, card.mu_max));
}

/** Verification chip text; the backend's underscored state names read as words. */
export function verificationChip(state: string | undefined): { text: string; tone: "good" | "warn" | "plain" } {
  switch (state) {
    case "expert_reviewed": return { text: "expert reviewed", tone: "good" };
    case "source_verified": return { text: "source verified", tone: "good" };
    case "source_changed_since_verification":
    case "source_changed": return { text: "source changed", tone: "warn" };
    case "candidate": return { text: "candidate", tone: "warn" };
    default: return { text: state ? state.replace(/_/g, " ") : "candidate", tone: "plain" };
  }
}

export const nSourceLabel = (source?: string): string =>
  source === "HNO3" ? "N source assumed: NO₃⁻ (HNO₃) — inlet N speciation unverified"
    : source === "NH3" ? "N source assumed: NH₃ — inlet N speciation unverified" : "N source not recorded";

/** The spec 170 section 2 provenance line shown in Results next to the card pin. */
export const nSourceNote = (source?: string): string =>
  source === "NH3" || source === "HNO3"
    ? `N source assumed: ${source}; inlet N speciation unverified` : "N source assumed: not recorded; inlet N speciation unverified";

/** Card and parameter-set pin in words: names and short revisions, never digests (ids belong in tooltips). */
export function pinDescription(pin: { card_name?: string; card_revision?: string; set_name?: string; set_revision?: string } | undefined): string {
  if (!pin) return "";
  const card = `${pin.card_name ?? "Model card"}, revision ${shortRevision(pin.card_revision)}`;
  return pin.set_revision || pin.set_name ? `${card} · parameter set ${pin.set_name ?? "unnamed"}, revision ${shortRevision(pin.set_revision)}` : card;
}

/**
 * Whether a failed mixed solve belongs to this unit. 168 records a Jarvis-unit failure under `failed_segment`
 * (the unit tag) with an empty `failed_units`, while DWSIM segment failures list tags in `failed_units`.
 */
export function failureTouchesUnit(solve: { failed_segment?: string; failed_units?: string[] } | undefined, tag: string): boolean {
  return Boolean(solve && (solve.failed_segment === tag || solve.failed_units?.includes(tag)));
}

// ------------------------------------------------------------------ findings and failures
const FAILURE_HEADINGS: Record<string, string> = {
  PBR_NONPHYSICAL_STATE: "Non-physical state",
  PBR_NO_FINITE_PRODUCTIVE_STATE: "No finite productive state",
  PBR_BRANCH_UNRESOLVED: "Branch could not be resolved",
  PBR_OPTICS_OUT_OF_RANGE: "Optical depth out of range",
  PBR_PERIODIC_STEADY_FAILED: "Periodic steady state not reached",
  PBR_NO_THROUGHFLOW: "No through-flow",
  PBR_INLET_DENSITY_UNAVAILABLE: "Inlet density unavailable",
  JARVIS_UNIT_TIMEOUT: "Unit timed out",
};

/** Heading derived from a typed failure code; unknown codes are humanised, never hidden. */
export function pbrFailureHeading(code: string | null | undefined): string {
  if (!code) return "Unit failed";
  if (FAILURE_HEADINGS[code]) return FAILURE_HEADINGS[code];
  const words = code.replace(/^(PBR|JARVIS)_/, "").toLowerCase().replace(/_/g, " ");
  return words.replace(/^./, (letter) => letter.toUpperCase());
}

export const PBR_PICKER_FINDING = /^(PBR_REQUIRES_MODEL_CARD|PBR_MODEL_CARD_|PBR_PARAMETER_UNVERIFIED)/;

export function severityWord(severity: string): { text: string; tone: "danger" | "warning" | "info" } {
  if (severity === "blocker") return { text: "Blocks Run", tone: "danger" };
  if (severity === "error") return { text: "Error", tone: "danger" };
  if (severity === "warning") return { text: "Warning", tone: "warning" };
  return { text: "Note", tone: "info" };
}

// ------------------------------------------------------------------ result wording
export function branchExplanation(branch: string | undefined): string {
  if (branch === "washout") return "Washout: dilution outpaces growth, so no steady culture is sustained in the reactor and only biomass arriving in the feed leaves.";
  if (branch === "productive") return "Productive: growth balances dilution at a positive steady culture, so the reactor holds a living culture and delivers biomass.";
  return "Branch not reported.";
}

export function growthVersusDilution(lambdaH: unknown, dilutionH: unknown): string {
  if (typeof lambdaH !== "number" || typeof dilutionH !== "number") return "Growth rate and dilution rate were not reported.";
  const text = `Thin-culture growth Λ = ${formatSig(lambdaH)} h⁻¹ and dilution D = ${formatSig(dilutionH)} h⁻¹`;
  if (lambdaH > dilutionH) return `${text}: Λ is greater than D, so growth can outrun washout.`;
  if (lambdaH < dilutionH) return `${text}: Λ is smaller than D, so the culture is washed out faster than it can grow.`;
  return `${text}: Λ equals D, which is the washout threshold.`;
}

export function gasTransferWords(kgPerDay: unknown): string {
  if (typeof kgPerDay !== "number") return "";
  if (kgPerDay > 0) return `Degassing: the unit releases ${formatSig(kgPerDay)} kg/d of O₂ to the gas phase.`;
  if (kgPerDay < 0) return `Absorption: the unit takes up ${formatSig(-kgPerDay)} kg/d of O₂ from the gas phase.`;
  return "No net O₂ transfer to or from the gas phase.";
}

export const BALANCE_FIELDS: Record<string, string> = {
  biomass: "Biomass", nitrogen: "Dissolved nitrogen", phosphorus: "Dissolved phosphorus", oxygen: "Dissolved oxygen", dic: "Dissolved inorganic carbon", salinity: "Salt",
};
