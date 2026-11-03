import { useCallback, useEffect, useRef, useState } from "react";
import { designTarget } from "../app/designTargets";
import { DesignStatus, Empty } from "../components/design/DesignStatus";
import { readableKey, regimeLabel, shortWhen } from "../components/design/designText";
import CfdRunsPanel from "./CfdRunsPanel";
import ReducedModelPublishPanel from "./ReducedModelPublishPanel";
import { getComponentCfdToolchain, getComponentFluidDomain, listComponentStudyTemplates, setComponentStudyTemplate,
  type CfdRun, type CfdToolchainStatus, type DesignComponent, type FluidDomain, type PipeStudyContract, type StudyTemplate } from "../api/designProject";
import { buildStudyContract, formFromStudyContract, initialStudyTemplateForm, normalizeStudyTemplateForm, shortDigest, TURBULENCE_MODELS,
  wallTreatmentsFor, type StudyRegime, type StudyTemplateFormState, type TurbulenceModel, type WallTreatment } from "./studyTemplateForm";

type Props = {
  projectId: string; versionId: string; component: DesignComponent;
  /** Called after a template is recorded so the page reloads the component (revision) and reduced-model status. */
  onChanged: () => Promise<void> | void;
  /** Passed to the runs panel: the page's results comparison follows the runs read there. */
  onRunsChange?(runs: CfdRun[]): void;
};

const message = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;
const toolReason = (reason: string | null) => ({
  TOOL_DISABLED: "disabled in the tool registry", TOOL_BINARY_MISSING: "binary missing",
  TOOL_HASH_MISMATCH: "binary digest mismatch", TOOL_UNKNOWN: "not registered",
  TOOL_ACCESS_ERROR: "binary cannot be read",
}[reason ?? ""] ?? "registry check failed");

export default function StudyTemplatesPanel({ projectId, versionId, component, onChanged, onRunsChange }: Props) {
  const [templates, setTemplates] = useState<StudyTemplate[] | null>(null);
  const [domain, setDomain] = useState<FluidDomain | null>(null);
  const [domainWhy, setDomainWhy] = useState<string | null>(null);
  const [toolchain, setToolchain] = useState<CfdToolchainStatus | null>(null);
  const [toolchainWhy, setToolchainWhy] = useState<string | null>(null);
  const [form, setForm] = useState<StudyTemplateFormState>(initialStudyTemplateForm);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    const [list, derived, tools] = await Promise.allSettled([
      listComponentStudyTemplates(projectId, versionId, component.id), getComponentFluidDomain(projectId, versionId, component.id),
      getComponentCfdToolchain(projectId, versionId, component.id)]);
    if (seq !== loadSeq.current) return;
    if (list.status === "fulfilled") setTemplates(list.value); else { setTemplates([]); setError(message(list.reason, "Study templates are unavailable.")); }
    if (derived.status === "fulfilled") { setDomain(derived.value); setDomainWhy(null); }
    else { setDomain(null); setDomainWhy(message(derived.reason, "The tube fluid domain is unavailable.")); }
    if (tools.status === "fulfilled") { setToolchain(tools.value); setToolchainWhy(null); }
    else { setToolchain(null); setToolchainWhy(message(tools.reason, "CFD tool status is unavailable.")); }
  }, [projectId, versionId, component.id]);
  const currentComponentId = useRef(component.id);
  if (currentComponentId.current !== component.id && templates !== null) {
    currentComponentId.current = component.id;
    setTemplates(null);
  }
  useEffect(() => { currentComponentId.current = component.id; loadSeq.current++; setTemplates(null); setError(null); setEditing(null); setForm(initialStudyTemplateForm()); }, [component.id]);
  useEffect(() => { void load(); }, [load, component.revision]);

  const update = (patch: Partial<StudyTemplateFormState>) => setForm((current) => normalizeStudyTemplateForm({ ...current, ...patch }));
  const built = buildStudyContract(form, domain);
  const turbulent = form.regime === "turbulent_rans";

  const record = async () => {
    if (!built.contract) return;
    setBusy(true); setError(null);
    try {
      await setComponentStudyTemplate(projectId, versionId, component, built.contract);
      await onChanged();
    } catch (cause) {
      setError(message(cause, "Recording the study template failed."));
      void onChanged();
    } finally { setBusy(false); await load(); }
  };

  const edit = (item: StudyTemplate) => {
    const loaded = formFromStudyContract(item.contract as PipeStudyContract);
    if (!loaded) { setError("This template has advanced inputs that this form cannot safely edit."); return; }
    setForm(loaded); setEditing(item.regime); setError(null);
  };

  return <section aria-label="CFD study templates" className="design-project__reduced" {...designTarget("design.panel.cfd-studies")}>
    <h3>CFD study templates</h3>
    {error ? <p role="alert" className="design-project__error">{error}</p> : null}
    <dl className="design-project__fields">
      <div><dt>CFD tools</dt><dd>{toolchain ? toolchain.binary_verified
        ? <DesignStatus state="ready" title="Pinned binaries verified; only a run proves the case" />
        : <DesignStatus state="failed" title={toolchain.tools.filter((tool) => !tool.binary_verified).map((tool) => `${tool.tool_id}: ${toolReason(tool.reason)}`).join(", ")} />
        : toolchainWhy ? <DesignStatus state="unknown" title={toolchainWhy} /> : <DesignStatus state="checking" />}</dd></div>
      <div><dt>Tube diameter</dt><dd>{domain ? `${Number((domain.inner_diameter_m * 1000).toPrecision(4))} mm` : <Empty title={domainWhy ?? undefined} />}</dd></div>
      <div><dt>Tube length</dt><dd>{domain ? `${Number(domain.length_m.toPrecision(4))} m` : <Empty title={domainWhy ?? undefined} />}</dd></div>
    </dl>
    <table className="design-project__table" aria-label="Saved study templates">
      <thead><tr><th>Regime</th><th>Updated</th><th /></tr></thead>
      <tbody>{templates === null ? <tr><td colSpan={3}>Loading…</td></tr>
        : !templates.length ? <tr><td><Empty /></td><td><Empty /></td><td /></tr>
        : templates.map((item) => <tr key={item.regime} title={`Template ${shortDigest(item.template_digest)}`}>
          <td>{regimeLabel(item.regime)}</td><td>{shortWhen(item.updated_at)}</td>
          <td><button type="button" onClick={() => edit(item)}>Edit template</button></td>
        </tr>)}</tbody>
    </table>
    {templates ? <CfdRunsPanel projectId={projectId} versionId={versionId} componentId={component.id} templates={templates} onRunsChange={onRunsChange} /> : null}
    {templates ? <ReducedModelPublishPanel projectId={projectId} versionId={versionId} component={component} onChanged={onChanged} /> : null}
    {editing ? <p title="The current tube geometry is used when recording">Editing the {regimeLabel(editing)} template</p> : null}
    <form onSubmit={(event) => { event.preventDefault(); void record(); }} aria-label="Record a study template">
      <label htmlFor="study-regime">Regime</label>
      <select id="study-regime" value={form.regime} onChange={(event) => update({ regime: event.target.value as StudyRegime })}>
        <option value="laminar">laminar</option><option value="turbulent_rans">turbulent</option>
      </select>
      {turbulent ? <>
        <label htmlFor="study-model">Turbulence model</label>
        <select id="study-model" value={form.turbulenceModel} onChange={(event) => update({ turbulenceModel: event.target.value as TurbulenceModel })}>
          {TURBULENCE_MODELS.map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
        <label htmlFor="study-wall">Wall treatment</label>
        <select id="study-wall" value={form.wallTreatment} onChange={(event) => update({ wallTreatment: event.target.value as WallTreatment })}>
          {wallTreatmentsFor(form.regime, form.turbulenceModel).map((value) => <option key={value} value={value}>{readableKey(value)}</option>)}
        </select>
      </> : null}
      <label htmlFor="study-density">Fluid density (kg/m³)</label>
      <input id="study-density" inputMode="decimal" value={form.density} onChange={(event) => update({ density: event.target.value })} />
      <label htmlFor="study-viscosity">Dynamic viscosity (Pa·s)</label>
      <input id="study-viscosity" inputMode="decimal" value={form.viscosity} onChange={(event) => update({ viscosity: event.target.value })} />
      <label htmlFor="study-velocity">Inlet mean velocity (m/s)</label>
      <input id="study-velocity" inputMode="decimal" value={form.velocity} onChange={(event) => update({ velocity: event.target.value })} />
      <label htmlFor="study-mesh">Mesh level (0 to 4)</label>
      <input id="study-mesh" inputMode="numeric" value={form.meshLevel} onChange={(event) => update({ meshLevel: event.target.value })} />
      <p>Outlet pressure 0 Pa{built.reynolds !== null ? ` · Re ${Math.round(built.reynolds)}` : ""}</p>
      {form.velocity.trim() || !domain ? <ul aria-label="Study template problems">{built.errors.map((text) => <li key={text}>{text}</li>)}</ul> : null}
      <button type="submit" disabled={busy || !built.contract}>Record {turbulent ? "turbulent" : "laminar"} study template</button>
    </form>
  </section>;
}
