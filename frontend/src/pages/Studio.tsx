import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { Navigate } from "../app/AppLink";
import DerivedDocumentation from "../components/studio/DerivedDocumentation";
import { EngineeringPropertiesPanel, type EngineeringPropertiesController } from "../components/engineering/EngineeringProperties";
import { getRun, listRuns, type SimulationRunDetail } from "../api/runs";
import WorkshopDraftPanel from "../components/studio/WorkshopDraftPanel";
import PythonEditor from "../components/studio/PythonEditor";
import DesignSplitter from "../components/design/DesignSplitter";
import { designTarget } from "../app/designTargets";
import { ContractTables, CultureGenerationField, PortForm, SymbolField, UnitCases, UnitReport } from "../components/studio/UnitAuthoring";
import { alignCase, blankPort, contractIssues } from "../components/studio/unitLogic";
import { GeometryArtifacts, GeometryCases, GeometryFacts, GeometryReport } from "../components/studio/GeometryAuthoring";
import { GEOMETRY_NOTICE, geometryRunOutput, runFailure } from "../components/studio/geometryLogic";
import { getDraftRegistry } from "../api/processDraft";
import ProcedureBench from "../components/studio/ProcedureBench";
import { ProcedureCases, ProcedureReportView, Refusal, SimulationNotice, StepEditor } from "../components/studio/ProcedureAuthoring";
import { blankStep, canPublishProcedure, parameterValuesIssues } from "../components/studio/procedureLogic";
import { isProcedureReport, listBenchDrafts, type BenchDraft, type ProcedureCase, type ProcedureStep } from "../api/studioProcedure";
import { canPublishStudioDraft } from "./studioState";
import {
  createStudioModel, getStudioModel, openStudioExample, getStudioRuntimeStatus, listStudioCategories, listStudioLibrary, listStudioModels, previewStudioDocumentation, previewStudioBuildDocumentation,
  publishStudioModel, reviseStudioModel, saveStudioModel, testStudioModel, geometryPreviewUrl, geometryRunArtifactUrl,
  StudioApiError, type StudioCase, type StudioCategoryId, type StudioDerived, type StudioGeometryCase, type StudioCategory, type StudioLibraryEntry, type StudioModel, type StudioPort, type StudioRuntimeStatus, type StudioUnitCase, type StudioVariable
} from "../api/studio";
import "./studio.css";
import NumericInput from "../components/ui/NumericInput";
import { numericOrUndefined } from "../components/ui/numericStep";

type Tab = "contract" | "code" | "documentation" | "tests" | "versions" | "run" | "steps" | "bench";
const tabTitle = (tab: Tab) => tab === "bench" ? "Test bench" : tab === "code" ? "Explanation & code" : tab[0].toUpperCase() + tab.slice(1);
const blankVariable = (): StudioVariable => ({ name: "", label: "", unit: "dimensionless", description: "" });
const defaultCategories: StudioCategory[] = [
  { id: "calculation", label: "Calculation / correlation", available: true },
  { id: "process_unit", label: "Process unit", available: true },
  { id: "dynamic", label: "Dynamic model", available: false, reason: "Time integration is not available in this Studio slice." },
  { id: "procedure", label: "Procedure", available: true },
  { id: "geometry", label: "Geometry", available: true },
  { id: "optimization", label: "Optimization", available: false, reason: "Optimization execution is not yet available." },
  { id: "cfd_fem", label: "CFD / FEM", available: false, reason: "CFD and FEM execution is not yet available." }
];
function VariableForm({ title, rows, onChange, allowEmpty = false, nameLabel = "Python name", requireDescription = true }: { title: string; rows: StudioVariable[]; onChange(rows: StudioVariable[]): void; allowEmpty?: boolean; nameLabel?: string; requireDescription?: boolean }) {
  const update = (index: number, patch: Partial<StudioVariable>) => onChange(rows.map((row, i) => i === index ? { ...row, ...patch } : row));
  return <fieldset className="studio-variables"><legend>{title}</legend>
    {rows.map((row, index) => <div className="studio-variable" key={index}>
      <label>{nameLabel}<input required pattern="[A-Za-z_][A-Za-z0-9_]*" value={row.name} onChange={(event) => update(index, { name: event.target.value })} placeholder="e.g. pipe_length" /></label>
      <label>Label<input required value={row.label} onChange={(event) => update(index, { label: event.target.value })} placeholder="e.g. Pipe length" /></label>
      <label>Unit<input required value={row.unit} onChange={(event) => update(index, { unit: event.target.value })} placeholder="e.g. meter" /></label>
      <label>Description<input required={requireDescription} value={row.description} onChange={(event) => update(index, { description: event.target.value })} placeholder="Meaning and basis" /></label>
      <label>Minimum<NumericInput value={row.min} onValueChange={(text) => update(index, { min: numericOrUndefined(text) ?? null })} /></label>
      <label>Maximum<NumericInput value={row.max} onValueChange={(text) => update(index, { max: numericOrUndefined(text) ?? null })} /></label>
      <button type="button" onClick={() => onChange(rows.filter((_, i) => i !== index))} disabled={rows.length === 1 && !allowEmpty}>Remove</button>
    </div>)}
    <button type="button" onClick={() => onChange([...rows, blankVariable()])}>+ Add {title.toLowerCase().slice(0, -1)}</button>
  </fieldset>;
}
function NumberMapEditor({ title, variables, values, onChange }: { title: string; variables: StudioVariable[]; values: Record<string, number>; onChange(values: Record<string, number>): void }) {
  return <div className="studio-number-map"><strong>{title}</strong>{variables.map((variable) => <label key={variable.name}>{variable.label || variable.name} [{variable.unit}]<NumericInput min={variable.min ?? undefined} max={variable.max ?? undefined} value={values[variable.name]} onValueChange={(text) => {
    const next = { ...values }; const parsed = numericOrUndefined(text); if (parsed === undefined) delete next[variable.name]; else next[variable.name] = parsed; onChange(next);
  }} /></label>)}</div>;
}
const variable = (name: string, label: string, unit: string, description: string): StudioVariable => ({ name, label, unit, description });
// Guided presets prefill the contract only; the Python body is always written by the operator.
const presets: { id: string; label: string; name: string; question: string; inputs: StudioVariable[]; outputs: StudioVariable[] }[] = [
  { id: "pipe-pressure-loss", label: "Pipe pressure loss", name: "Pipe pressure-loss correlation", question: "What is the frictional pressure loss along a straight pipe?",
    inputs: [variable("flow_velocity", "Flow velocity", "m/s", "Mean velocity in the pipe"), variable("pipe_length", "Pipe length", "m", "Straight pipe length"), variable("pipe_diameter", "Pipe diameter", "m", "Inner diameter"), variable("density", "Fluid density", "kg/m**3", "Fluid density at operating conditions"), variable("friction_factor", "Darcy friction factor", "dimensionless", "Darcy friction factor")],
    outputs: [variable("pressure_loss", "Pressure loss", "Pa", "Frictional pressure loss")] },
  { id: "reynolds-number", label: "Reynolds number", name: "Pipe Reynolds number", question: "What is the flow regime in the pipe?",
    inputs: [variable("flow_velocity", "Flow velocity", "m/s", "Mean velocity in the pipe"), variable("pipe_diameter", "Pipe diameter", "m", "Inner diameter"), variable("density", "Fluid density", "kg/m**3", "Fluid density"), variable("viscosity", "Dynamic viscosity", "Pa*s", "Dynamic viscosity")],
    outputs: [variable("reynolds_number", "Reynolds number", "dimensionless", "Ratio of inertial to viscous forces")] }
];
const STEADY_LIMITS = "Steady only; no energy balance is checked; outlet temperature and pressure are set by the model; unqualified.";
const CARRIED_NOTICE = "jarvisos:studio:carried-notice";
const takeCarriedNotice = (): string | null => { const value = sessionStorage.getItem(CARRIED_NOTICE); sessionStorage.removeItem(CARRIED_NOTICE); return value; };
const emptyCase = (): StudioCase => ({ inputs: {}, expected_outputs: {}, tolerance: 1e-6 });
const runOutputs = (payload: string | null): string => {
  try {
    const outputs = (JSON.parse(payload ?? "{}") as { outputs?: Record<string, { value: unknown; unit: string }> }).outputs ?? {};
    return Object.entries(outputs).map(([name, item]) => `${name} = ${String(item.value)} ${item.unit}`).join(" · ");
  } catch { return ""; }
};
export default function Studio({ workspaceId, navigate, engineeringProperties, onModelSelectionChange, onWorkspaceChange, requestedModelId, startWizard = false, embedded = false, onExit, contextLabel }: { workspaceId: string | null; navigate: Navigate; engineeringProperties: EngineeringPropertiesController; onModelSelectionChange?(id: string | null): void; onWorkspaceChange?(id: string): void; requestedModelId?: string | null; startWizard?: boolean; embedded?: boolean; onExit?(): void; contextLabel?: string }) {
  const [runs, setRuns] = useState<SimulationRunDetail[]>([]);
  const [models, setModels] = useState<StudioModel[]>([]);
  const [library, setLibrary] = useState<StudioLibraryEntry[]>([]);
  const [categories, setCategories] = useState<StudioCategory[]>(defaultCategories);
  const [runtime, setRuntime] = useState<StudioRuntimeStatus | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [model, setModel] = useState<StudioModel | null>(null);
  const [liveMath, setLiveMath] = useState<StudioDerived | null>(null);
  const [mathError, setMathError] = useState<string | null>(null);
  const [activeSourceLine, setActiveSourceLine] = useState<number | null>(null);
  const [jumpToSourceLine, setJumpToSourceLine] = useState<number | null>(null);
  const [selectedRange, setSelectedRange] = useState<{ from: number; to: number } | null>(null);
  const [linkedLine, setLinkedLine] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("contract");
  // Spec 230: the embedded Workshop opens focused (explanation/code pair over the whole main area); the Library and
  // the explanation/code split are visual-only preferences. "split" | "explanation" | "code" picks which pane is shown.
  const [focused, setFocused] = useState(embedded);
  const [libraryOpen, setLibraryOpen] = useState(!embedded);
  const [pairView, setPairView] = useState<"split" | "explanation" | "code">("split");
  const [mathWidth, setMathWidth] = useState<number | null>(null);
  const [notesOpen, setNotesOpen] = useState(false);
  const pairRef = useRef<HTMLDivElement>(null);
  const [wizard, setWizard] = useState(false);
  const [wizardStep, setWizardStep] = useState(0);
  const [approach, setApproach] = useState("");
  const [testIntent, setTestIntent] = useState("");
  const [custom, setCustom] = useState(false);
  const [name, setName] = useState(""); const [question, setQuestion] = useState("");
  const [inputs, setInputs] = useState<StudioVariable[]>([blankVariable()]); const [outputs, setOutputs] = useState<StudioVariable[]>([blankVariable()]);
  const [source, setSource] = useState(""); const [annotations, setAnnotations] = useState(""); const [assumptions, setAssumptions] = useState("");
  const [cases, setCases] = useState<StudioCase[]>([]);
  // Spec 190: the process_unit category declares ports, parameters, an optional symbol and culture generation.
  const [wizardCategory, setWizardCategory] = useState<StudioCategoryId>("calculation");
  // Spec 189: the geometry category declares inputs only; build returns one build123d solid.
  const [geometryCases, setGeometryCases] = useState<StudioGeometryCase[]>([]);
  // Spec 191: procedures declare parameters and typed temporal steps.
  const [steps, setSteps] = useState<ProcedureStep[]>([]); const [procCases, setProcCases] = useState<ProcedureCase[]>([]); const [benchDrafts, setBenchDrafts] = useState<BenchDraft[]>([]); const [tagDraftId, setTagDraftId] = useState(""); const [errorCode, setErrorCode] = useState<string | null>(null);
  const [inlets, setInlets] = useState<StudioPort[]>([blankPort()]); const [outlets, setOutlets] = useState<StudioPort[]>([blankPort()]);
  const [parameters, setParameters] = useState<StudioVariable[]>([]); const [symbol, setSymbol] = useState(""); const [generation, setGeneration] = useState<string[]>([]);
  const [unitCases, setUnitCases] = useState<StudioUnitCase[]>([]); const [symbolRefusal, setSymbolRefusal] = useState<string | null>(null); const [compounds, setCompounds] = useState<string[]>([]);
  const [acceptedSource, setAcceptedSource] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null); const [notice, setNotice] = useState<string | null>(takeCarriedNotice);
  const contractKey = JSON.stringify([wizardCategory, name, question, inputs, outputs, inlets, outlets, parameters]);
  useEffect(() => { setAcceptedSource(null); }, [contractKey]);
  const isProc = model?.category === "procedure";
  const sourceDirty = Boolean(model && !isProc && source !== model.source);
  const isUnit = model?.category === "process_unit";
  const isGeometry = model?.category === "geometry";
  const unitContract = { inlets, outlets, parameters, symbol: symbol.trim() ? symbol : null, culture_generation: generation };
  const contractDirty = Boolean(model && (name !== model.name || question !== model.engineering_question || (isProc
    ? JSON.stringify([parameters, steps]) !== JSON.stringify([model.parameters ?? [], model.steps ?? []])
    : isUnit
    ? JSON.stringify([inlets, outlets, parameters, generation]) !== JSON.stringify([model.inlets, model.outlets, model.parameters, model.culture_generation]) || symbol.trim() !== (model.symbol ?? "").trim()
    : JSON.stringify(inputs) !== JSON.stringify(model.inputs) || (!isGeometry && JSON.stringify(outputs) !== JSON.stringify(model.outputs)))));
  const textDirty = Boolean(model && (annotations !== model.annotations || assumptions !== model.assumptions));
  const draftDirty = sourceDirty || contractDirty || textDirty;
  const runtimeReady = Boolean(runtime?.enabled && runtime?.available);
  const locked = model?.status === "published";
  const activeCases = isProc ? procCases : isUnit ? unitCases : isGeometry ? geometryCases : cases;
  const issues = isUnit || (wizard && wizardCategory === "process_unit") ? contractIssues({ inlets, outlets, parameters }) : [];
  const validVariables = (rows: StudioVariable[], descriptionRequired = true) => rows.every((row) =>
    /^[A-Za-z_][A-Za-z0-9_]*$/.test(row.name) && !!row.label.trim() && !!row.unit.trim() && (!descriptionRequired || !!row.description.trim()));
  const guidedFieldsReady = wizardCategory === "process_unit"
    ? issues.length === 0 && inlets.every((port) => !!port.name.trim() && !!port.label.trim()) && outlets.every((port) => !!port.name.trim() && !!port.label.trim()) && validVariables(parameters)
    : wizardCategory === "procedure" ? validVariables(parameters, true)
    : wizardCategory === "geometry" ? inputs.length > 0 && validVariables(inputs)
    : inputs.length > 0 && outputs.length > 0 && validVariables(inputs) && validVariables(outputs);
  const newest = useMemo(() => model?.versions[model.versions.length - 1] ?? null, [model]);
  useEffect(() => { onModelSelectionChange?.(model?.id ?? null); }, [model?.id, onModelSelectionChange]);
  useEffect(() => {
    if (!model || model.category === "procedure" || source === model.source) { setLiveMath(null); setMathError(null); return; }
    let alive = true;
    setLiveMath(null); setMathError(null);
    const timer = window.setTimeout(() => {
      void (model.category === "geometry" ? previewStudioBuildDocumentation(source) : previewStudioDocumentation(source)).then((derived) => { if (alive) setLiveMath(derived); }).catch((cause: unknown) => { if (alive) setMathError(cause instanceof Error ? cause.message : "Mathematical preview is unavailable."); });
    }, 350);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [model, source]);

  const adopt = (next: StudioModel) => { setModel(next); setSelectedId(next.id); setName(next.name); setQuestion(next.engineering_question); setInputs(next.inputs ?? [blankVariable()]); setOutputs(next.outputs ?? [blankVariable()]); setSource(next.source); setAnnotations(next.annotations ?? ""); setAssumptions(next.assumptions ?? "");
    const unit = next.category === "process_unit"; const shape = next.category === "geometry"; const proc = next.category === "procedure";
    setCases(unit || shape || proc ? [] : (next.test_cases ?? []) as StudioCase[]);
    setUnitCases(unit ? (next.test_cases ?? []) as StudioUnitCase[] : []);
    setGeometryCases(shape ? (next.test_cases ?? []) as StudioGeometryCase[] : []);
    setProcCases(proc ? (next.test_cases ?? []) as ProcedureCase[] : []);
    if (proc) { setParameters(next.parameters ?? []); setSteps(next.steps ?? []); return; }
    if (unit) { setInlets(next.inlets ?? []); setOutlets(next.outlets ?? []); setParameters(next.parameters ?? []); setSymbol(next.symbol ?? ""); setGeneration(next.culture_generation ?? []); setSymbolRefusal(null); } };
  const refreshLists = (ws: string) => Promise.all([listStudioModels(ws), listStudioLibrary(ws)]).then(([nextModels, nextLibrary]) => { setModels(nextModels); setLibrary(nextLibrary); });
  useEffect(() => {
    const receive = (event: Event) => {
      const detail = (event as CustomEvent<{ workspaceId?: string; surface?: string; state?: string; draftId?: string | null }>).detail;
      if (!workspaceId || detail?.workspaceId !== workspaceId || detail.surface !== "studio" || detail.state !== "applied" || !detail.draftId) return;
      if (draftDirty) { setNotice("A Studio proposal was applied. Save or discard your unsaved edits, then reopen the draft to see it."); return; }
      void getStudioModel(workspaceId, detail.draftId).then((next) => {
        adopt(next); setWizard(false); setTab("code");
        setNotice("Proposal applied as a draft. Review and edit the source, then Test before Publish.");
        return refreshLists(workspaceId);
      }).catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "Could not open the applied Studio draft."));
    };
    window.addEventListener("jarvis:workspace-action", receive);
    return () => window.removeEventListener("jarvis:workspace-action", receive);
  }, [workspaceId, draftDirty]);
  useEffect(() => {
    let alive = true; setModel(null); setSelectedId(null); setError(null); setWizard(startWizard); setWizardStep(0);
    void Promise.all([listStudioCategories(), getStudioRuntimeStatus()]).then(([nextCategories, nextRuntime]) => { if (alive) { setCategories(nextCategories); setRuntime(nextRuntime); } }).catch((cause: Error) => { if (alive) setError(cause.message); });
    if (workspaceId) void Promise.all([listStudioModels(workspaceId), listStudioLibrary(workspaceId)]).then(([nextModels, nextLibrary]) => { if (alive) { setModels(nextModels); setLibrary(nextLibrary); } }).catch((cause: Error) => { if (alive) setError(cause.message); });
    // Test streams name flowsheet compounds; the Process registry is their single source.
    if (workspaceId) void getDraftRegistry(workspaceId).then((registry) => { if (alive) setCompounds(registry.compounds); }).catch(() => undefined);
    if (workspaceId) void listBenchDrafts(workspaceId).then((next) => { if (alive) setBenchDrafts(next); }).catch(() => undefined);
    // "Inspect model" in the Process inspector opens the pinned model: /design/studio?model=<spec id>.
    const target = requestedModelId !== undefined ? requestedModelId : new URLSearchParams(window.location.search).get("model");
    if (workspaceId && target) void getStudioModel(workspaceId, target).then((next) => { if (alive) { adopt(next); setWizard(false); setTab(embedded && next.category !== "procedure" ? "code" : "versions"); } }).catch((cause: Error) => { if (alive) setError(cause.message); });
    return () => { alive = false; };
  }, [workspaceId, requestedModelId, startWizard]);
  useEffect(() => { if (workspaceId && isProc && (tab === "steps" || tab === "tests")) void listBenchDrafts(workspaceId).then(setBenchDrafts).catch(() => undefined); }, [workspaceId, isProc, tab]);
  const perform = async (action: () => Promise<void>) => { setBusy(true); setError(null); setErrorCode(null); setNotice(null); setSymbolRefusal(null); try { await action(); } catch (cause) { setError(cause instanceof Error ? cause.message : "Studio action failed"); setErrorCode(cause instanceof StudioApiError ? cause.code : null); setSymbolRefusal(cause instanceof StudioApiError && cause.code === "studio_symbol_invalid" ? cause.message : null); } finally { setBusy(false); } };
  const openExample = () => { void perform(async () => {
    const example = await openStudioExample();
    const message = example.created || example.added.length ? `Example workspace ready: ${example.added.length} verified artifacts published. Open one from the Library.` : "Example workspace opened. Its existing artifacts, including your edits, were left unchanged.";
    // Switching to a newly created workspace reloads the workspace list and remounts this page.
    if (example.workspace.id !== workspaceId) sessionStorage.setItem(CARRIED_NOTICE, message);
    setNotice(message);
    onWorkspaceChange?.(example.workspace.id);
  }); };
  const open = (id: string) => { if (!workspaceId) return; void perform(async () => { const next = await getStudioModel(workspaceId, id); adopt(next); setWizard(false); setTab(next.category === "procedure" ? "contract" : "code"); }); };
  const create = (event: React.FormEvent) => { event.preventDefault(); if (!workspaceId) return; void perform(async () => {
    if (wizardCategory === "procedure") {
      const made = await createStudioModel(workspaceId, { category: "procedure", name: name.trim(), engineering_question: question.trim(), parameters, steps: [] });
      adopt(made); if (embedded) { setAnnotations(`Approach: ${approach}\nTest intent: ${testIntent}`); setAssumptions(assumptions.trim()); }
      await refreshLists(workspaceId); setWizard(false); setTab("steps"); setNotice("Procedure draft created. Review the guided notes, save, test against a Process draft, then publish."); return;
    }
    const next = await createStudioModel(workspaceId, wizardCategory === "process_unit"
      ? { category: "process_unit", name: name.trim(), engineering_question: question.trim(), inlets, outlets, parameters, symbol: symbol.trim() ? symbol : null, culture_generation: generation }
      : wizardCategory === "geometry" ? { category: "geometry", name: name.trim(), engineering_question: question.trim(), inputs }
      : { name: name.trim(), engineering_question: question.trim(), inputs, outputs });
    adopt(embedded && acceptedSource ? await saveStudioModel(workspaceId, next.id, { source: acceptedSource }) : next); if (embedded) { setAnnotations(`Approach: ${approach}\nTest intent: ${testIntent}`); setAssumptions(assumptions.trim()); }
    await refreshLists(workspaceId); setWizard(false); setTab("code"); setNotice(embedded && acceptedSource ? "Draft created with the accepted source. Review it, save, then test it." : "Draft created. Review the guided notes and Python scaffold, then save and test it.");
  }); };
  const save = () => { if (!workspaceId || !model) return; void perform(async () => { const next = await saveStudioModel(workspaceId, model.id, isProc ? { name, engineering_question: question, parameters, steps, annotations, assumptions } : isUnit ? { name, engineering_question: question, ...unitContract, symbol, source, annotations, assumptions } : isGeometry ? { name, engineering_question: question, inputs, source, annotations, assumptions } : { name, engineering_question: question, inputs, outputs, source, annotations, assumptions }); adopt(next); if (isProc) setProcCases(procCases); await refreshLists(workspaceId); setNotice("Draft saved. Documentation reflects the saved source."); }); };
  const test = () => { if (!workspaceId || !model) return; void perform(async () => {
    if (isProc) { const report = await testStudioModel(workspaceId, model.id, procCases); adopt(await getStudioModel(workspaceId, model.id)); setNotice(report.passed ? "Test passed. The report is bound to the current steps and parameters." : "Test failed. The typed refusal is shown below; nothing was corrected."); return; }
    const sent = isUnit ? unitCases.map((item) => alignCase(item, { inlets, outlets, parameters })) : cases; if (isUnit) setUnitCases(sent as StudioUnitCase[]);
    await testStudioModel(workspaceId, model.id, isGeometry ? geometryCases : sent as StudioCase[] | StudioUnitCase[]); adopt(await getStudioModel(workspaceId, model.id)); setNotice("Test report recorded against the current source and contract."); }); };
  const publish = () => { if (!workspaceId || !model) return; void perform(async () => { const version = await publishStudioModel(workspaceId, model.id); adopt(await getStudioModel(workspaceId, model.id)); await refreshLists(workspaceId); setTab("versions"); setNotice(`Published ${version.version_label}. Scientific status: unqualified.`); }); };
  const revise = () => { if (!workspaceId || !model) return; void perform(async () => { const carried = procCases; adopt(await reviseStudioModel(workspaceId, model.id)); if (isProc) setProcCases(carried); await refreshLists(workspaceId); setTab(isProc ? "steps" : "code"); setNotice("New draft opened from the latest published version."); }); };
  const versionLabels = useMemo(() => new Map((model?.versions ?? []).map((version) => [version.id, version.version_label])), [model]);
  const loadRuns = () => { if (!workspaceId || !versionLabels.size) { setRuns([]); return; } void listRuns(workspaceId).then((all) => Promise.all(all.filter((run) => run.model_version_id && versionLabels.has(run.model_version_id)).map((run) => getRun(workspaceId, run.id)))).then(setRuns).catch((cause: Error) => setError(cause.message)); };
  useEffect(loadRuns, [workspaceId, versionLabels, engineeringProperties.runResult]);
  const selectVersion = (modelId: string, versionId: string) => { if (!workspaceId) return; void perform(async () => { if (model?.id !== modelId) adopt(await getStudioModel(workspaceId, modelId)); await engineeringProperties.reload(); engineeringProperties.setSelectedId(versionId); setWizard(false); setTab("run"); }); };
  const refreshRun = () => { void engineeringProperties.reload(); loadRuns(); };
  const resetUnit = () => { setWizardCategory("calculation"); setInlets([blankPort()]); setOutlets([blankPort()]); setParameters([]); setSymbol(""); setGeneration([]); setSymbolRefusal(null); };
  const chooseCategory = (id: StudioCategoryId) => { setParameters(id === "procedure" ? [{ name: "", label: "", unit: "dimensionless", description: "" }] : []); setSteps([]); setWizardCategory(id); setName(""); setQuestion(""); setInputs([blankVariable()]); setOutputs([blankVariable()]); setCustom(id !== "calculation"); if (id === "process_unit") { setInlets([blankPort()]); setOutlets([blankPort()]); } };
  const updateCase = (index: number, patch: Partial<StudioCase>) => setCases(cases.map((item, i) => i === index ? { ...item, ...patch } : item));

  const focus = embedded && focused && !!model && !wizard;
  useEffect(() => { if (focus) document.getElementById("app-main")?.scrollTo({ top: 0 }); }, [focus]);
  const pairWidth = pairRef.current?.clientWidth ?? 1200;
  const mathBounds = { min: 280, max: Math.max(320, pairWidth - 340), fallback: Math.round(pairWidth / 2) };
  return <main className={`studio${embedded ? " studio--embedded" : ""}${focus ? " studio--focus" : ""}${embedded && !libraryOpen ? " studio--library-closed" : ""}`} aria-labelledby="studio-title">
    <header className="design-stage__header studio-header"><div className="design-stage__title-row"><div><p className="eyebrow">{contextLabel ?? "Design"}</p><h1 id="studio-title">{embedded ? "Workshop" : "Engineering Studio"}</h1>{!runtimeReady && runtime && !isProc && !(wizard && wizardCategory === "procedure") ? <details className="studio-runtime-status"><summary><span className="status-word status-word--missing" role="status"><span aria-hidden="true">●</span> Execution unavailable</span></summary><p>Studio execution unavailable: {runtime.reason || (runtime.enabled ? "Runtime unavailable" : "Runtime disabled")}. To enable it, start the backend with JARVISOS_STUDIO_USER_CODE_ENABLED=1 on a Linux or WSL2 host with bubblewrap installed. Test and Run stay refused until then.</p></details> : null}</div><div className="studio-header__actions">{embedded ? <><button type="button" aria-expanded={libraryOpen} aria-controls="studio-library" onClick={() => setLibraryOpen((current) => !current)} {...designTarget("design.workshop.library")}>{libraryOpen ? "Hide library" : "Show library"}</button>{model && !wizard ? <button type="button" onClick={() => setFocused((current) => !current)} {...designTarget("design.workshop.focus")}>{focused ? "Exit focus" : "Focus"}</button> : null}{onExit ? <button type="button" onClick={onExit}>Back to Design</button> : null}</> : null}{!embedded ? <button type="button" disabled={busy || (!!runtime && !runtimeReady)} title="Open a workspace with an AI-prepared pipe calculation, flanged spool geometry, harvest settler unit and harvest-cycle procedure" onClick={openExample}>Open example workspace</button> : null}<button type="button" onClick={() => { setWizard(true); setWizardStep(0); setApproach(""); setTestIntent(""); setAssumptions(""); setModel(null); setSelectedId(null); setName(""); setQuestion(""); setInputs([blankVariable()]); setOutputs([blankVariable()]); setCustom(false); resetUnit(); }}>+ New model</button></div></div></header>
    {error ? errorCode ? <Refusal code={errorCode} message={error} /> : <p className="studio-error" role="alert">{error}</p> : null}{notice ? <p className="studio-notice" role="status">{notice}</p> : null}
    <div className="studio-layout"><aside id="studio-library" className="studio-library" hidden={embedded && !libraryOpen}><h2>Library</h2><p>Published versions are saved here. They are not yet verified against data: scientifically unqualified.</p>
      {library.length ? library.map((item) => <article key={item.id}><button type="button" onClick={() => open(item.model_spec_id)}><strong>{item.name ? `${item.name} ` : ""}{item.version_label}</strong><span>{item.category === "process_unit" ? "Process unit · " : item.category === "geometry" ? "Geometry · " : item.category === "procedure" ? "Procedure · " : ""}Published · scientifically unqualified</span></button>{item.category === "process_unit" ? <small>Process unit · place it from the Process palette</small> : item.category === "procedure" ? <small>Procedure · apply it from its Test bench</small> : <button type="button" onClick={() => selectVersion(item.model_spec_id, item.id)}>{item.category === "geometry" ? "Select for generation" : "Select for calculation"}</button>}</article>) : <p>No published versions yet.</p>}
      <h2>Drafts & models</h2>{models.map((item) => <button className={selectedId === item.id ? "is-selected" : ""} type="button" key={item.id} onClick={() => open(item.id)}><strong>{item.name}</strong><span>{item.versions.length} published version{item.versions.length === 1 ? "" : "s"}</span></button>)}
    </aside><section className="studio-workspace">
      {wizard ? <form onSubmit={create}><h2>{wizardCategory === "process_unit" ? "New process unit" : wizardCategory === "geometry" ? "New geometry" : wizardCategory === "procedure" ? "New procedure" : "New calculation"}</h2>
        {embedded ? <p role="status">Guided initialization · step {wizardStep + 1} of 7</p> : null}
        {!embedded || wizardStep === 0 ? <>
        <div className="studio-paths" role="group" aria-label="Model category">{categories.filter((category) => category.available && ["calculation", "process_unit", "procedure", "geometry"].includes(category.id)).map((category) => <button type="button" key={category.id} aria-pressed={wizardCategory === category.id} onClick={() => chooseCategory(category.id as StudioCategoryId)}>{category.label}</button>)}</div>
        {wizardCategory === "procedure" ? <><SimulationNotice /><p>A procedure is an ordered list of typed steps (draw, separate, refill, dose, inoculate), optionally repeating or condition-triggered, with numbers that can be bound to parameters. It compiles to a Process draft schedule; it runs no code.</p></> : wizardCategory === "geometry" ? <p>A build123d part. Declare its inputs with units; the scaffold passes their magnitudes to build, which returns one solid in millimetres. {GEOMETRY_NOTICE}</p> : wizardCategory === "process_unit" ? <p>A steady process unit with 1 to 4 material inlets and outlets. It appears in the Process palette once published. {STEADY_LIMITS}</p> : <>
        <div className="studio-paths"><button type="button" aria-pressed={!custom} onClick={() => setCustom(false)}>Guided calculation</button><button type="button" aria-pressed={custom} onClick={() => { setCustom(true); setName(""); setQuestion(""); setInputs([blankVariable()]); setOutputs([blankVariable()]); }}>Custom · no preset</button></div></>}
        {wizardCategory === "calculation" && !custom ? <label>Preset contract<select defaultValue="" onChange={(event) => { const preset = presets.find((item) => item.id === event.target.value); if (preset) { setName(preset.name); setQuestion(preset.question); setInputs(preset.inputs); setOutputs(preset.outputs); } }}><option value="">No preset</option>{presets.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label> : null}
        </> : null}
        {!embedded || wizardStep === 1 ? <>
        <label>Model name<input required value={name} onChange={(event) => setName(event.target.value)} placeholder="Pipe pressure-loss correlation" /></label>
        <label>Engineering question<textarea required value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="What should this model calculate?" /></label>
        </> : null}
        {embedded && wizardStep === 2 ? <label>What approach should the model use?<textarea required value={approach} onChange={(event) => setApproach(event.target.value)} placeholder="Describe the governing relation or algorithm and its limits." /></label> : null}
        {!embedded || wizardStep === 3 ? <>
        {wizardCategory === "procedure" ? <><VariableForm title="Parameters" nameLabel="Parameter name" rows={parameters} onChange={setParameters} allowEmpty requireDescription /><p>Example: draw_fraction, unit percent, minimum 0, maximum 100. Values are given in this unit and converted by the host.</p></> : wizardCategory === "process_unit" ? <><PortForm title="Inlets" rows={inlets} onChange={setInlets} /><PortForm title="Outlets" rows={outlets} onChange={setOutlets} />
          <VariableForm title="Parameters" rows={parameters} onChange={setParameters} allowEmpty />
          <CultureGenerationField value={generation} onChange={setGeneration} /><SymbolField value={symbol} onChange={setSymbol} refusal={symbolRefusal} />
          {issues.map((issue) => <p className="studio-warning" key={issue}>{issue}</p>)}</>
          : wizardCategory === "geometry" ? <VariableForm title="Inputs" rows={inputs} onChange={setInputs} />
          : <><VariableForm title="Inputs" rows={inputs} onChange={setInputs} /><VariableForm title="Outputs" rows={outputs} onChange={setOutputs} /></>}
        </> : null}
        {embedded && wizardStep === 4 ? <label>What assumptions bound this model?<textarea required value={assumptions} onChange={(event) => setAssumptions(event.target.value)} /></label> : null}
        {embedded && wizardStep === 5 ? <label>How should it be tested?<textarea required value={testIntent} onChange={(event) => setTestIntent(event.target.value)} /></label> : null}
        {embedded && wizardStep === 6 ? <section aria-label="Guided contract review"><h3>Review contract</h3><p><strong>{name}</strong> · {question}</p><p>Approach: {approach}</p><p>Inputs: {wizardCategory === "process_unit" ? inlets.map((port) => port.name).join(", ") : wizardCategory === "procedure" ? parameters.map((item) => `${item.name} [${item.unit}]`).join(", ") : inputs.map((item) => `${item.name} [${item.unit}]`).join(", ")}</p><p>Outputs: {wizardCategory === "process_unit" ? outlets.map((port) => port.name).join(", ") : wizardCategory === "procedure" ? "Process schedule" : wizardCategory === "geometry" ? "One solid" : outputs.map((item) => `${item.name} [${item.unit}]`).join(", ")}</p><p>Assumptions: {assumptions}</p><p>Test intent: {testIntent}</p><p>The draft opens in Studio for source review, Save Draft, Test and Publish. No test or scientific qualification is implied.</p></section> : null}
        {embedded && workspaceId && wizardStep >= 1 ? <WorkshopDraftPanel workspaceId={workspaceId} step={wizardStep} answers={{ category: wizardCategory, step: wizardStep, name, engineering_question: question, approach, assumptions, test_intent: testIntent, inputs, outputs, inlets, outlets, parameters }} accepted={acceptedSource !== null} onAccept={setAcceptedSource} onClear={() => setAcceptedSource(null)} /> : null}
        {embedded ? <div className="studio-actions">{wizardStep > 0 ? <button type="button" onClick={() => setWizardStep((step) => step - 1)}>Back</button> : null}{wizardStep < 6 ? <button key="next" type="button" disabled={wizardStep === 1 && (!name.trim() || !question.trim()) || wizardStep === 2 && !approach.trim() || wizardStep === 3 && !guidedFieldsReady || wizardStep === 4 && !assumptions.trim() || wizardStep === 5 && !testIntent.trim()} onClick={() => setWizardStep((step) => step + 1)}>Next</button> : <button key="create" type="submit" disabled={busy || !guidedFieldsReady}>{wizardCategory === "procedure" ? "Create procedure draft" : "Create draft and Python scaffold"}</button>}</div> : <button type="submit" disabled={busy || issues.length > 0}>{wizardCategory === "procedure" ? "Create procedure draft" : "Create draft and Python scaffold"}</button>}
        {!embedded || wizardStep === 0 ? <><h3>Not yet available</h3><div className="studio-categories">{categories.filter((category) => !category.available).map((category) => <div key={category.id}><button type="button" disabled={!category.available} title={category.reason ?? undefined}>{category.label}</button><small>{category.reason || "Available"}</small></div>)}</div></> : null}
      </form> : model ? <><div className="studio-model-head"><div><h2>{model.name}</h2><p>{model.engineering_question}</p><p className="studio-model-status">{isUnit ? <span>Process unit</span> : isProc ? <span>Procedure</span> : isGeometry ? <span>Geometry</span> : null}<span className={`status-word status-word--${locked ? "available" : "draft"}`}><span aria-hidden="true">{locked ? "●" : "○"}</span> {locked ? "Published" : draftDirty ? "Unsaved draft" : "Draft"}</span><span>{model.versions.length} published</span><span className="status-word status-word--caution" title="Published versions are recorded but scientifically unqualified"><span aria-hidden="true">▲</span> Unqualified</span></p>
          <details className="studio-about"><summary>Details</summary><small>{isUnit ? "Process unit · " : isProc ? "Procedure · " : isGeometry ? "Geometry · " : ""}Model ID {model.id} · {model.versions.length} published · scientifically unqualified</small>{locked ? <p>{newest?.version_label} is published and immutable. Revise opens a new draft from it; existing runs keep their version.</p> : null}</details></div><div className="studio-actions"><button type="button" disabled={busy || locked || !draftDirty} onClick={save}>Save Draft</button><button type="button" disabled={busy || locked || draftDirty || (!isProc && !runtimeReady) || activeCases.length === 0 || (isProc && procCases.some((item) => !item.draft_id || !item.scenario_id))} onClick={test}>Test</button><button type="button" disabled={busy || locked || !(isProc ? canPublishProcedure(model, draftDirty) : canPublishStudioDraft(model, activeCases as StudioCase[], draftDirty, runtimeReady))} onClick={publish}>Publish</button>{locked ? <button type="button" disabled={busy} onClick={revise}>Revise</button> : null}</div></div>
        {isProc ? <SimulationNotice /> : null}
        
        {draftDirty && !locked ? <p className="studio-warning">Unsaved edits. Save Draft to refresh documentation and test the exact source.</p> : null}
        <div className="studio-tabbar"><nav className="studio-tabs" aria-label="Model workspace">{((isProc ? ["contract", "steps", "tests", "versions", "bench"] : isUnit ? ["contract", "code", "documentation", "tests", "versions"] : ["contract", "code", "documentation", "tests", "versions", "run"]) as Tab[]).map((item) => <button key={item} type="button" aria-current={tab === item ? "page" : undefined} onClick={() => setTab(item)}>{tabTitle(item)}</button>)}</nav>{tab !== "code" ? null : <div className="studio-pair-tools" role="group" aria-label="Show">{!isProc ? <><button type="button" aria-pressed={pairView === "explanation"} onClick={() => setPairView("explanation")} {...designTarget("design.workshop.view-explanation")}>Explanation</button><button type="button" aria-pressed={pairView === "split"} onClick={() => setPairView("split")} {...designTarget("design.workshop.view-split")}>Side by side</button><button type="button" aria-pressed={pairView === "code"} onClick={() => setPairView("code")} {...designTarget("design.workshop.view-code")}>Python</button></> : null}{focus && !isProc ? <button type="button" aria-expanded={notesOpen} aria-controls="studio-notes" onClick={() => setNotesOpen((current) => !current)} {...designTarget("design.workshop.notes")}>Contract & notes</button> : null}</div>}</div>
        {tab === "contract" ? <fieldset className="studio-panel" disabled={locked}><label>Model name<input value={name} onChange={(event) => setName(event.target.value)} /></label><label>Engineering question<textarea value={question} onChange={(event) => setQuestion(event.target.value)} /></label>{isProc ? <><VariableForm title="Parameters" nameLabel="Parameter name" rows={parameters} onChange={setParameters} allowEmpty requireDescription /><p className="studio-hash">Changing a parameter or a step changes the source or contract hash and invalidates the test report; Save Draft, then Test again.</p></> : isUnit ? <><PortForm title="Inlets" rows={inlets} onChange={setInlets} /><PortForm title="Outlets" rows={outlets} onChange={setOutlets} /><VariableForm title="Parameters" rows={parameters} onChange={setParameters} allowEmpty />
          <CultureGenerationField value={generation} onChange={setGeneration} /><SymbolField value={symbol} onChange={setSymbol} refusal={symbolRefusal} />{issues.map((issue) => <p className="studio-warning" key={issue}>{issue}</p>)}<p className="studio-hash">Changing the contract changes the contract hash and invalidates the test report; Save Draft, then Run Test again.</p></>
          : isGeometry ? <VariableForm title="Inputs" rows={inputs} onChange={setInputs} />
          : <><VariableForm title="Inputs" rows={inputs} onChange={setInputs} /><VariableForm title="Outputs" rows={outputs} onChange={setOutputs} /></>}</fieldset> : null}
        {tab === "code" ? <div className="studio-panel studio-panel--pair"><div ref={pairRef} className={`studio-math-workspace studio-math-workspace--${isProc ? "code" : pairView}`} style={mathWidth ? { "--studio-math-width": `${mathWidth}px` } as CSSProperties : undefined}>{!isProc ? <section id="studio-math-pane" className="studio-math-pane" aria-label="Derived mathematics"><h3>Explanation</h3>{sourceDirty && !liveMath && !mathError ? <p role="status">Refreshing equations from unsaved source…</p> : null}{sourceDirty && mathError ? <p className="studio-warning" role="alert">{mathError} Saved equations are stale.</p> : null}{sourceDirty && liveMath ? <p className="studio-notice" role="status" title={`source ${liveMath.source_sha256.slice(0, 12)}`}>Preview from unsaved Python. Save Draft before Test.</p> : null}{liveMath ? <DerivedDocumentation derived={liveMath} selectedRange={selectedRange} entry={isGeometry ? "build" : "evaluate"} selectedLine={activeSourceLine} onSelectLine={(line) => { setActiveSourceLine(line); setJumpToSourceLine(line); setLinkedLine(line); }} /> : !sourceDirty ? <DerivedDocumentation derived={model.documentation.derived} selectedRange={selectedRange} entry={isGeometry ? "build" : "evaluate"} selectedLine={activeSourceLine} onSelectLine={(line) => { setActiveSourceLine(line); setJumpToSourceLine(line); setLinkedLine(line); }} /> : null} </section> : null}{!isProc && pairView === "split" ? <DesignSplitter label="Resize explanation and code" controls="studio-math-pane" bounds={mathBounds} side="start" width={mathWidth ?? mathBounds.fallback} onResize={setMathWidth} onCommit={setMathWidth} onReset={() => setMathWidth(null)} target="design.workshop.splitter" /> : null}<section className="studio-code-pane" aria-label="Python source"><h3>Python</h3><PythonEditor value={source} onChange={setSource} disabled={locked} focusLine={jumpToSourceLine} linkedLine={linkedLine} onCursorLineChange={(line) => { setActiveSourceLine(line); setJumpToSourceLine(null); }} onSelectionLinesChange={(range) => { setSelectedRange(range); setLinkedLine((current) => current !== null && range.from === current && range.to === current ? current : null); }} /><details className="studio-about"><summary>Source details</summary><small>Python is the executable source. Equations are read-only and derived from that source; the source is never executed to render them.</small><small className="studio-hash">Saved source SHA-256: {model.source_sha256}</small>{!sourceDirty ? <small className="studio-hash">Equations derived from saved source {model.documentation.derived.source_sha256.slice(0, 12)}.</small> : null}</details></section></div>{!isProc && (!focus || notesOpen) ? <section id="studio-notes" className="studio-math-details" aria-label="Contract, units and notes"><h3>Contract, units and notes</h3>{isUnit ? <div className="studio-contract-tables"><section><h4>Inlets</h4>{inlets.map((port) => <p key={port.name}>{port.label || port.name} ({port.name})</p>)}</section><section><h4>Outlets</h4>{outlets.map((port) => <p key={port.name}>{port.label || port.name} ({port.name})</p>)}</section><section><h4>Parameters</h4>{parameters.map((row) => <p key={row.name}>{row.label || row.name} ({row.name}) [{row.unit}]</p>)}</section></div> : <div className="studio-contract-tables"><section><h4>Inputs</h4>{inputs.map((row) => <p key={row.name}>{row.label || row.name} ({row.name}) [{row.unit}]</p>)}</section><section><h4>Output</h4>{isGeometry ? <p>One build123d solid. Dimensions are in millimetres.</p> : outputs.map((row) => <p key={row.name}>{row.label || row.name} ({row.name}) [{row.unit}]</p>)}</section></div>}{contractDirty ? <p className="studio-warning">Unsaved contract changes; Save Draft before Test.</p> : null}{model.annotations_stale || sourceDirty ? <p className="studio-warning">Annotations refer to an older saved source.</p> : null}{model.last_test_report && (sourceDirty || model.last_test_report.source_sha256 !== model.source_sha256) ? <p className="studio-warning">The displayed test report refers to an older source. Save Draft and Test again.</p> : null}<label>Annotations<textarea disabled={locked} value={annotations} onChange={(event) => setAnnotations(event.target.value)} /></label><label>Assumptions<textarea disabled={locked} value={assumptions} onChange={(event) => setAssumptions(event.target.value)} /></label></section> : null}</div> : null}
        {tab === "documentation" ? <div className="studio-panel"><h3>Derived equations</h3><p>Read-only equations are parsed from saved Python; source is never executed to render them.</p><DerivedDocumentation derived={model.documentation.derived} entry={isGeometry ? "build" : "evaluate"} />
          <h3>Contract tables</h3>{isUnit ? <ContractTables contract={model.documentation} /> : <div className="studio-contract-tables"><section><h4>Inputs</h4>{model.documentation?.inputs.map((row) => <p key={row.name}>{row.label} ({row.name}) [{row.unit}] · {row.description}</p>)}</section><section><h4>Outputs</h4>{(model.documentation?.outputs ?? []).map((row) => <p key={row.name}>{row.label} ({row.name}) [{row.unit}] · {row.description}</p>)}</section></div>}
          {model.annotations_stale || sourceDirty ? <p className="studio-warning">Annotations were written against an older source hash.</p> : null}<label>Annotations<textarea disabled={locked} value={annotations} onChange={(event) => setAnnotations(event.target.value)} /></label><label>Assumptions<textarea disabled={locked} value={assumptions} onChange={(event) => setAssumptions(event.target.value)} /></label></div> : null}
        {tab === "tests" && isUnit ? <fieldset className="studio-panel" disabled={locked}><h3>Test cases</h3><p>Each case gives one inlet stream per port (kg/s, K, Pa, mass fractions of flowsheet compounds, optional culture fields) and a value for every parameter in its declared unit. Expected outlet values are optional. Every call is host-checked for outlet names, finite and positive state, mass fractions summing to 1, and mass, compound and culture conservation.</p>
          <UnitCases contract={{ inlets, outlets, parameters }} cases={unitCases} compounds={compounds} onChange={setUnitCases} />
          <button type="button" disabled={busy || locked || draftDirty || !runtimeReady || !unitCases.length} onClick={test}>Run Test</button>
          {draftDirty ? <p className="studio-warning">Save Draft before testing: a test report is bound to the saved source and contract.</p> : null}
          {model.last_test_report ? <UnitReport report={model.last_test_report} /> : null}</fieldset> : null}
        {tab === "tests" && isGeometry ? <fieldset className="studio-panel" disabled={locked}><h3>Builds</h3><p>Each build runs build with the given inputs in the isolated runtime, then checks that the result is one valid solid and that any declared bounds hold. Passing builds show a preview and their STEP, STL and GLB files.</p>
          <GeometryCases inputs={model.inputs} cases={geometryCases} onChange={setGeometryCases} />
          <button type="button" disabled={busy || locked || draftDirty || !runtimeReady || !geometryCases.length} onClick={test}>Run Test</button>
          {busy ? <p className="studio-notice" role="status">Building in the isolated runtime; a build may take up to 120 s.</p> : null}
          {draftDirty ? <p className="studio-warning">Save Draft before testing: a test report is bound to the saved source and contract.</p> : null}
          {model.last_test_report ? <GeometryReport report={model.last_test_report} url={(previewId, file) => geometryPreviewUrl(model.workspace_id, model.id, previewId, file)} /> : null}</fieldset> : null}
        {tab === "tests" && !isUnit && !isGeometry && !isProc ? <fieldset className="studio-panel" disabled={locked}><h3>Test cases</h3><p>Enter values in declared units. Expected outputs are optional; each test checks that every output exists and is finite.</p>{cases.map((item, index) => <fieldset className="studio-case" key={index}><legend>Case {index + 1}</legend><NumberMapEditor title="Inputs" variables={model.inputs} values={item.inputs} onChange={(values) => updateCase(index, { inputs: values })} /><NumberMapEditor title="Expected outputs (optional)" variables={model.outputs} values={item.expected_outputs ?? {}} onChange={(values) => updateCase(index, { expected_outputs: values })} /><label>Tolerance<NumericInput min={0} value={item.tolerance ?? 1e-6} onValueChange={(text) => updateCase(index, { tolerance: (numericOrUndefined(text) ?? 0) })} /></label><button type="button" onClick={() => setCases(cases.filter((_, i) => i !== index))}>Remove case</button></fieldset>)}<button type="button" onClick={() => setCases([...cases, emptyCase()])}>+ Add test case</button><button type="button" disabled={busy || locked || draftDirty || !runtimeReady || !cases.length} onClick={test}>Run Test</button>
          {model.last_test_report ? <section className="studio-report"><h4>{model.last_test_report.passed ? "Passing report" : "Failed report"}</h4><p>Source {model.last_test_report.source_sha256.slice(0, 12)} · Contract {model.last_test_report.contract_sha256.slice(0, 12)}</p>{model.last_test_report.cases.map((result, i) => <div key={i}><strong>Case {i + 1}: {result.passed ? "Pass" : "Fail"}</strong> · {result.classification}{result.errors.length ? <ul>{result.errors.map((message, n) => <li key={n}>{message}</li>)}</ul> : null}{result.outputs ? <pre>{JSON.stringify(result.outputs, null, 2)}</pre> : null}</div>)}</section> : null}</fieldset> : null}
        {tab === "steps" && isProc ? <><label>Suggest tank and unit tags from the Process draft<select aria-label="Tag source draft" value={tagDraftId} onChange={(event) => setTagDraftId(event.target.value)}><option value="">No draft chosen (type tags)</option>{benchDrafts.map((item) => <option key={item.draft_id} value={item.draft_id}>{item.name}</option>)}</select></label>
          <StepEditor steps={steps} parameters={parameters} disabled={locked} tanks={benchDrafts.find((item) => item.draft_id === tagDraftId)?.tanks ?? []} units={(benchDrafts.find((item) => item.draft_id === tagDraftId)?.units ?? []).map((item) => item.tag)} onChange={setSteps} /></> : null}
        {tab === "tests" && isProc ? <fieldset className="studio-panel" disabled={locked}><h3>Test cases</h3><SimulationNotice /><p>A case gives parameter values and a target Process draft with a scenario. Test compiles the procedure and runs the host checks on a scratch copy; the draft is not changed. Tick simulate to also run it and check expected values. Every refusal keeps the host's code.</p>
          <ProcedureCases cases={procCases} parameters={parameters} drafts={benchDrafts} onChange={setProcCases} />
          {procCases.flatMap((item) => parameterValuesIssues(parameters, item.parameter_values).map((issue) => <p className="studio-warning" key={`${item.name}${issue}`}>{item.name}: {issue}</p>))}
          <button type="button" disabled={busy || locked || draftDirty || !procCases.length || procCases.some((item) => !item.draft_id || !item.scenario_id)} onClick={test}>Run Test</button>
          {draftDirty ? <p className="studio-warning">Save Draft before testing: a test report is bound to the saved steps and parameters.</p> : null}
          {isProcedureReport(model.last_test_report) ? <ProcedureReportView report={model.last_test_report} /> : null}</fieldset> : null}
        {tab === "bench" && isProc && workspaceId ? <ProcedureBench workspaceId={workspaceId} model={model} /> : null}
        {tab === "versions" ? <div className="studio-panel"><h3>Published versions</h3><p>Published versions are immutable and scientifically unqualified. Revise creates a new draft.</p>{model.versions.length ? model.versions.map((version) => <article key={version.id}><strong>{version.version_label}</strong><span> · {new Date(version.created_at).toLocaleString()}</span><p>Source {version.source_sha256.slice(0, 12)} · Contract {version.contract_sha256.slice(0, 12)}</p>{isProc ? <small>Apply it from the Test bench.</small> : isUnit ? <small>Place it from the Process palette (Studio units).</small> : <button type="button" onClick={() => selectVersion(model.id, version.id)}>{isGeometry ? "Select for generation" : "Select for calculation"}</button>}</article>) : <p>No published versions.</p>}{isProc && model.versions.length ? <button type="button" onClick={() => setTab("bench")}>Open Test bench</button> : null}{isUnit && model.versions.length ? <button type="button" onClick={() => navigate("/design/process")}>Open Process</button> : null}</div> : null}
        {tab === "run" ? <div className="studio-panel"><h3>{isGeometry ? "Generate from a published version" : "Run a published version"}</h3>{model.versions.length ? <>
          {isGeometry ? <p>{GEOMETRY_NOTICE} Each generation records the version, source hash and inputs; its files appear under the run below.</p> : null}
          <p>This is the existing calculation surface. Choose the published version as the model contract, bind each input to a value or to a project Parameter, then Run. Runs keep their inputs, version and provenance.</p>
          {!runtimeReady ? <p className="studio-warning">Run is refused while the Studio runtime is unavailable.</p> : null}
          <header className="studio-runs-head"><h3>Runs of this model</h3><button type="button" onClick={refreshRun}>Refresh</button></header>
          {runs.length ? runs.map((run) => { const shape = isGeometry ? geometryRunOutput(run.output_payload) : null; const failure = run.status !== "succeeded" ? runFailure(run.output_payload) : null; return <article key={run.id}><strong>{run.run_label || "Unnamed run"} · {versionLabels.get(run.model_version_id ?? "") ?? "unknown version"} · {run.status}</strong><span>{new Date(run.created_at).toLocaleString()}</span>{run.output_payload && run.status === "succeeded" && !isGeometry ? <p>{runOutputs(run.output_payload)}</p> : null}
            {failure ? <p className="studio-warning">{failure}</p> : null}
            {shape && workspaceId ? <><p>Source {model.versions.find((version) => version.id === run.model_version_id)?.source_sha256.slice(0, 12) ?? "unknown"} · Inputs: {Object.entries(JSON.parse(run.input_payload ?? "{}") as Record<string, { value: number; unit: string; source_parameter_id?: string }>).map(([key, item]) => `${key} = ${item.value} ${item.unit}${item.source_parameter_id ? " (Parameter)" : ""}`).join(" · ")}</p><GeometryFacts facts={shape.geometry} /><GeometryArtifacts artifacts={shape.artifacts} url={(file) => geometryRunArtifactUrl(workspaceId, run.id, file)} /></> : null}
            {run.stale ? <p className="studio-warning" role="status">Stale: a bound Parameter changed after this run. Refresh, select the Parameter again and Run to use its current value.</p> : null}
            {run.superseded ? <p className="studio-warning" role="status">Superseded: a newer published version exists. This run is unchanged and stays bound to its version.</p> : null}</article>; }) : <p>No runs of this model yet.</p>}
          <EngineeringPropertiesPanel controller={engineeringProperties} navigate={navigate} />
          {engineeringProperties.runResult?.output?.outputs ? <section className="studio-report"><h4>Latest result</h4>{Object.entries(engineeringProperties.runResult.output.outputs).map(([name, item]) => <p key={name}>{name} = {String(item.value)} {item.unit}</p>)}<p>Outputs are recorded as proposed Parameters, never promoted automatically.</p></section> : null}
        </> : <p>Publish a version first. Only published versions can be run.</p>}</div> : null}
      </> : <div className="studio-empty"><h2>No model selected</h2><button type="button" className="primary-button" onClick={() => setWizard(true)}>New model</button></div>}
    </section></div>
  </main>;
}
