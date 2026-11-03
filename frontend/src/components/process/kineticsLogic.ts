import type { DraftQuantity, DraftReaction, Finding, KineticsFormId, KineticsRecord, KineticsRegistry, ResultsState } from "../../api/processDraft";
import type { MathNode } from "../../api/bioModels";

// Pure logic for the reactor Kinetics and Results tabs (spec 180 section 8). No React, no I/O.

export type Q = { text: string; unit: string };
export type InhibitionKind = "noncompetitive" | "competitive";
export type ProvenanceKind = "literature" | "measurement" | "operator_estimate" | "synthetic";
export type KineticsForm = {
  id: string; existing: boolean; name: string; kind: KineticsFormId; phase: string;
  rows: { compound: string; coeff: string }[]; base: string; orders: Record<string, string>;
  q: Record<string, Q>; inhibitions: { kind: InhibitionKind; inhibitor: string; k_i: Q }[];
  useTemperature: boolean; provenance: ProvenanceKind; citation: string; note: string;
};
type Stored = DraftQuantity | { value: number; unit: string; si?: number } | number | null | undefined;
type Reaction = DraftReaction & { phase?: string };

export const FORM_LABELS: Record<KineticsFormId, string> = { power_law_arrhenius: "Power law (Arrhenius)", monod: "Monod", haldane: "Haldane / Andrews" };
export const formLabel = (form: string | undefined, registry?: KineticsRegistry) =>
  registry?.forms.find((item) => item.id === form)?.label ?? FORM_LABELS[(form ?? "power_law_arrhenius") as KineticsFormId] ?? "Rate law";
export const PROVENANCE_LABELS: Record<ProvenanceKind, string> = {
  literature: "Literature", measurement: "Measurement", operator_estimate: "Operator estimate", synthetic: "Synthetic (test value)" };
export const INHIBITION_LABELS: Record<InhibitionKind, string> = { noncompetitive: "Non-competitive", competitive: "Competitive" };

/** Display units per quantity kind when the registry does not provide them. */
export const FALLBACK_UNITS: Record<string, string[]> = {
  reaction_rate: ["kmol/[m3.h]", "mol/[m3.s]", "mol/[L.h]"], molar_concentration: ["mol/m3", "mmol/L", "kmol/m3"],
  molar_energy: ["J/mol", "kJ/mol"], temperature: ["degC", "K"],
};
/** Factor or offset converting a display unit to the unit of the registry range (kmol/m3, kmol/[m3.h], J/mol, K). */
const TO_RANGE: Record<string, (value: number) => number> = {
  "kmol/[m3.h]": (v) => v, "mol/[m3.s]": (v) => v * 3.6, "mol/[L.h]": (v) => v,
  "kmol/m3": (v) => v, "mol/m3": (v) => v / 1000, "mmol/L": (v) => v / 1000,
  "J/mol": (v) => v, "kJ/mol": (v) => v * 1000, K: (v) => v, degC: (v) => v + 273.15,
};
export const toRangeUnit = (value: number, unit: string): number => (TO_RANGE[unit] ? TO_RANGE[unit](value) : Number.NaN);

export const humanUnit = (unit: string) => ({ "kmol/[m3.h]": "kmol/(m³·h)", "mol/[m3.s]": "mol/(m³·s)", "mol/[L.h]": "mol/(L·h)", "mol/m3": "mol/m³",
  "kmol/m3": "kmol/m³", degC: "°C" } as Record<string, string>)[unit] ?? unit;

export const formatNumber = (value: number | null | undefined, digits = 4): string => {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (value === 0) return "0";
  const magnitude = Math.abs(value);
  return magnitude >= 1e-3 && magnitude < 1e6 ? String(Number(value.toPrecision(digits))) : Number(value.toPrecision(digits)).toExponential().replace("e+", "e");
};

// ---------------------------------------------------------------- equation (mirror of backend kinetics.equation_tree)
const node = (tag: string, ...children: MathNode[]): MathNode => ({ tag, children });
const leaf = (tag: string, text: string): MathNode => ({ tag, text });
const mi = (text: string) => leaf("mi", text);
const mo = (text: string) => leaf("mo", text);
const mn = (text: string) => leaf("mn", text);
const row = (...children: MathNode[]) => node("mrow", ...children);

export function equationTree(form: string, inhibitions: { kind?: string }[] = [], temperature = false): MathNode {
  if (form === "power_law_arrhenius") {
    const exponent = row(mo("−"), node("mfrac", mi("E"), row(mi("R"), mi("T"))));
    return node("math", row(mi("r"), mo("="), row(mi("A"), mo("·"), node("msup", mi("e"), exponent), mo("·"), mo("∏"), node("msup", mi("C_i"), mi("n_i")))));
  }
  let ks: MathNode = mi("K_S");
  inhibitions.forEach((term, index) => {
    if (term.kind === "competitive") ks = row(ks, row(mo("("), mn("1"), mo("+"), node("mfrac", mi(`I_${index + 1}`), mi(`K_i${index + 1}`)), mo(")")));
  });
  const denominator: MathNode[] = [ks, mo("+"), mi("S")];
  if (form === "haldane") denominator.push(mo("+"), node("mfrac", node("msup", mi("S"), mn("2")), mi("K_I")));
  const body: MathNode[] = [node("mfrac", row(mi("V_max"), mo("·"), mi("S")), row(...denominator))];
  inhibitions.forEach((term, index) => {
    if (term.kind !== "competitive") body.push(mo("·"), node("mfrac", mi(`K_i${index + 1}`), row(mi(`K_i${index + 1}`), mo("+"), mi(`I_${index + 1}`))));
  });
  if (temperature) {
    body.push(mo("·"), node("msup", mi("e"), row(mo("−"), node("mfrac", mi("E_a"), mi("R")), mo("·"),
      row(mo("("), node("mfrac", mn("1"), mi("T")), mo("−"), node("mfrac", mn("1"), mi("T_ref")), mo(")")))));
  }
  return node("math", row(mi("r"), mo("="), ...body));
}

export const reactionEquation = (reaction: Reaction): MathNode => {
  const law = reaction.rate_law;
  return law ? equationTree(law.form, law.inhibitions ?? [], Boolean(law.temperature)) : equationTree("power_law_arrhenius");
};

// ---------------------------------------------------------------- form state
const qty = (stored: Stored, fallbackUnit: string): Q => {
  if (stored && typeof stored === "object") return { text: String(stored.value), unit: stored.unit };
  return { text: typeof stored === "number" ? String(stored) : "", unit: fallbackUnit };
};
const emptyQ = (unit: string): Q => ({ text: "", unit });

export function nextReactionId(reactions: Record<string, unknown>): string {
  let n = 1;
  while (`r${n}` in reactions) n += 1;
  return `r${n}`;
}

export function emptyForm(reactions: Record<string, unknown>, defaultKind: KineticsFormId = "monod"): KineticsForm {
  return {
    id: nextReactionId(reactions), existing: false, name: "", kind: defaultKind, phase: "Mixture", rows: [], base: "", orders: {},
    q: { v_max: emptyQ("kmol/[m3.h]"), k_s: emptyQ("kmol/m3"), k_i: emptyQ("kmol/m3"), A: emptyQ("kmol/[m3.h]"), E: emptyQ("J/mol"),
      activation: emptyQ("J/mol"), reference: emptyQ("K"), tMin: emptyQ("K"), tMax: emptyQ("K"), sMax: emptyQ("kmol/m3") },
    inhibitions: [], useTemperature: false, provenance: "synthetic", citation: "", note: "",
  };
}

export function formFromReaction(id: string, reaction: Reaction, reactions: Record<string, unknown>): KineticsForm {
  const base = emptyForm(reactions);
  const law = reaction.rate_law;
  const validity = reaction.validity ?? {};
  return {
    ...base, id, existing: true, name: reaction.name, kind: (law?.form ?? "power_law_arrhenius") as KineticsFormId, phase: reaction.phase ?? "Mixture",
    rows: Object.entries(reaction.stoichiometry).map(([compound, coeff]) => ({ compound, coeff: String(coeff) })),
    base: reaction.base_reactant ?? "", orders: Object.fromEntries(Object.entries(reaction.orders ?? {}).map(([key, value]) => [key, String(value)])),
    q: {
      v_max: qty(law?.v_max, "kmol/[m3.h]"), k_s: qty(law?.k_s, "kmol/m3"), k_i: qty(law?.k_i, "kmol/m3"),
      A: qty(reaction.A_forward as Stored, "kmol/[m3.h]"), E: qty(reaction.E_forward as Stored, "J/mol"),
      activation: qty(law?.temperature?.activation_energy, "J/mol"), reference: qty(law?.temperature?.reference_temperature, "K"),
      tMin: qty(validity.temperature_min, "K"), tMax: qty(validity.temperature_max, "K"), sMax: qty(validity.substrate_max, "kmol/m3"),
    },
    inhibitions: (law?.inhibitions ?? []).map((term) => ({ kind: term.kind, inhibitor: term.inhibitor, k_i: qty(term.k_i, "kmol/m3") })),
    useTemperature: Boolean(law?.temperature),
    provenance: reaction.provenance?.kind ?? "synthetic", citation: reaction.provenance?.citation ?? "", note: reaction.provenance?.note ?? "",
  };
}

// ---------------------------------------------------------------- validation
export type Errors = Record<string, string>;
const rangeText = (low: number, high: number, unit: string) => `${formatNumber(low, 3)} to ${formatNumber(high, 3)} ${humanUnit(unit)}`;

function rangeError(label: string, entry: Q, param: { minimum: number; maximum: number; unit: string; domain: string } | undefined): string {
  if (!entry.text.trim()) return `${label} is required.`;
  const raw = Number(entry.text);
  if (!Number.isFinite(raw)) return `${label} must be a finite number.`;
  const value = toRangeUnit(raw, entry.unit);
  if (!Number.isFinite(value)) return `${label}: unit ${entry.unit} is not supported.`;
  if (!param) return value < 0 ? `${label} must not be negative.` : "";
  if (value < param.minimum || value > param.maximum || (param.domain.startsWith(">") && value <= 0)) {
    return `${label} must be ${param.domain.startsWith(">") ? "greater than zero, " : ""}within ${rangeText(param.minimum, param.maximum, param.unit)}.`;
  }
  return "";
}

export function validateForm(form: KineticsForm, registry: KineticsRegistry | undefined, compounds: string[]): Errors {
  const errors: Errors = {};
  const rows = form.rows.filter((item) => item.compound);
  if (!form.name.trim()) errors.name = "Name is required.";
  else if (form.name.length > 80) errors.name = "Name must be 80 characters or fewer.";
  if (rows.length < 2) errors.stoichiometry = "Choose at least two participants.";
  else if (new Set(rows.map((item) => item.compound)).size !== rows.length) errors.stoichiometry = "Each compound can appear once.";
  else if (rows.some((item) => !item.coeff.trim() || !Number.isFinite(Number(item.coeff)) || Number(item.coeff) === 0)) errors.stoichiometry = "Coefficients must be finite and non-zero.";
  else if (rows.some((item) => !compounds.includes(item.compound))) errors.stoichiometry = "Every compound must be declared in Thermo.";
  const coefficient = (name: string) => Number(rows.find((item) => item.compound === name)?.coeff);
  if (!form.base) errors.base = "Choose the base reactant.";
  else if (!(coefficient(form.base) < 0)) errors.base = "The base reactant must be a reactant (negative coefficient).";
  const formSpec = registry?.forms.find((item) => item.id === form.kind);
  const param = (key: string) => formSpec?.parameters.find((item) => item.key === key);
  if (form.kind === "power_law_arrhenius") {
    if (!form.q.A.text.trim()) errors.A = "A is required.";
    else if (!Number.isFinite(Number(form.q.A.text)) || Number(form.q.A.text) < 0) errors.A = "A must be finite and not negative.";
    if (!form.q.E.text.trim()) errors.E = "E is required.";
    else if (!Number.isFinite(Number(form.q.E.text)) || Number(form.q.E.text) < 0) errors.E = "E must be finite and not negative.";
    for (const [name, text] of Object.entries(form.orders)) {
      if (text.trim() && (!Number.isFinite(Number(text)) || Number(text) < 0)) errors.orders = `The order of ${name} must be finite and not negative.`;
    }
    return errors;
  }
  errors["rate_law.v_max"] = rangeError("Maximum rate V_max", form.q.v_max, param("v_max"));
  errors["rate_law.k_s"] = rangeError("Half-saturation K_S", form.q.k_s, param("k_s"));
  if (form.kind === "haldane") errors["rate_law.k_i"] = rangeError("Substrate inhibition K_I", form.q.k_i, param("k_i"));
  const maxTerms = registry?.inhibition.max_terms ?? 3;
  if (form.inhibitions.length > maxTerms) errors.inhibitions = `At most ${maxTerms} inhibition terms.`;
  form.inhibitions.forEach((term, index) => {
    const names = form.inhibitions.map((item) => item.inhibitor);
    if (!term.inhibitor || term.inhibitor === form.base || !rows.some((item) => item.compound === term.inhibitor) || names.indexOf(term.inhibitor) !== index) {
      errors[`rate_law.inhibitions.${index}.inhibitor`] = "Choose a distinct participant other than the base reactant.";
    }
    errors[`rate_law.inhibitions.${index}.k_i`] = rangeError("Inhibition K_i", term.k_i, registry?.inhibition.k_i);
  });
  if (form.useTemperature) {
    errors["rate_law.temperature.activation_energy"] = rangeError("Activation energy E_a", form.q.activation, registry?.temperature.activation_energy);
    errors["rate_law.temperature.reference_temperature"] = rangeError("Reference temperature T_ref", form.q.reference, registry?.temperature.reference_temperature);
  }
  if (form.provenance === "literature" && !form.citation.trim()) errors["provenance.citation"] = "A literature source needs a citation.";
  if (form.note.length > 500) errors["provenance.note"] = "Note must be 500 characters or fewer.";
  if (form.citation.length > 300) errors["provenance.citation"] = "Citation must be 300 characters or fewer.";
  const tMin = form.q.tMin.text.trim() ? toRangeUnit(Number(form.q.tMin.text), form.q.tMin.unit) : null;
  const tMax = form.q.tMax.text.trim() ? toRangeUnit(Number(form.q.tMax.text), form.q.tMax.unit) : null;
  if (tMin !== null && !(tMin > 0)) errors.tMin = "Minimum temperature must be above absolute zero.";
  if (tMax !== null && !(tMax > 0)) errors.tMax = "Maximum temperature must be above absolute zero.";
  if (tMin !== null && tMax !== null && !errors.tMin && !errors.tMax && tMax <= tMin) errors.tMax = "Maximum temperature must exceed the minimum.";
  if (form.q.sMax.text.trim() && !(toRangeUnit(Number(form.q.sMax.text), form.q.sMax.unit) > 0)) errors.sMax = "Maximum substrate concentration must be greater than zero.";
  return Object.fromEntries(Object.entries(errors).filter(([, message]) => message));
}

// ---------------------------------------------------------------- building the DraftOp reaction
const send = (entry: Q): DraftQuantity => ({ value: Number(entry.text), unit: entry.unit });
const numberOrNull = (entry: Q) => (entry.text.trim() ? send(entry) : null);

export function buildReaction(form: KineticsForm): Record<string, unknown> {
  const rows = form.rows.filter((item) => item.compound);
  const stoichiometry = Object.fromEntries(rows.map((item) => [item.compound, Number(item.coeff)]));
  if (form.kind === "power_law_arrhenius") {
    const orders = Object.fromEntries(Object.entries(form.orders).filter(([name, text]) => text.trim() && name in stoichiometry).map(([name, text]) => [name, Number(text)]));
    return { name: form.name.trim(), stoichiometry, orders, base_reactant: form.base, phase: form.existing ? form.phase : "Mixture", basis: "MolarConc",
      A_forward: send(form.q.A), E_forward: send(form.q.E) };
  }
  const law: Record<string, unknown> = { form: form.kind, substrate: form.base, v_max: send(form.q.v_max), k_s: send(form.q.k_s),
    ...(form.kind === "haldane" ? { k_i: send(form.q.k_i) } : {}),
    inhibitions: form.inhibitions.map((term) => ({ kind: term.kind, inhibitor: term.inhibitor, k_i: send(term.k_i) })),
    ...(form.useTemperature ? { temperature: { activation_energy: send(form.q.activation), reference_temperature: send(form.q.reference) } } : {}) };
  const validity: Record<string, DraftQuantity> = {};
  for (const [key, entry] of [["temperature_min", form.q.tMin], ["temperature_max", form.q.tMax], ["substrate_max", form.q.sMax]] as const) {
    const value = numberOrNull(entry);
    if (value) validity[key] = value;
  }
  return { name: form.name.trim(), stoichiometry, base_reactant: form.base, phase: "Liquid", basis: "MolarConc", rate_law: law,
    provenance: { kind: form.provenance, ...(form.citation.trim() ? { citation: form.citation.trim() } : {}), ...(form.note.trim() ? { note: form.note.trim() } : {}) },
    ...(Object.keys(validity).length ? { validity } : {}) };
}

const stripQuantity = (value: unknown): unknown => (value && typeof value === "object" && "value" in value && "unit" in value
  ? { value: (value as DraftQuantity).value, unit: (value as DraftQuantity).unit } : value);

/** An already stored reaction as a DraftOp body (stored quantities lose their SI mirror). */
export function reactionBody(reaction: Reaction): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const key of ["name", "stoichiometry", "orders", "base_reactant", "phase", "basis", "A_forward", "E_forward", "rate_law", "provenance", "validity"]) {
    if (reaction[key] === undefined || reaction[key] === null) continue;
    body[key] = JSON.parse(JSON.stringify(reaction[key], (name, value) => (name === "si" ? undefined : value))) as unknown;
  }
  if (!body.orders || !Object.keys(body.orders as object).length) delete body.orders;
  for (const key of ["A_forward", "E_forward"]) if (key in body) body[key] = stripQuantity(body[key]);
  return body;
}

export const stoichiometryText = (reaction: Reaction): { reactants: string; products: string } => {
  const side = (sign: number) => Object.entries(reaction.stoichiometry).filter(([, coeff]) => Math.sign(coeff) === sign)
    .map(([name, coeff]) => `${Math.abs(coeff) === 1 ? "" : `${Math.abs(coeff)} `}${name}`).join(" + ");
  return { reactants: side(-1), products: side(1) };
};

// ---------------------------------------------------------------- findings
export const KINETICS_FINDING = /^(KINETICS_|REACTION_SET_|REACTOR_ENERGY_)/;
const REASONS: Record<string, string> = {
  KINETICS_VERIFICATION_FAILED: "Jarvis could not confirm DWSIM's reactor result against the rate law, so no numbers are shown as results.",
  KINETICS_TWO_PHASE_UNSUPPORTED: "A vapour phase appeared in the reactor streams. Typed rate laws support liquid-phase reactors only.",
  KINETICS_OUTSIDE_VALIDITY: "The run was outside the validity range you declared for this reaction.",
  KINETICS_PARAMETER_UNVERIFIED: "The parameters are an estimate or a synthetic test value, not measured or cited.",
};
export const kineticsFindingText = (finding: { code: string; message: string }) => finding.message || REASONS[finding.code] || finding.code.replace(/^KINETICS_/, "").replace(/_/g, " ").toLowerCase();

export function reactionFindings(findings: Finding[], tag: string, reactionId: string): Finding[] {
  return findings.filter((item) => item.object === tag && (item.field === `reactions.${reactionId}` || item.field?.startsWith(`reactions.${reactionId}.`)));
}
/** Findings for the reactor itself: kinetics codes that do not point at one reaction. */
export function reactorFindings(findings: Finding[], tag: string): Finding[] {
  return findings.filter((item) => item.object === tag && KINETICS_FINDING.test(item.code) && !item.field?.startsWith("reactions."));
}
/** Server findings of one reaction keyed by the path after `reactions.<id>.`, for inline display next to the matching input. */
export function serverFieldErrors(findings: Finding[], tag: string, reactionId: string): Errors {
  const out: Errors = {};
  for (const item of reactionFindings(findings, tag, reactionId)) {
    if (item.severity === "info") continue;
    const path = (item.field ?? "").slice(`reactions.${reactionId}.`.length);
    const key = path === "rate_law.substrate" ? "base" : path;
    if (key && !out[key]) out[key] = item.message;
  }
  return out;
}

// ---------------------------------------------------------------- run state
export type CompileState = { tone: "ok" | "failed" | "stale" | "pending"; text: string };
export function compilationState(record: KineticsRecord | undefined, results: ResultsState, compilable: boolean | undefined): CompileState {
  if (record && !record.verified) return { tone: "failed", text: `Compiled and run, but Jarvis verification failed (${record.code ?? "KINETICS_VERIFICATION_FAILED"}). ${REASONS[record.code ?? "KINETICS_VERIFICATION_FAILED"] ?? ""}`.trim() };
  if (record?.verified && results.state === "current") return { tone: "ok", text: "Compiled into the DWSIM flowsheet on the last Run and verified by Jarvis." };
  if (record?.verified) return { tone: "stale", text: "Verified on a previous Run. The draft changed since, so run again to re-verify." };
  if (compilable === false) return { tone: "failed", text: "The rate law cannot be compiled yet. Resolve the findings above." };
  return { tone: "pending", text: "Compiles into the DWSIM flowsheet when you Run; Jarvis then verifies the result." };
}

export const FAILURE_WORDS: Record<string, string> = REASONS;
