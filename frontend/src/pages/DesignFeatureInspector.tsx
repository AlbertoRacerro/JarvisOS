import { useEffect, useRef, useState } from "react";
import NumericInput from "../components/ui/NumericInput";
import { getComponentFeatureHistory, putComponentFeatureHistory,
  type ComponentFeatureHistory, type DesignComponent, type FeatureKind, type FeatureSequence, type FeatureStep } from "../api/designProject";

/** Spec 235 slice 1: form editor for a component's `feature_sequence_v1`; the server compiles, builds and attaches. */
type Param = string | { $param: string };
type DraftFeature = Omit<FeatureStep, "params"> & { params: Record<string, Param> };

const KINDS: { kind: FeatureKind; label: string; defaults: Record<string, number> }[] = [
  { kind: "tube_run", label: "Tube", defaults: { outer_d_mm: 20, wall_t_mm: 2, length_mm: 200 } },
  { kind: "bend", label: "Bend", defaults: { outer_d_mm: 20, wall_t_mm: 2, bend_radius_mm: 50, angle_deg: 90 } },
  { kind: "manifold", label: "Manifold", defaults: { outer_d_mm: 40, wall_t_mm: 2, length_mm: 400, branch_count: 3, branch_outer_d_mm: 10 } },
  { kind: "serpentine", label: "Serpentine", defaults: { outer_d_mm: 20, wall_t_mm: 2, segment_length_mm: 200, segment_count: 3, bend_radius_mm: 50 } }
];
const LABELS: Record<string, string> = {
  outer_d_mm: "Outer Ø", wall_t_mm: "Wall", length_mm: "Length", bend_radius_mm: "Bend radius", angle_deg: "Angle",
  branch_count: "Branches", branch_outer_d_mm: "Branch Ø", segment_length_mm: "Run length", segment_count: "Runs"
};
const COUNTS: Record<string, [number, number]> = { branch_count: [1, 12], segment_count: [1, 64] };
// Ranges and relations mirror the 234 compiler refusals (finite, positive, wall below the outer radius, ...).
const RANGES: Record<string, [number, number]> = {
  outer_d_mm: [2, 1000], wall_t_mm: [0.5, 100], length_mm: [1, 5000], branch_outer_d_mm: [2, 1000],
  segment_length_mm: [1, 5000], bend_radius_mm: [1, 5000], angle_deg: [0, 180]
};
// A straight-through feature joins the previous outlet at its own port_a; manifolds are never auto-connected.
const CONNECTABLE = new Set<FeatureKind>(["tube_run", "bend", "serpentine"]);
const unitOf = (key: string) => (key === "angle_deg" ? "deg" : COUNTS[key] ? "" : "mm");

const toDraft = (sequence: FeatureSequence | null): DraftFeature[] => (sequence?.features ?? []).map((feature) => ({
  ...feature, params: Object.fromEntries(Object.entries(feature.params).map(([key, value]) => [key, typeof value === "number" ? String(value) : value]))
}));

/** Why the draft cannot be sent, or null; `$param` references are resolved by the server, so they are skipped. */
export function featureDraftProblem(features: DraftFeature[]): string | null {
  if (!features.length) return "Add a feature first.";
  if (!features.some((feature) => feature.enabled)) return "At least one feature must be enabled.";
  for (const feature of features) {
    const values: Record<string, number> = {};
    for (const [key, raw] of Object.entries(feature.params)) {
      if (typeof raw !== "string") continue;
      const value = raw.trim() === "" ? NaN : Number(raw);
      const label = `${feature.feature_id}: ${LABELS[key] ?? key}`;
      if (!Number.isFinite(value)) return `${label} must be a finite number.`;
      if (!(value > 0)) return `${label} must be greater than 0.`;
      if (COUNTS[key]) {
        const [low, high] = COUNTS[key];
        if (!Number.isInteger(value) || value < low || value > high) return `${label} must be a whole number from ${low} to ${high}.`;
      } else if (RANGES[key] && (value > RANGES[key][1] || (key !== "angle_deg" && value < RANGES[key][0]))) {
        return `${label} must be between ${RANGES[key][0]} and ${RANGES[key][1]} ${unitOf(key)}.`;
      }
      values[key] = value;
    }
    const { outer_d_mm: outer, wall_t_mm: wall } = values;
    if (outer !== undefined && wall !== undefined && wall * 2 >= outer) return `${feature.feature_id}: Wall must be less than half the outer Ø.`;
    if ((feature.kind === "bend" || feature.kind === "serpentine") && values.bend_radius_mm !== undefined && outer !== undefined && values.bend_radius_mm < outer) return `${feature.feature_id}: Bend radius must be at least the outer Ø.`;
    if (feature.kind === "manifold" && values.branch_outer_d_mm !== undefined && outer !== undefined && values.branch_count !== undefined && values.length_mm !== undefined) {
      if (values.branch_outer_d_mm >= outer) return `${feature.feature_id}: Branch Ø must be less than the outer Ø.`;
      if (wall !== undefined && wall * 2 >= values.branch_outer_d_mm) return `${feature.feature_id}: Wall must be less than half the branch Ø.`;
      if (values.branch_outer_d_mm >= values.length_mm / (values.branch_count + 1)) return `${feature.feature_id}: Branches do not fit along the length.`;
    }
  }
  return null;
}

const toSequence = (base: FeatureSequence | null, features: DraftFeature[]): FeatureSequence => ({
  schema_version: "feature_sequence_v1", parameters: base?.parameters ?? [],
  features: features.map((feature) => ({ ...feature, params: Object.fromEntries(Object.entries(feature.params).map(([key, value]) => [key, typeof value === "string" ? Number(value) : value])) }))
});

type Props = Readonly<{
  projectId: string; versionId: string; component: DesignComponent;
  /** Process draft revision for Process-linked components (sent as `expected_draft_revision`), else null. */
  draftRevision: string | null;
  disabled?: boolean;
  onApplied(): Promise<void>;
}>;

export default function DesignFeatureInspector({ projectId, versionId, component, draftRevision, disabled, onApplied }: Props) {
  const [read, setRead] = useState<ComponentFeatureHistory | null>(null);
  const [features, setFeatures] = useState<DraftFeature[]>([]);
  const [dirty, setDirty] = useState(false);
  const [undoStack, setUndoStack] = useState<FeatureSequence[]>([]);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;
  const scope = `${projectId}:${versionId}:${component.id}`;
  const scopeRef = useRef(scope);

  // The session undo stack belongs to one component of one version.
  useEffect(() => {
    scopeRef.current = scope;
    setUndoStack([]); setRead(null); setFeatures([]); setDirty(false); setProblem(null); setRefused(null);
  }, [scope]);

  useEffect(() => {
    let live = true;
    setLoadError(null);
    void getComponentFeatureHistory(projectId, versionId, component.id).then((item) => {
      if (!live) return;
      setRead(item);
      if (!dirtyRef.current) setFeatures(toDraft(item.sequence));
    }).catch((cause: unknown) => { if (live) setLoadError(cause instanceof Error ? cause.message : "Feature history is unavailable."); });
    return () => { live = false; };
  }, [scope, component.revision, component.geometry_candidate_id]);

  const edit = (next: DraftFeature[]) => { setFeatures(next); setDirty(true); setProblem(null); setRefused(null); };
  const add = (kind: FeatureKind) => {
    const entry = KINDS.find((item) => item.kind === kind)!;
    const taken = new Set(features.map((feature) => feature.feature_id));
    let n = 1;
    while (taken.has(`${kind}_${n}`)) n += 1;
    const previous = [...features].reverse().find((feature) => feature.enabled);
    const connect = previous && CONNECTABLE.has(previous.kind) && CONNECTABLE.has(kind);
    edit([...features, {
      feature_id: `${kind}_${n}`, enabled: true, kind,
      params: Object.fromEntries(Object.entries(entry.defaults).map(([key, value]) => [key, String(value)])),
      connect_to: connect ? { from_port: "port_a", to_feature_id: previous.feature_id, to_port: "port_b" } : null
    }]);
  };
  const remove = (id: string) => {
    const dependent = features.find((feature) => feature.connect_to?.to_feature_id === id);
    if (dependent) { setProblem(`${dependent.feature_id} connects to ${id}; remove ${dependent.feature_id} first.`); return; }
    edit(features.filter((feature) => feature.feature_id !== id));
  };
  const patch = (id: string, change: (feature: DraftFeature) => DraftFeature) => edit(features.map((feature) => (feature.feature_id === id ? change(feature) : feature)));

  const send = async (sequence: FeatureSequence, undoing: boolean) => {
    setBusy(true); setProblem(null); setRefused(null);
    const previous = read?.sequence ?? null;
    try {
      const next = await putComponentFeatureHistory(projectId, versionId, component, sequence, draftRevision);
      if (scopeRef.current !== scope) return;
      setRead(next); setFeatures(toDraft(next.sequence)); setDirty(false);
      setUndoStack((stack) => (undoing ? stack.slice(0, -1) : previous ? [...stack, previous] : stack));
      await onApplied();
    } catch (cause) {
      if (scopeRef.current === scope) setRefused(cause instanceof Error ? cause.message : "Rebuild was refused.");
    } finally { setBusy(false); }
  };
  const rebuild = () => {
    const issue = featureDraftProblem(features);
    if (issue) { setProblem(issue); return; }
    void send(toSequence(read?.sequence ?? null, features), false);
  };
  const undo = () => { const last = undoStack[undoStack.length - 1]; if (last) void send(last, true); };

  const locked = disabled || busy;
  return <section aria-label="Feature inspector" className="design-feature-inspector">
    <h4>Features</h4>
    <p className="design-feature-inspector__lineage" role="status">
      {loadError ? loadError : !read ? "Loading feature history…"
        : read.persisted ? `Feature history revision ${read.revision}${read.stale_against_attached_candidate ? " · stale against the attached geometry" : " · matches the attached geometry"}`
          : read.sequence ? "Not saved yet: started from the attached geometry (synthesized feature list)."
            : read.attached_candidate_id ? "The attached geometry has no editable feature list; rebuilding replaces it."
              : "No geometry yet. Add a feature, then rebuild."}
    </p>
    {features.length ? <ol className="design-feature-inspector__list">
      {features.map((feature) => <li key={feature.feature_id}>
        <div className="design-project__node-head">
          <strong>{KINDS.find((item) => item.kind === feature.kind)?.label}</strong><code>{feature.feature_id}</code>
          <label><input type="checkbox" checked={feature.enabled} disabled={locked}
            onChange={(event) => patch(feature.feature_id, (item) => ({ ...item, enabled: event.target.checked }))} /> Enabled</label>
          <button type="button" disabled={locked} aria-label={`Remove ${feature.feature_id}`} onClick={() => remove(feature.feature_id)}>Remove</button>
        </div>
        <dl className="design-project__fields">
          {Object.entries(feature.params).map(([key, value]) => <div key={key}>
            <dt><label htmlFor={`feature-${feature.feature_id}-${key}`}>{LABELS[key] ?? key}{unitOf(key) ? ` (${unitOf(key)})` : ""}</label></dt>
            <dd>{typeof value === "string"
              ? <NumericInput id={`feature-${feature.feature_id}-${key}`} value={value} disabled={locked} integer={Boolean(COUNTS[key])}
                  min={0} minExclusive onValueChange={(text) => patch(feature.feature_id, (item) => ({ ...item, params: { ...item.params, [key]: text } }))} />
              : <span id={`feature-${feature.feature_id}-${key}`} title="Parameter reference, preserved unchanged">
                {`= ${value.$param} (${read?.sequence?.parameters.find((item) => item.name === value.$param)?.value ?? "?"})`}</span>}</dd>
          </div>)}
        </dl>
        {feature.connect_to ? <p className="design-feature-inspector__note">Connected to {feature.connect_to.to_feature_id}.{feature.connect_to.to_port}</p> : null}
        {feature.frame ? <p className="design-feature-inspector__note">Frame: origin {feature.frame.origin.join(", ")} · direction {feature.frame.direction.join(", ")} (preserved)</p> : null}
      </li>)}
    </ol> : null}
    <div className="design-feature-inspector__actions" role="group" aria-label="Add feature">
      {KINDS.map((item) => <button key={item.kind} type="button" disabled={locked} onClick={() => add(item.kind)}>Add {item.label.toLowerCase()}</button>)}
    </div>
    {problem ? <p role="alert" className="design-project__error">{problem}</p> : null}
    {refused ? <p role="alert" className="design-project__error">Not applied: {refused} The previous geometry is still attached.</p> : null}
    {dirty && !refused ? <p role="status">Edits not applied yet.</p> : null}
    <div className="design-feature-inspector__actions">
      <button type="button" disabled={locked || !features.length || (!dirty && !refused && read?.persisted)} onClick={rebuild}>Rebuild geometry</button>
      <button type="button" disabled={locked || !undoStack.length} onClick={undo}>Undo</button>
    </div>
  </section>;
}
