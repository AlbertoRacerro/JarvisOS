import { Suspense, lazy, useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import AppLink, { type Navigate } from "../app/AppLink";
import ExampleBadge from "../app/ExampleBadge";
import { designTarget, type DesignTargetId } from "../app/designTargets";
import { resolveRoute } from "../app/routes";
import { C1_EXAMPLE_SLUG, caseStudyLabel, caseStudyName, homeProject, isExampleWorkspace } from "../app/exampleWorkspace";
import { publishDesignSurface, type DesignPanelState } from "../app/workspaceActionSurface";
import { bluecadArtifactContentUrl, getBluecadCandidateAggregate, listBluecadCandidates, listWorkspaces,
  type BluecadCandidate, type Workspace, type BluecadCandidateAggregateRead } from "../api/client";
import {
  activateDesignVersion, attachDesignGeometry, attachDesignModel, COMPONENT_NODE_OF_CATEGORY, createDesignComponent, createDesignVersion, deepenDesignGeometry,
  editDesignComponent, getComponentReducedModels, getProcessGeometryMapping, listComponentCfdRuns, listComponentStudyTemplates,
  listDesignComponents, listDesignVersions, openDesignUnit,
  type CfdRun, type DesignComponent, type DesignVersion, type DesignVersionSet, type ProcessGeometryMapping
} from "../api/designProject";
import { listStudioLibrary, type StudioLibraryEntry } from "../api/studio";
import ProcessDraftEditor from "../stages/ProcessDraftEditor";
import Studio from "./Studio";
import type { EngineeringPropertiesController } from "../components/engineering/EngineeringProperties";
import { getDraft, type DraftObject } from "../api/processDraft";
import ReducedModelsPanel from "./ReducedModelsPanel";
import { CfdResultBlocks, CfdRunPicker, type CfdRunsRead } from "./CfdResultsCompare";
import CfdMeshPanel from "./CfdMeshPanel";
import StudyTemplatesPanel from "./StudyTemplatesPanel";
import DesignFeatureInspector from "./DesignFeatureInspector";
import BluecadGlbViewer from "../components/BluecadGlbViewer";
import NumericInput from "../components/ui/NumericInput";
import DesignComponentTree, { componentNodeStatus, type DesignNode } from "../components/design/DesignComponentTree";
import { cfdNodeStatus, type CfdOwnerRead, type NodeBadge } from "../components/design/designNodeStatus";
import { DesignStatus, Empty } from "../components/design/DesignStatus";
import { readableKey, readableProse } from "../components/design/designText";
import { libraryLabel } from "../components/design/libraryLabel";
import DesignOpticsPanel from "./DesignOpticsPanel";
import DesignDynamicPanel from "./DesignDynamicPanel";
import DesignSplitter, { clampDesignWidth, type DesignPanelBounds } from "../components/design/DesignSplitter";
import { designViewHref, readDesignView, STAGE_NODE, STAGES, type DesignView, type Stage } from "./designView";
import "./designProject.css";

const LEFT_BOUNDS: DesignPanelBounds = { min: 200, max: 480, fallback: 270 };
const RIGHT_BOUNDS: DesignPanelBounds = { min: 260, max: 640, fallback: 360 };
const WIDTH_KEY = "jarvisos:design-panels:v1";
function readWidths(): { left: number; right: number } {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(WIDTH_KEY) ?? "{}") as Record<string, unknown>;
    return { left: clampDesignWidth(LEFT_BOUNDS, typeof parsed.left === "number" ? parsed.left : LEFT_BOUNDS.fallback),
      right: clampDesignWidth(RIGHT_BOUNDS, typeof parsed.right === "number" ? parsed.right : RIGHT_BOUNDS.fallback) };
  } catch { return { left: LEFT_BOUNDS.fallback, right: RIGHT_BOUNDS.fallback }; }
}
const STAGE_TARGET: Record<Stage, DesignTargetId> = {
  Plant: "design.stage.plant", Dynamic: "design.stage.dynamic", CAD: "design.stage.cad", "Motion/Mechanics": "design.stage.mechanics",
  CFD: "design.stage.cfd", Optics: "design.stage.optics", "Reduced model": "design.stage.reduced-model"
};
const ATTACH_MODEL: DesignTargetId = "design.node.attach-model";
const EnvironmentPanel = lazy(() => import("../stages/EnvironmentPanel"));
const cfdPanelState = (read: CfdOwnerRead, count: (ready: Extract<CfdOwnerRead, { state: "ready" }>) => number): DesignPanelState =>
  read.state === "ready" ? (count(read) ? "available" : "empty") : read.state === "loading" ? "loading" : "unavailable";

export default function DesignProject({ projectId, onProjectChange, search, navigate, engineeringProperties }: {
  projectId: string | null; onProjectChange(id: string): void; search: string; navigate: Navigate; engineeringProperties: EngineeringPropertiesController;
}) {
  const view = readDesignView(search);
  const { stage, componentId: selectedId, node: selectedNode } = view;
  // Every change of view is a navigation: a pushed entry for the operator's own moves, a replaced one for corrections.
  // A unit opened after an await must not pull the operator back if they have left Design meanwhile.
  const showView = (next: DesignView, replace = false) => {
    if (resolveRoute(window.location.pathname).route.id !== "design-project") return;
    const href = designViewHref(next, window.location.search);
    if (!replace) navigate(href);
    else if (href !== `${window.location.pathname}${window.location.search}`) navigate(href, { replace: true });
  };
  const [projectName, setProjectName] = useState("");
  const [projectWorkspace, setProjectWorkspace] = useState<Workspace | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [cfdRead, setCfdRead] = useState<CfdOwnerRead>({ state: "loading" });
  // The selected component's CFD runs, and the ones the operator checked to compare on Results (null: the default).
  const [cfdRuns, setCfdRuns] = useState<CfdRunsRead>(null);
  const [compareIds, setCompareIds] = useState<string[] | null>(null);
  const [environmentOpen, setEnvironmentOpen] = useState(false);
  const [versions, setVersions] = useState<DesignVersionSet | null>(null);
  // While a case study (an example workspace) is open, the home project's versions it can return to.
  const [homeVersions, setHomeVersions] = useState<DesignVersionSet | null>(null);
  const [components, setComponents] = useState<DesignComponent[]>([]);
  const [library, setLibrary] = useState<StudioLibraryEntry[]>([]);
  const [sourceUnit, setSourceUnit] = useState<DraftObject | null>(null);
  const [sourceRevision, setSourceRevision] = useState<string | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [sourceNonce, setSourceNonce] = useState(0);
  const [geometryCandidates, setGeometryCandidates] = useState<BluecadCandidate[]>([]);
  const [geometrySelection, setGeometrySelection] = useState("");
  const [geometryAggregate, setGeometryAggregate] = useState<BluecadCandidateAggregateRead | null>(null);
  const [geometryMapping, setGeometryMapping] = useState<ProcessGeometryMapping | null>(null);
  const [wallMm, setWallMm] = useState("3");
  const [modelSelection, setModelSelection] = useState("");
  // The unit selected on the Plant flowsheet; CAD, CFD and Reduced model open it as the component to work on.
  const [plantUnit, setPlantUnit] = useState<{ draftId: string; revision: string; unit: DraftObject } | null>(null);
  const [workshop, setWorkshop] = useState<{ modelId: string | null; key: number } | null>(null);
  // Results open with the inspector shown: it holds the runs to compare.
  const [fieldExpanded, setFieldExpanded] = useState(false);
  const [newVersion, setNewVersion] = useState("");
  const [emptyVersion, setEmptyVersion] = useState(false);
  const [newComponent, setNewComponent] = useState("");
  const [editName, setEditName] = useState("");
  const [editParent, setEditParent] = useState("");
  const [widths, setWidths] = useState(readWidths);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldSlice, setFieldSlice] = useState<{ key: string; state: DesignPanelState } | null>(null);

  const reload = useCallback(async () => {
    if (!projectId) return;
    const next = await listDesignVersions(projectId);
    const active = next.versions.find((item) => item.id === next.active_version_id);
    if (!active) throw new Error("The active Design version is unavailable.");
    const [tree, published] = await Promise.all([
      listDesignComponents(projectId, active.id), listStudioLibrary(active.data_workspace_id)
    ]);
    setVersions(next); setComponents(tree); setLibrary(published);
  }, [projectId]);

  useEffect(() => {
    let live = true;
    setVersions(null); setHomeVersions(null); setComponents([]); setLibrary([]); setError(null); setProjectWorkspace(null);
    if (projectId) {
      void Promise.all([listWorkspaces(), listDesignVersions(projectId)]).then(async ([workspaces, next]) => {
        const active = next.versions.find((item) => item.id === next.active_version_id);
        if (!active) throw new Error("The active Design version is unavailable.");
        const project = workspaces.find((item) => item.id === projectId);
        const home = isExampleWorkspace(project) ? homeProject(workspaces) : undefined;
        // The home's versions only feed the way back, so the case study still opens if they cannot be read.
        const [tree, published, homeSet] = await Promise.all([
          listDesignComponents(projectId, active.id), listStudioLibrary(active.data_workspace_id),
          home ? listDesignVersions(home.id).catch(() => null) : null
        ]);
        if (!live) return;
        setProjectName(project && home ? `${home.name} · case study ${caseStudyName(project)}` : project?.name ?? "Project");
        setProjectWorkspace(project ?? null); setWorkspaces(workspaces);
        setVersions(next); setHomeVersions(homeSet); setComponents(tree); setLibrary(published);
      }).catch((cause: Error) => { if (live) setError(cause.message); });
    }
    return () => { live = false; };
  }, [projectId]);

  const perform = async (action: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await action(); await reload(); } catch (cause) { setError(cause instanceof Error ? cause.message : "Design operation failed."); }
    finally { setBusy(false); }
  };
  const active = versions?.versions.find((item) => item.id === versions.active_version_id);
  const plantDraftIds = [...new Set(components.map((item) => item.source_draft_id).filter((id): id is string => Boolean(id)))];
  const plantDraftId = plantDraftIds.length === 1 ? plantDraftIds[0] : undefined;
  useEffect(() => { setWorkshop(null); }, [active?.id]);
  const selected = components.find((item) => item.id === selectedId);
  // Keep the URL truthful: a component that is not in the loaded version (another project's, a deleted one) and
  // malformed values are corrected in place, never as a new entry.
  useEffect(() => {
    const known = !selectedId || versions === null || components.some((item) => item.id === selectedId);
    showView(known ? view : { ...view, componentId: null }, true);
  }, [search, versions, components]);
  // A view reached by Back/Forward or a link starts as a fresh selection would: inspector forms follow the component.
  useEffect(() => {
    setGeometrySelection(""); setModelSelection("");
    setEditName(selected?.name ?? ""); setEditParent(selected?.parent_id ?? "");
  }, [selected?.id]);
  useEffect(() => { setWorkshop(null); }, [search]);
  // Spec 229: right after a project switch the version and component still belong to the previous project for one
  // render; reads that combine them with the new project id would 404, so they wait until all three agree.
  const sameProject = Boolean(projectId && active?.project_id === projectId && selected?.project_id === projectId);
  useEffect(() => {
    let live = true;
    setGeometryCandidates([]); setGeometrySelection("");
    if (active) void listBluecadCandidates(active.data_workspace_id).then((items) => {
      if (live) setGeometryCandidates(items.filter((item) => item.status === "valid"));
    }).catch((cause: unknown) => { if (live) setError(cause instanceof Error ? cause.message : "BlueCad candidates are unavailable."); });
    return () => { live = false; };
  }, [active?.data_workspace_id, active?.id, selected?.geometry_candidate_id]);
  useEffect(() => {
    let live = true;
    setGeometryMapping(null);
    if (projectId && active && sameProject && selected?.source_unit_id && selectedNode === "Geometry") {
      void getProcessGeometryMapping(projectId, active.id, selected.id).then((item) => {
        if (live) setGeometryMapping(item);
      }).catch((cause: unknown) => { if (live) setError(cause instanceof Error ? cause.message : "Process geometry mapping is unavailable."); });
    }
    return () => { live = false; };
  }, [projectId, active?.id, sameProject, selected?.id, selected?.source_unit_id, selected?.revision, selectedNode, sourceRevision]);
  useEffect(() => {
    let live = true;
    setGeometryAggregate(null);
    if (active && selected?.geometry_candidate_id) {
      void getBluecadCandidateAggregate(selected.geometry_workspace_id ?? active.data_workspace_id, selected.geometry_candidate_id).then((item) => {
        if (live) setGeometryAggregate(item);
      }).catch((cause: unknown) => { if (live) setError(cause instanceof Error ? cause.message : "Attached geometry is unavailable."); });
    }
    return () => { live = false; };
  }, [active?.data_workspace_id, selected?.geometry_candidate_id, selected?.geometry_workspace_id]);
  useEffect(() => {
    let live = true;
    setSourceUnit(null); setSourceRevision(null); setSourceError(null);
    if (active && selected?.source_draft_id && selected.source_unit_id) {
      void getDraft(active.data_workspace_id, selected.source_draft_id).then((process) => {
        if (!live) return;
        setSourceRevision(process.revision);
        const unit = process.objects.find((item) => item.id === selected.source_unit_id && item.kind === "unit");
        if (unit) setSourceUnit(unit);
        else setSourceError("The source Process unit is no longer in this draft.");
      }).catch((cause: unknown) => {
        if (live) setSourceError(cause instanceof Error ? cause.message : "Process source is unavailable.");
      });
    }
    return () => { live = false; };
  }, [active?.data_workspace_id, selected?.source_draft_id, selected?.source_unit_id, sourceNonce]);
  // Spec 229: tree badges for CFD-owned nodes read the same owners as the stage panels. They are re-read on every node
  // change (a panel may have started a run or pinned a model); only a different component resets them to "checking…".
  const cfdReadOf = useRef<string | null>(null);
  const currentCfdKey = projectId && active && selected && sameProject ? `${active.id}:${selected.id}` : null;
  if (cfdReadOf.current !== currentCfdKey && (cfdRuns !== null || compareIds !== null)) {
    setCfdRuns(null);
    setCompareIds(null);
  }
  useEffect(() => {
    let live = true;
    const key = currentCfdKey;
    if (cfdReadOf.current !== key) { cfdReadOf.current = key; setCfdRead({ state: "loading" }); setCfdRuns(null); setCompareIds(null); }
    if (projectId && active && selected && sameProject) {
      void Promise.all([listComponentStudyTemplates(projectId, active.id, selected.id), listComponentCfdRuns(projectId, active.id, selected.id),
        getComponentReducedModels(projectId, active.id, selected.id)])
        .then(([templates, runs, reduced]) => { if (live) { setCfdRead({ state: "ready", templates: templates.length, runs, reduced }); setCfdRuns(runs); } })
        .catch(() => { if (live) { setCfdRead({ state: "unavailable" }); setCfdRuns("unavailable"); } });
    }
    return () => { live = false; };
  }, [currentCfdKey, projectId, active?.id, sameProject, selected?.id, selected?.revision, selectedNode]);
  // A run started or finished in the Study templates fold shows up in the comparison and the badges without leaving Results.
  const selectedComponentId = selected?.id;
  const onRunsChange = useCallback((runs: CfdRun[]) => {
    if (!selectedComponentId || runs.some((run) => run.component_id !== selectedComponentId)) return;
    setCfdRuns(runs);
    setCfdRead((read) => read.state === "ready" ? { ...read, runs } : read);
  }, [selectedComponentId]);
  const nodeStatus = useCallback((node: DesignNode) => cfdNodeStatus(cfdRead, node), [cfdRead]);
  const nodeModels = selected?.models.filter((item) => item.node === selectedNode) ?? [];
  const libraryNames = new Map(library.map((item) => [item.id, `${item.name} · ${item.version_label}`]));
  const compatibleModels = library.filter((item) => COMPONENT_NODE_OF_CATEGORY[item.category ?? "calculation"] === selectedNode
    && !nodeModels.some((model) => model.model_version_id === item.id));
  const attachShown = !!active && !!selected && (selectedNode === "Physics" || selectedNode === "Geometry");
  const attachDisabled = !attachShown ? null : busy ? "busy" : modelSelection ? null : compatibleModels.length ? "no_model_chosen" : "no_compatible_model";
  const fieldSliceKey = active && selected ? `${projectId}:${active.id}:${selected.id}` : null;
  const reportFieldSlice = useCallback((state: DesignPanelState) => {
    if (fieldSliceKey) setFieldSlice({ key: fieldSliceKey, state });
  }, [fieldSliceKey]);
  useEffect(() => {
    publishDesignSurface(projectId ? {
      route_id: "design-project", workspace_id: projectId, example: isExampleWorkspace(projectWorkspace),
      version_id: active?.id ?? null, version_label: active?.label ?? null, stage: workshop ? "Workshop" : stage,
      component_id: selected?.id ?? null, component_name: selected?.name ?? null, node: selected ? selectedNode : null,
      panels: {
        components: versions === null ? "loading" : components.length ? "available" : "empty",
        plant_draft: versions === null ? "loading" : plantDraftId ? "available" : plantDraftIds.length > 1 ? "unavailable" : "empty",
        geometry: !selected ? "not_checked" : selected.geometry_candidate_id ? "available" : "empty",
        cfd_runs: !selected ? "not_checked" : cfdPanelState(cfdRead, (read) => read.runs.length),
        cfd_fields: !selected ? "not_checked" : cfdPanelState(cfdRead, (read) => read.runs.filter((run) => run.state === "finished").length),
        reduced_models: !selected ? "not_checked" : cfdPanelState(cfdRead, (read) => read.reduced.length),
        cfd_field_slice: selected && selectedNode === "Results" ? (fieldSlice?.key === fieldSliceKey ? fieldSlice.state : "loading") : "not_checked"
      },
      disabled: attachDisabled ? { [ATTACH_MODEL]: attachDisabled } : {}
    } : null);
  }, [projectId, projectWorkspace, active?.id, active?.label, stage, workshop, selected, selectedNode, versions, components.length, plantDraftId, plantDraftIds.length, cfdRead, fieldSlice, fieldSliceKey, attachDisabled]);
  useEffect(() => () => publishDesignSurface(null), []);
  const parameterEntries = Object.entries(sourceUnit?.params ?? {});
  const deepenable = geometryMapping?.supported ? geometryMapping : null;
  // The inspector heading carries the same status word as the node's tree badge; Parameters also reflect the Process read.
  const inspectorBadge: NodeBadge | null = !selected ? null
    : selectedNode === "Parameters" && selected.source_unit_id
      ? sourceError ? { state: "failed", detail: sourceError } : !sourceUnit ? { state: "checking" }
        : parameterEntries.length ? { state: "ready", detail: "From Process" } : { state: "empty" }
      : cfdNodeStatus(cfdRead, selectedNode) ?? componentNodeStatus(selected, selectedNode);
  const descendants = new Set<string>();
  if (selected) {
    const visit = (id: string) => {
      if (descendants.has(id)) return;
      descendants.add(id);
      components.filter((item) => item.parent_id === id).forEach((item) => visit(item.id));
    };
    visit(selected.id);
  }
  const commitWidths = (next: { left: number; right: number }) => {
    setWidths(next);
    try { window.localStorage.setItem(WIDTH_KEY, JSON.stringify(next)); } catch { /* best-effort visual preference */ }
  };
  const selectNode = (node: DesignNode) => showView({ componentId: selectedId, node,
    stage: node === "Geometry" ? "CAD" : node === "Reduced model" ? "Reduced model" : ["Physics", "Mesh", "Results"].includes(node) ? "CFD" : "Plant" });
  const selectComponent = (item: DesignComponent) => showView({ stage, componentId: item.id, node: stage === "CFD" ? "Results" : "Parameters" });
  // Opening a unit reuses its component when one already exists for this version; it is a new entry, so Back returns to Plant.
  const openUnit = async (draftId: string, revision: string, unit: DraftObject, target: "CAD" | "CFD" | "Reduced model" = "CAD") => {
    if (!projectId || !active) return;
    setBusy(true); setError(null);
    try {
      const component = await openDesignUnit(projectId, active.id, draftId, revision, unit.id);
      await reload();
      showView({ stage: target, componentId: component.id, node: STAGE_NODE[target] });
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not open the Process unit."); }
    finally { setBusy(false); }
  };

  if (!projectId) return <p>Select a project to open Design.</p>;
  const plantMode = stage === "Plant" && !selected;
  const opticsMode = stage === "Optics";
  const dynamicMode = stage === "Dynamic";
  // The older surfaces read the app workspace, which is this project's first version's data context.
  const firstVersion = active?.id === projectId;
  const c1Example = workspaces.find((item) => item.slug === C1_EXAMPLE_SLUG);
  const offerExample = versions !== null && !components.length && c1Example && c1Example.id !== projectId ? c1Example : null;
  // Examples are case studies of the home project: one picker lists the home's versions and the case studies. A case
  // study stays its own workspace underneath, so picking one (or a home version from one) switches the app project.
  // Values carry their kind because a project's first version shares the project's id.
  const caseStudies = workspaces.filter((item) => isExampleWorkspace(item));
  const inCaseStudy = isExampleWorkspace(projectWorkspace);
  const home = inCaseStudy ? homeProject(workspaces) : projectWorkspace;
  const homeSet = inCaseStudy ? homeVersions : versions;
  const versionLabel = (item: DesignVersion) => `${item.label}${item.starts_empty ? " · empty start" : item.source_mode === "fork" ? " · fork" : ""}`;
  // A project switch lands on Plant: the component in the URL belongs to the project left behind.
  const switchProject = (id: string) => {
    setWorkshop(null); onProjectChange(id);
    showView({ stage: "Plant", componentId: null, node: STAGE_NODE.Plant }, true);
  };
  const pickVersion = async (value: string) => {
    const [kind, id] = [value.slice(0, value.indexOf(":")), value.slice(value.indexOf(":") + 1)];
    if (kind === "case") { switchProject(id); return; }
    if (kind === "version" && versions) { await perform(() => activateDesignVersion(projectId, id, versions.active_version_id)); return; }
    if (kind !== "home" || !home || !homeVersions) return;
    setBusy(true); setError(null);
    try {
      if (id !== homeVersions.active_version_id) await activateDesignVersion(home.id, id, homeVersions.active_version_id);
      switchProject(home.id);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not open the version."); }
    finally { setBusy(false); }
  };
  const glbArtifactId = active && selected?.geometry_candidate_id && geometryAggregate?.candidate.id === selected.geometry_candidate_id
    ? geometryAggregate.candidate.glb_artifact_id : null;
  const glbUrl = active && selected && glbArtifactId ? bluecadArtifactContentUrl(selected.geometry_workspace_id ?? active.data_workspace_id, glbArtifactId) : null;
  const currentProject = projectWorkspace?.id === projectId;
  const currentVersion = active?.project_id === projectId;
  const currentComponent = currentVersion && selected?.project_id === projectId ? selected : null;
  const location = `${currentProject ? projectName : "Project loading"} · ${currentVersion ? active.label : "Version loading"} · ${stage}${currentComponent ? ` · ${currentComponent.name} / ${selectedNode}` : selectedId ? " · Component loading" : " · No component selected"}`;
  const stageHelp = error ? "Design data could not be loaded. Check the error above and try again."
    : !currentVersion ? "Loading the active version and its Design state…"
    : stage === "Plant" ? "Plant shows the Process flowsheet. Select a unit there, then choose CAD, CFD or Reduced model."
    : stage === "Dynamic" ? "Dynamic works with this version's Process drafts. Choose a draft in this stage to inspect its dynamic setup."
    : stage === "Optics" ? "Optics shows available Process-unit optics inputs and runs for this version. Check the panel below for availability."
    : stage === "Motion/Mechanics" ? "Motion/Mechanics is a placeholder; calculations are not available here yet. Use another Design stage for available work."
    : !currentComponent ? stage === "CAD" ? "Select a component in the model tree, or open a unit from Plant, to inspect or attach geometry."
      : stage === "CFD" ? "Select a component in the model tree, or open a unit from Plant, to inspect its CFD runs. Selection does not imply a result exists."
      : "Select a component in the model tree, or open a unit from Plant, to inspect its reduced models."
    : stage === "CAD" ? "Inspect this component's Geometry node. Attach or generate geometry there if none is available."
    : stage === "CFD" ? "Inspect this component's Results and run status below; an empty result means no finished run is available."
    : "Inspect this component's Reduced model node for available models and their status.";

  const leftPane = <aside id="design-pane-tree" className="design-project__panel design-project__left" aria-label="Model tree">
    <h2>Components</h2>
    <DesignComponentTree components={components} selectedId={selectedId} selectedNode={selectedNode}
      onSelectComponent={selectComponent} onSelectNode={selectNode} nodeStatus={nodeStatus} />
    <form onSubmit={(event) => { event.preventDefault(); if (!active || !newComponent.trim()) return; void perform(async () => { await createDesignComponent(projectId, active.id, newComponent.trim(), selectedId); setNewComponent(""); }); }}>
      <label htmlFor="design-component-name">{selected ? `Add within ${selected.name}` : "Add a component"}</label>
      <input id="design-component-name" value={newComponent} maxLength={120} onChange={(event) => setNewComponent(event.target.value)} />
      <button type="submit" disabled={!active || busy || !newComponent.trim()}>Add</button>
    </form>
    <details className="design-project__fold">
      <summary>Library</summary>
      {library.length ? <ul className="design-library">{library.map((item) => { const label = libraryLabel(item); return <li key={item.id}>
        <button type="button" className="design-library__row" title={label.full} onClick={() => setWorkshop({ modelId: item.model_spec_id, key: Date.now() })}>
          {label.kind ? <span className="design-library__kind">{label.kind}</span> : null}<span className="design-library__name">{label.name}</span><span className="design-library__version">{label.version}</span>
        </button></li>; })}</ul> : <p className="design-empty">empty</p>}
      <button type="button" disabled={!active} onClick={() => setWorkshop({ modelId: null, key: Date.now() })}>Generate new in Workshop</button>
      {firstVersion ? <AppLink href="/design/studio" navigate={navigate} {...designTarget("design.link.studio")}>Open Studio</AppLink> : null}
    </details>
    <details className="design-project__fold">
      <summary>Versions</summary>
      <form onSubmit={(event) => { event.preventDefault(); if (!versions || !newVersion.trim()) return; void perform(async () => { await createDesignVersion(projectId, newVersion.trim(), versions.active_version_id, emptyVersion ? "empty" : "fork"); setNewVersion(""); setEmptyVersion(false); }); }}>
        <label htmlFor="design-new-version">New version (forks the current version)</label>
        <input id="design-new-version" value={newVersion} maxLength={80} onChange={(event) => setNewVersion(event.target.value)} />
        <label><input type="checkbox" checked={emptyVersion} onChange={(event) => setEmptyVersion(event.target.checked)} />Start empty instead of forking</label>
        <button type="submit" disabled={!versions || busy || !newVersion.trim()}>{emptyVersion ? "Create empty version" : "Fork version"}</button>
      </form>
    </details>
  </aside>;

  return <div className="design-project">
    <header className="design-project__header" aria-label="Design top bar">
      <div className="design-project__title"><p className="eyebrow">Design</p><h1>{projectName || "Project"}<ExampleBadge workspace={projectWorkspace} /></h1></div>
      <div className="design-project__version">
        <label htmlFor="design-version">Project version</label>
        <select id="design-version" value={versions ? `version:${versions.active_version_id}` : ""} disabled={!versions || busy} {...designTarget("design.version-picker")}
          onChange={(event) => void pickVersion(event.target.value)}>
          {versions ? <>
            {homeSet?.versions.length ? <optgroup label="Versions">
              {homeSet.versions.map((item) => <option key={item.id} value={`${inCaseStudy ? "home" : "version"}:${item.id}`}>{versionLabel(item)}</option>)}
            </optgroup> : null}
            {caseStudies.length ? <optgroup label="Case studies">
              {caseStudies.flatMap((item) => item.id !== projectId ? [<option key={item.id} value={`case:${item.id}`} title={item.name}>{caseStudyLabel(item)}</option>]
                : versions.versions.map((version) => <option key={version.id} value={`version:${version.id}`} title={item.name}>
                  {caseStudyLabel(item)}{versions.versions.length > 1 ? ` · ${versionLabel(version)}` : ""}</option>))}
            </optgroup> : null}
          </> : null}
        </select>
      </div>
      <nav className="design-project__stages" aria-label="Design stages" {...designTarget("design.stages")}>
        {STAGES.map((item) => <button key={item} type="button" aria-current={stage === item ? "step" : undefined} {...designTarget(STAGE_TARGET[item])}
          title={item === "Motion/Mechanics" ? "Placeholder: no motion or mechanics capability yet" : undefined} onClick={() => {
          setWorkshop(null);
          if (plantMode && plantUnit && (item === "CAD" || item === "CFD" || item === "Reduced model")) {
            void openUnit(plantUnit.draftId, plantUnit.revision, plantUnit.unit, item);
            return;
          }
          showView({ stage: item, componentId: item === "Plant" ? null : selectedId, node: STAGE_NODE[item] });
        }}>{item}{item === "Motion/Mechanics" ? <small className="design-project__placeholder"> (placeholder)</small> : null}</button>)}
      </nav>
    </header>
    {error ? <p role="alert" className="design-project__error">{error}</p> : null}
    <section className="design-project__orientation" aria-label="Current Design location and stage help">
      <p><strong>Current location:</strong> {location}</p>
      <p>{stageHelp}</p>
    </section>
    {offerExample ? <p className="design-project__example-offer">This project has no Design components yet. Open a Process unit from the Plant flowsheet or add a component to start, or explore the worked case study:{" "}
      <button type="button" onClick={() => switchProject(offerExample.id)} {...designTarget("design.example-open")}>Open case study C1</button></p> : null}
    {workshop && active ? <section className="design-project__workshop" aria-label="Workshop">
      <Studio key={`${active.data_workspace_id}:${workshop.key}`} workspaceId={active.data_workspace_id} navigate={navigate}
        engineeringProperties={engineeringProperties} requestedModelId={workshop.modelId} startWizard={!workshop.modelId} embedded contextLabel={`Design · ${active.label}`}
        onExit={() => { setWorkshop(null); void reload(); }} />
    </section> : opticsMode ? <div className="design-project__body design-project__body--plant">
      {leftPane}
      {active ? <DesignOpticsPanel workspaceId={active.data_workspace_id} plantDraftId={plantDraftId} /> : <p>Loading the active version…</p>}
    </div> : dynamicMode ? <div className="design-project__body design-project__body--plant">
      {active ? <DesignDynamicPanel key={active.id} workspaceId={active.data_workspace_id} plantDraftIds={plantDraftIds} requestedDraftId={view.draftId ?? null}
        onChooseDraft={(draftId, replace) => showView({ ...view, draftId }, replace)} onOpenPlant={() => showView({ stage: "Plant", componentId: null, node: STAGE_NODE.Plant })} />
        : <p>Loading the active version…</p>}
    </div> : plantMode ? <div className="design-project__body design-project__body--plant">
      <section className="design-project__panel design-project__main" aria-label="Plant" {...designTarget("design.panel.plant")}>
        <h2>Plant</h2>
        <p className="design-project__plant-hint" role="status">{plantUnit
          ? <>Selected <strong>{plantUnit.unit.tag}</strong>. Choose CAD, CFD or Reduced model above to work on it.</>
          : "Click a unit in the flowsheet to select it, then choose CAD, CFD or Reduced model above to work on it."}</p>
        {active ? <>
          <details className="design-project__about"><summary>About Plant</summary><p>This is the plant flowsheet: the same one you see in the Process editor. Double-click a unit to open it as a component and work on its geometry, CFD and reduced model.</p></details>
          <div className="design-project__context-links">
            <button type="button" onClick={() => setEnvironmentOpen(true)} {...designTarget("design.link.environment")}>Environment…</button>
            {firstVersion ? <AppLink href="/design/process" navigate={navigate} {...designTarget("design.link.process-editor")}>Open full Process editor</AppLink> : null}          </div>
          <ProcessDraftEditor key={`${active.id}:${plantDraftId ?? "unbound"}`} workspaceId={active.data_workspace_id} initialDraftId={plantDraftId} navigate={navigate} dynamicLink={false} onOpenUnit={(draftId, revision, unit) => void openUnit(draftId, revision, unit)} onSelectUnit={setPlantUnit} />
          {environmentOpen ? <Suspense fallback={<p role="status">Loading environment profiles…</p>}><EnvironmentPanel workspaceId={active.data_workspace_id} onClose={() => setEnvironmentOpen(false)} /></Suspense> : null}
        </> : <p>Loading the active version…</p>}
      </section>
    </div> : <div className={`design-project__body design-project__body--components${selectedNode === "Results" && fieldExpanded ? " design-project__body--field" : ""}`}
      style={{ "--design-left": `${widths.left}px`, "--design-right": `${widths.right}px` } as CSSProperties}>
      {leftPane}
      <DesignSplitter label="Resize model tree" controls="design-pane-tree" bounds={LEFT_BOUNDS} side="start" width={widths.left}
        onResize={(left) => setWidths((current) => ({ ...current, left }))} onCommit={(left) => commitWidths({ ...widths, left })}
        onReset={() => commitWidths({ ...widths, left: LEFT_BOUNDS.fallback })} />
      <section id="design-pane-viewport" className="design-project__panel design-project__viewport" aria-label="Component viewport" {...designTarget("design.panel.viewport")}>
        <div className="design-project__viewport-heading"><h2>{selected && selectedNode === "Results" ? `${selected.name} · CFD results` : selected ? `${selected.name} · 3D viewport` : "3D viewport"}</h2>
          {selected && selectedNode === "Results" ? <button type="button" aria-pressed={fieldExpanded} onClick={() => setFieldExpanded((current) => !current)} {...designTarget("design.field.expand")}>{fieldExpanded ? "Show inspector" : "Expand field"}</button> : null}
        </div>
        {active && selected && selectedNode === "Results" ? <CfdResultBlocks projectId={projectId} versionId={active.id} componentId={selected.id}
          runs={cfdRuns} chosen={compareIds} onFieldState={reportFieldSlice} /> : null}
        {selected && selectedNode === "Results" ? null
          : glbUrl ? <BluecadGlbViewer key={glbUrl} artifactUrl={glbUrl} />
          : <div className="design-project__viewport-empty" role="status" aria-label={selected?.geometry_candidate_id ? "Geometry preview" : selected ? `No geometry attached to ${selected.name}` : "No component selected"}>
            {selected?.geometry_candidate_id ? geometryAggregate?.candidate.id === selected.geometry_candidate_id
              ? <DesignStatus state="empty" title="This geometry has no 3D preview" /> : <DesignStatus state="checking" />
              : selected ? <DesignStatus state="empty" title="Attach or generate it from the Geometry node" />
              : <span className="design-empty">No component selected</span>}
          </div>}
      </section>
      <DesignSplitter label="Resize inspector" controls="design-pane-inspector" bounds={RIGHT_BOUNDS} side="end" width={widths.right}
        onResize={(right) => setWidths((current) => ({ ...current, right }))} onCommit={(right) => commitWidths({ ...widths, right })}
        onReset={() => commitWidths({ ...widths, right: RIGHT_BOUNDS.fallback })} />
      <aside id="design-pane-inspector" className="design-project__panel design-project__inspector" aria-label="Inspector" {...designTarget("design.panel.inspector")}>
        <h2>{selected ? `${selected.name} · ${selectedNode}` : stage}</h2>
        {selected?.source_unit_id ? <details className="design-project__about"><summary>Process unit</summary><p>Calculated by the plant flowsheet (0D/1D). Geometry, CFD and reduced models are separate.</p></details> : null}
        {selected ? <>
          <section aria-label={`${selectedNode} details`}>
            <div className="design-project__node-head"><h3>{selectedNode}</h3>{inspectorBadge ? <DesignStatus state={inspectorBadge.state} title={inspectorBadge.detail} /> : null}</div>
            {selectedNode === "Geometry" ? <>
              <dl className="design-project__fields">
                <div><dt>BlueCad geometry</dt><dd>{!selected.geometry_candidate_id ? <Empty />
                  : geometryAggregate?.candidate.id === selected.geometry_candidate_id ? (geometryAggregate.candidate.brief_text.slice(0, 80) || "Attached") : "Attached"}</dd></div>
              </dl>
              {selected.geometry_candidate_id ? <details className="design-project__about"><summary>Details</summary><code>{selected.geometry_candidate_id}</code>{selected.geometry_source_draft_revision ? <p>Process source revision: {selected.geometry_source_draft_revision.slice(0, 12)}</p> : null}<p>No CFD run or reduced model is implied.</p></details> : null}
              {firstVersion && selected.geometry_candidate_id ? <AppLink href="/design/bluecad" navigate={navigate} {...designTarget("design.link.bluecad")}>Open BlueCad workbench</AppLink> : null}
            </> : selectedNode === "Parameters" ? <>
              <dl className="design-project__fields">
                <div><dt>Source</dt><dd title={selected.source_unit_id ? "Read from this version's Process draft" : undefined}>{selected.source_unit_id ? "Process" : <Empty />}</dd></div>
                <div><dt>Unit</dt><dd>{sourceUnit ? `${sourceUnit.tag} · ${sourceUnit.type}` : <Empty />}</dd></div>
                {parameterEntries.map(([key, value]) => <div key={key}><dt title={key}>{readableKey(key)}</dt><dd>{value.value} {value.unit}</dd></div>)}
              </dl>
              {sourceError ? <p role="status" className="design-project__error">{sourceError}</p> : null}
            </> : selectedNode === "Mesh" && active ? <CfdMeshPanel projectId={projectId} versionId={active.id} componentId={selected.id} />
              : selectedNode === "Results" ? <>
                <CfdRunPicker runs={cfdRuns} chosen={compareIds} onChoose={setCompareIds} />
                <details className="design-project__about"><summary>Details</summary><p>Checked runs are shown one below the other in the viewport. Study templates define the CFD inputs; they are recorded and run in the Study templates fold below.</p></details>
                {firstVersion ? <AppLink href="/design/studies" navigate={navigate} {...designTarget("design.link.studies")}>Open engineering Studies</AppLink> : null}</>
              : null}
          </section>
          {active && selectedNode === "Reduced model" ? <ReducedModelsPanel projectId={projectId} versionId={active.id} component={selected}
            draftRevision={selected.source_draft_id ? sourceRevision : null}
            onChanged={async () => { await reload(); setSourceNonce((value) => value + 1); }} /> : null}
          {active && selectedNode === "Results" ? <details className="design-project__fold"><summary>Study templates</summary>
            <StudyTemplatesPanel projectId={projectId} versionId={active.id} component={selected}
              onChanged={async () => { await reload(); }} onRunsChange={onRunsChange} /></details> : null}
          {active && (selectedNode === "Physics" || selectedNode === "Geometry" || selectedNode === "Materials") ? <section aria-label={`${selectedNode} Studio models`}>
            <h4>Studio models</h4>
            {nodeModels.length ? <ul>{nodeModels.map((item) => <li key={item.model_version_id} title={`Studio ${item.category} · source ${item.source_sha256.slice(0, 12)}`}>
              {libraryNames.get(item.model_version_id) ?? "Studio model"}
            </li>)}</ul> : <p><Empty /></p>}
            <form onSubmit={(event) => {
              event.preventDefault();
              if (!modelSelection) return;
              void perform(async () => { await attachDesignModel(projectId, active.id, selected, modelSelection); setModelSelection(""); });
            }}>
              <label htmlFor="design-node-model">Attach Studio model</label>
              <select id="design-node-model" value={modelSelection} disabled={!compatibleModels.length} onChange={(event) => setModelSelection(event.target.value)}>
                <option value="">{compatibleModels.length ? "Choose a model" : "empty"}</option>
                {compatibleModels.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.version_label}</option>)}
              </select>
              <button type="submit" disabled={busy || !modelSelection} title={compatibleModels.length ? undefined : "No compatible model in this version's Library"} {...designTarget(ATTACH_MODEL)}>Attach model</button>
              <details className="design-project__about"><summary>Details</summary><p>Published Studio models of this node's kind attach here. Process unit models and procedures attach to Plant through Process.</p></details>
            </form>
          </section> : null}
          {selectedNode === "Geometry" && active ? <DesignFeatureInspector projectId={projectId} versionId={active.id} component={selected}
            draftRevision={selected.source_draft_id ? sourceRevision : null} disabled={busy || Boolean(selected.source_draft_id && !sourceRevision)}
            onApplied={reload} /> : null}
          {selectedNode === "Geometry" && active ? <form onSubmit={(event) => {
            event.preventDefault();
            if (!geometrySelection || (selected.source_draft_id && !sourceRevision)) return;
            void perform(() => attachDesignGeometry(projectId, active.id, selected, geometrySelection,
              selected.source_draft_id ? sourceRevision : null));
          }}>
            <label htmlFor="design-geometry-candidate">Attach BlueCad candidate</label>
            <select id="design-geometry-candidate" value={geometrySelection} disabled={!geometryCandidates.length} onChange={(event) => setGeometrySelection(event.target.value)}>
              <option value="">{geometryCandidates.length ? "Choose a candidate" : "empty"}</option>
              {geometryCandidates.map((item) => <option key={item.id} value={item.id} title={item.id}>{item.brief_text.slice(0, 80)}</option>)}
            </select>
            <button type="submit" disabled={busy || !geometrySelection || Boolean(selected.source_draft_id && !sourceRevision)}
              title={geometryCandidates.length ? undefined : "No validated BlueCad candidate in this version"}>Attach geometry</button>
          </form> : null}
          {selectedNode === "Geometry" && active && selected.source_unit_id ? <form aria-label="Geometry from Process" onSubmit={(event) => {
            event.preventDefault();
            const wall = Number(wallMm);
            if (!geometryMapping?.supported || !(wall > 0)) return;
            void perform(() => deepenDesignGeometry(projectId, active.id, selected, wall, geometryMapping.draft_revision));
          }}>
            <div className="design-project__node-head"><h4>From Process</h4>
              <DesignStatus state={!geometryMapping ? "checking" : geometryMapping.supported ? "ready" : "empty"}
                title={geometryMapping && !geometryMapping.supported ? readableProse(geometryMapping.basis) : undefined} /></div>
            <dl className="design-project__fields">
              <div><dt>Template</dt><dd>{deepenable?.template ?? <Empty />}</dd></div>
              <div><dt>Inner Ø</dt><dd>{deepenable?.inner_d_mm != null ? `${deepenable.inner_d_mm.toFixed(2)} mm` : <Empty />}</dd></div>
              <div><dt>Length</dt><dd>{deepenable?.length_mm != null ? `${deepenable.length_mm.toFixed(1)} mm` : <Empty />}</dd></div>
              {deepenable?.segment_count ? <>
                <div><dt>Runs</dt><dd>{deepenable.segment_count} × {deepenable.segment_length_mm?.toFixed(1)} mm</dd></div>
                <div><dt>Bend radius</dt><dd>{deepenable.bend_radius_mm?.toFixed(0)} mm</dd></div>
              </> : null}
            </dl>
            <label htmlFor="design-geometry-wall">Wall thickness (mm)</label>
            <NumericInput id="design-geometry-wall" min={0.5} max={100} value={wallMm} onValueChange={setWallMm} />
            <button type="submit" disabled={busy || !deepenable || !(Number(wallMm) > 0)}
              title={deepenable ? undefined : "This Process unit has no BlueCad template"}>Generate and attach geometry</button>
            {geometryMapping ? <details className="design-project__about"><summary>Details</summary>
              <p>{geometryMapping.unit_type} → BlueCad {geometryMapping.template ?? "(none)"} template. {readableProse(geometryMapping.basis)}</p>
              {geometryMapping.supported ? <p>Wall thickness is not a Process parameter.</p> : null}</details> : null}
          </form> : null}
          {active ? <form onSubmit={(event) => { event.preventDefault(); if (!editName.trim()) return;
            void perform(() => editDesignComponent(projectId, active.id, selected, editName.trim(), editParent || null));
          }}>
            <h3>Edit {selected.name}</h3>
            <label htmlFor="design-edit-name">Name</label>
            <input id="design-edit-name" value={editName} maxLength={120} onChange={(event) => setEditName(event.target.value)} />
            <label htmlFor="design-edit-parent">Parent component</label>
            <select id="design-edit-parent" value={editParent} onChange={(event) => setEditParent(event.target.value)}>
              <option value="">Plant</option>
              {components.filter((item) => !descendants.has(item.id)).map((item) =>
                <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
            <button type="submit" disabled={busy || !editName.trim() ||
              (editName.trim() === selected.name && editParent === (selected.parent_id ?? ""))}>Save component</button>
          </form> : null}
        </> : null}
        {stage === "Motion/Mechanics" ? <div className="design-project__node-head"><h3>Motion/Mechanics</h3><DesignStatus state="empty" title="Not modelled yet" /></div>
          : !selected ? <p className="design-empty">Select a component</p> : null}
      </aside>
    </div>}
  </div>;
}
