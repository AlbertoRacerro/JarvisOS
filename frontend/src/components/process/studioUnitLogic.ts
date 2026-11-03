import type { DraftObject, DraftOp, RegistryUnit, StudioPaletteUnit, StudioPin, StudioPinStatus, StudioRunProvenance } from "../../api/processDraft";

// Spec 190 process-side helpers for custom Studio units. Pure so the build-gating test can import them.
export const STUDIO_UNIT = "StudioUnit";
export const STUDIO_LIMITS = "Steady only; no energy balance is checked; outlet temperature and pressure are set by the model; unqualified";
export const isStudioUnit = (unit: Pick<DraftObject, "type">) => unit.type === STUDIO_UNIT;

/** The registry shape of a palette row; ports and parameters come from the published contract. */
export function paletteSpec(entry: StudioPaletteUnit): RegistryUnit {
  return {
    type: STUDIO_UNIT, label: entry.label, owner: entry.owner, culture_rule: entry.culture_rule, inlets: entry.inlets, outlets: entry.outlets,
    required_inlets: entry.required_inlets, modes: [], energy_inlets: [], energy_outlets: [],
    params: entry.params.map((item) => ({ key: item.key, label: item.label, kind: "studio", modes: [], default: null, minimum: item.minimum, maximum: item.maximum }))
  };
}

/** The ports and parameters of a placed unit come from its own pinned contract, never from the latest version. */
export function placedSpec(unit: DraftObject): RegistryUnit | undefined {
  const studio = unit.studio;
  if (!isStudioUnit(unit) || !studio) return undefined;
  const contract = studio.contract;
  return {
    type: STUDIO_UNIT, label: studio.name, owner: "jarvis_bio", culture_rule: "studio", inlets: contract.inlets.map((port) => port.name),
    outlets: contract.outlets.map((port) => port.name), required_inlets: contract.inlets.length, modes: [], energy_inlets: [], energy_outlets: [],
    params: contract.parameters.map((item) => ({ key: item.name, label: item.label, kind: "studio", modes: [], default: null, minimum: item.min ?? null, maximum: item.max ?? null }))
  };
}

export const portLabels = (unit: DraftObject, end: "inlets" | "outlets"): string[] =>
  (unit.studio?.contract[end] ?? []).map((port) => (port.label ? `${port.label} (${port.name})` : port.name));

export const pinOf = (unit: DraftObject): StudioPin | null => {
  const pin = unit.model as Partial<StudioPin> | null | undefined;
  return pin?.version_id && pin.source_sha256 && pin.contract_sha256 ? { version_id: pin.version_id, source_sha256: pin.source_sha256, contract_sha256: pin.contract_sha256 } : null;
};

export const addStudioUnitOp = (entry: StudioPaletteUnit, id: string, tag: string, spot: { x: number; y: number }): DraftOp =>
  ({ op: "add_unit", id, type: STUDIO_UNIT, tag, version_id: entry.version_id, ...spot });

export const updateStudioUnitOp = (unit: DraftObject, status: StudioPinStatus): DraftOp | null =>
  status.update_available && status.latest_version_id ? { op: "update_studio_unit", unit: unit.id, version_id: status.latest_version_id } : null;

/** Parameter values travel as value + the declared unit; the backend converts and enforces bounds. */
export function parameterOp(unit: DraftObject, entered: Record<string, string>): DraftOp | null {
  const declared = new Map((unit.studio?.contract.parameters ?? []).map((item) => [item.name, item]));
  const values: Record<string, { value: number; unit: string }> = {};
  for (const [name, text] of Object.entries(entered)) {
    const variable = declared.get(name);
    const value = Number(text);
    if (!variable || !text.trim() || !Number.isFinite(value)) return null;
    values[name] = { value, unit: variable.unit };
  }
  return Object.keys(values).length ? { op: "set_unit_params", unit: unit.id, values } : null;
}

/** Same unit in the same place as Studio's `?model=` deep link ("Inspect"). */
export const inspectHref = (unit: DraftObject) => (unit.studio ? `/design/studio?model=${encodeURIComponent(unit.studio.model_spec_id)}` : null);

const short = (hash: string) => hash.slice(0, 12);
/** Provenance rows of one custom unit in a run; optional fields are shown only when the backend sent them. */
export function provenanceRows(item: StudioRunProvenance): { label: string; value: string }[] {
  const rows = [
    { label: "Version", value: `${item.name ?? "Studio unit"} ${item.version_label}` },
    { label: "Source SHA-256", value: short(item.source_sha256) },
    { label: "Contract SHA-256", value: short(item.contract_sha256) },
    { label: "Calls", value: String(item.call_count) },
    { label: "Runtime", value: (item.runtime_classifications ?? []).join(", ") || "not recorded" },
    { label: "Duration", value: `${Number((item.duration_s ?? 0).toPrecision(4))} s` }
  ];
  if (item.cache_hits !== undefined) rows.push({ label: "Cache hits", value: String(item.cache_hits) });
  if (item.sandbox_seconds !== undefined) rows.push({ label: "Sandbox time", value: `${Number(item.sandbox_seconds.toPrecision(4))} s` });
  return rows;
}
export const supersededText = (item: StudioRunProvenance) => item.superseded
  ? `Superseded: this run used v${item.version_number}; ${item.latest_version_number ? `v${item.latest_version_number} is published` : "the pin has changed"}. The run is unchanged.`
  : null;
