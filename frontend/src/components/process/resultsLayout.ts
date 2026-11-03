import type { DraftObject, Kpi, RunOutcome } from "../../api/processDraft";

/**
 * The draft's own streams, units and KPI rows, so every results table keeps its rows (with `empty` cells)
 * before a run, after a failed run, and for objects added since the last run.
 */
export type ResultsLayout = {
  streams: { tag: string; role: "feed" | "product" | "internal"; from: string | null; to: string | null }[];
  units: { tag: string; inlets: string[]; outlets: string[] }[];
  kpis: Kpi[];
};

const byTag = (a: { tag: string }, b: { tag: string }) => (a.tag < b.tag ? -1 : a.tag > b.tag ? 1 : 0);

/**
 * KPI rows of an unsolved draft. Mirrors the row order, labels and definitions of backend
 * `results_view.kpis` (spec 181); tests/181-results-empty-states.mjs keeps the two in step.
 */
function kpiSkeleton(objects: DraftObject[], products: DraftObject[]): Kpi[] {
  const row = (id: string, label: string, unit: string, definition: string): Kpi =>
    ({ id, label, value: null, unit, status: "unavailable", reason: "no run yet", definition, sources: [] });
  const units = (type: string) => objects.filter((item) => item.kind === "unit" && item.type === type).sort(byTag);
  const rows = [row("biomass_product_rate", "Biomass product rate", "kg/d",
    "Σ over product boundary streams of culture biomass (kg/m³) × volumetric flow (m³/d), 167 culture basis")];
  for (const { tag } of units("PhotobioreactorT1")) {
    rows.push(
      row(`pbr_volumetric_productivity:${tag}`, `${tag} volumetric productivity`, "kg/(m³·d)", "PBR reported net volumetric biomass productivity"),
      row(`pbr_net_biomass_production:${tag}`, `${tag} net biomass production`, "kg/d", "PBR reported net biomass production rate"),
      row(`pbr_projected_area_productivity:${tag}`, `${tag} productivity per projected tube area (not ground footprint)`, "kg/(m²·d)",
        "net biomass production ÷ A_proj, A_proj = n_tubes · D_inner · L: the projected tube area normal to a "
        + "beam perpendicular to the tube axis; not ground footprint"),
      row(`culture_throughput:${tag}`, `${tag} culture throughput`, "m³/h", "PBR reported carrier volumetric flow Q"),
    );
  }
  for (const { tag } of products)
    rows.push(row(`outlet_biomass_concentration:${tag}`, `${tag} biomass concentration`, "kg/m³", "culture biomass concentration of the product stream"));
  for (const { tag } of units("SpecifiedSeparator"))
    rows.push(row(`separator_biomass_recovery:${tag}`, `${tag} biomass recovery`, "1",
      "biomass mass rate in the concentrate outlet ÷ biomass mass rate at the inlet, from solved stream culture"));
  rows.push(
    row("water_input", "Water input", "kg/h", "Σ over feed streams of mass flow × Water mass fraction"),
    row("nitrogen_input", "Nitrogen input", "kg/d", "Σ over feed streams of culture nitrogen (kg/m³) × volumetric flow (m³/d)"),
    row("mass_balance_closure", "Mass balance closure", "1", "|residual| ÷ boundary mass flow in"),
    row("co2_feed", "CO₂ feed", "kg/d", "not modelled in T1"),
    row("co2_uptake", "CO₂ uptake", "kg/d", "not modelled in T1"),
    row("co2_per_biomass", "CO₂ per biomass", "kg/kg", "not modelled in T1"),
  );
  return rows;
}

export function resultsLayout(objects: DraftObject[]): ResultsLayout {
  const tagOf = new Map(objects.map((item) => [item.id, item.tag]));
  const material = objects.filter((item) => item.kind === "stream" && item.type !== "EnergyStream").sort(byTag);
  const streams = material.map((item) => ({
    tag: item.tag,
    role: !item.source ? "feed" as const : !item.target ? "product" as const : "internal" as const,
    from: item.source ? tagOf.get(item.source.unit) ?? null : null,
    to: item.target ? tagOf.get(item.target.unit) ?? null : null,
  }));
  const units = objects.filter((item) => item.kind === "unit").sort(byTag).map((unit) => ({
    tag: unit.tag,
    inlets: material.filter((item) => item.target?.unit === unit.id).sort((a, b) => a.target!.port - b.target!.port).map((item) => item.tag),
    outlets: material.filter((item) => item.source?.unit === unit.id).sort((a, b) => a.source!.port - b.source!.port).map((item) => item.tag),
  }));
  return { streams, units, kpis: kpiSkeleton(objects, material.filter((item) => item.source && !item.target)) };
}

/** A stream named by its endpoints ("REC-401 → STATE-401") when both are known; otherwise its tag. */
export function streamLabel(layout: ResultsLayout | null | undefined, tag: string): string {
  const stream = layout?.streams.find((item) => item.tag === tag);
  return stream?.from && stream.to ? `${stream.from} → ${stream.to}` : tag;
}

/** A tear field key in words: `mass_flow_kg_s` → "mass flow", `mass_fraction.Water` → "Water mass fraction". */
export function fieldLabel(field: string | null | undefined): string {
  if (!field) return "";
  if (field.startsWith("mass_fraction.")) return `${field.slice("mass_fraction.".length)} mass fraction`;
  return field.replace(/_(kg_s|kg_h|mol_s|m3_s|m3_h|kg_m3|K|Pa|kPa|bar)$/, "").replace(/_/g, " ");
}

/** First sentence of an engine message, without an exception type prefix, capped to one line. */
export function firstSentence(text: string | null | undefined, max = 160): string {
  const line = (String(text ?? "").trim().split(/\r?\n/)[0] ?? "").replace(/^(?:[\w.]+(?:Exception|Error):\s*)+/, "");
  const sentence = line.split(/(?<=[.!?])\s+/)[0] ?? line;
  return sentence.length > max ? `${sentence.slice(0, max - 1).trimEnd()}…` : sentence;
}

/**
 * The run outcome in one line, from its structured fields: the state, the first failing object (a stream by its
 * endpoints), and the first sentence of its error. Without a failing object, the reason code in words.
 */
export function outcomeHeadline(outcome: RunOutcome, streamName: (tag: string) => string, reasonText: (code: string) => string): string {
  if (outcome.state !== "failed" && outcome.state !== "non_converged") return outcome.label;
  const first = outcome.failing.find((item) => item.tag || item.error);
  if (!first) return `${outcome.label}${outcome.reason ? `: ${reasonText(outcome.reason)}` : ""}`;
  const more = outcome.failing.filter((item) => item.tag).length - 1;
  let cause = firstSentence(first.error ?? outcome.message);
  // Engines prefix the message with the object they failed on (often a stream tag such as REC_TO_STATE): name it
  // readably next to the failing unit instead of repeating the raw tag.
  const prefix = /^([A-Za-z][\w-]*): /.exec(cause);
  const stream = prefix && prefix[1] !== first.tag ? streamName(prefix[1]) : null;
  if (prefix) cause = cause.slice(prefix[0].length);
  const at = first.tag ? `${streamName(first.tag)}${stream ? `, stream ${stream}` : ""}` : stream ? `stream ${stream}` : "";
  const where = at ? ` at ${at}${more > 0 ? ` (+${more} more)` : ""}` : "";
  return `${outcome.label}${where}${cause ? `: ${cause}` : ""}`;
}
