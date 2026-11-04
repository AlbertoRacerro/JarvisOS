import type { KnowledgeContextPreview } from "./api/knowledgeActions";
import { useEffect, useCallback, useRef, useState, type ReactNode } from "react";

import type { StageSelection } from "./app/selection";
import { useAppRouter } from "./app/useAppRouter";
import { publishDesignSurface, publishWorkspaceListSurface } from "./app/workspaceActionSurface";
import Layout from "./components/Layout";
import { useBubuIntegration } from "./components/bubu/useBubuIntegration";
import PageErrorBoundary from "./components/PageErrorBoundary";
import JarvisKnowledgeActions from "./components/ai/JarvisKnowledgeActions";
import { useJarvisSidecar } from "./components/ai/useJarvisSidecar";
import ProcessProposals from "./components/process/ProcessProposals";
import AnalyticsDockContent from "./components/analytics/AnalyticsDockContent";
import {
  EngineeringPropertiesPanel,
  useEngineeringProperties
} from "./components/engineering/EngineeringProperties";
import JarvisEngineeringActions from "./components/engineering/JarvisEngineeringActions";
import FinalOperatorReadSurface from "./components/fusion/FinalOperatorReadSurface";
import FinalSettingsSurface from "./components/fusion/FinalSettingsSurface";
import FinalWorkspaceHeader from "./components/fusion/FinalWorkspaceHeader";
import ProjectKnowledgePanel from "./components/fusion/ProjectKnowledgePanel";
import ProjectSearchPanel from "./components/fusion/ProjectSearchPanel";
import WorkspaceBootstrap, { readStoredWorkspaceId, writeStoredWorkspaceId } from "./components/WorkspaceBootstrap";
import { listWorkspaces, type Workspace } from "./api/client";
import MigrationPendingSurface from "./components/shell/MigrationPendingSurface";
import ProjectSwitcher from "./components/shell/ProjectSwitcher";
import AIThreads from "./pages/AIThreads";
import CodingWorkbench from "./pages/CodingWorkbench";
import DesignProject from "./pages/DesignProject";
import DevelopmentRoadmap from "./pages/DevelopmentRoadmap";
import EngineeringData from "./pages/EngineeringData";
import LiteratureKnowledge from "./pages/LiteratureKnowledge";
import ModelDossier from "./pages/ModelDossier";
import RunsWorkbench from "./pages/RunsWorkbench";
import Studio from "./pages/Studio";
import { PRIMARY_STAGES, type ShellRegion, type ShellRegionContributions } from "./stages/registry";

type ShellRegionRequest = Readonly<{ region: ShellRegion; nonce: number }>;
const PROJECT_BASIS_RECORD_KINDS = new Set(["requirement", "parameter", "assumption", "decision"]);
const KNOWLEDGE_ROUTES = new Set(["memory-project-basis", "memory-models", "memory-literature"]);
// 161: these routes render the Jarvis conversation in-page; the shell Sidecar must not offer a second copy.
const IN_PAGE_JARVIS_ROUTES = new Set(["memory-project-basis", "memory-models", "memory-literature", "development-roadmap-timeline", "development-roadmap-calendar"]);
const WORKSPACE_OPTIONAL_ROUTES = new Set(["settings-appearance", "settings-ai", "settings-system", "coding-runtime"]);
type WorkspaceLoadState = "loading" | "ready" | "empty" | "error";

function boundedSearchParam(params: URLSearchParams, name: string): string | null {
  const value = params.get(name)?.trim() ?? "";
  return value && value.length <= 200 ? value : null;
}

function App() {
  const { resolved, search, navigate } = useAppRouter();
  const { route } = resolved;
  const [workspaceId, setWorkspaceIdState] = useState<string | null>(null);
  const [workspaceList, setWorkspaceList] = useState<Workspace[]>([]);
  const [workspaceLoadState, setWorkspaceLoadState] = useState<WorkspaceLoadState>("loading");
  const [workspaceLoadError, setWorkspaceLoadError] = useState<string | null>(null);
  const workspaceIds = useRef(new Set<string>());
  const workspaceLoadGeneration = useRef(0);
  const [selection, setSelection] = useState<StageSelection | null>(null);
  const [shellRegions, setShellRegions] = useState<ShellRegionContributions>({});
  const [shellRegionRequest, setShellRegionRequest] = useState<ShellRegionRequest | null>(null);
  const [knowledgeContext, setKnowledgeContext] = useState<KnowledgeContextPreview | null>(null);
  const [selectedKnowledgeLabel, setSelectedKnowledgeLabel] = useState<string | null>(null);
  const [selectedKnowledgeRef, setSelectedKnowledgeRef] = useState<string | null>(null);
  const [selectedModelVersionId, setSelectedModelVersionId] = useState<string | null>(null);
  const [studioModelId, setStudioModelId] = useState<string | null>(null);
  const selectKnowledge = useCallback((ref: string | null, label?: string) => {
    setSelectedKnowledgeRef(ref); setSelectedKnowledgeLabel(label ?? null);
  }, []);
  const selectModelVersion = useCallback((id: string | null, label?: string) => {
    setSelectedModelVersionId(id); setSelectedKnowledgeLabel(label ?? null);
  }, []);
  const loadWorkspaces = useCallback((preferredId?: string | null) => {
    const generation = ++workspaceLoadGeneration.current;
    setWorkspaceLoadState("loading"); setWorkspaceLoadError(null); setWorkspaceIdState(null);
    return listWorkspaces().then((items: Workspace[]) => {
      if (generation !== workspaceLoadGeneration.current) return;
      workspaceIds.current = new Set(items.map((item) => item.id));
      setWorkspaceList(items);
      const stored = preferredId ?? readStoredWorkspaceId();
      const selected = stored && workspaceIds.current.has(stored) ? stored : items[0]?.id ?? null;
      setWorkspaceIdState(selected);
      writeStoredWorkspaceId(selected);
      setWorkspaceLoadState(items.length ? "ready" : "empty");
    }).catch((cause: unknown) => {
      if (generation !== workspaceLoadGeneration.current) return;
      setWorkspaceLoadState("error"); setWorkspaceLoadError(cause instanceof Error ? cause.message : "Workspace discovery failed.");
    });
  }, []);
  useEffect(() => { void loadWorkspaces(); return () => { workspaceLoadGeneration.current++; }; }, [loadWorkspaces]);
  const setWorkspaceId = useCallback((next: string | null) => {
    if (!next || !workspaceIds.current.has(next)) { void loadWorkspaces(next); return; }
    setWorkspaceIdState(next);
    writeStoredWorkspaceId(next);
  }, [loadWorkspaces]);
  const handleWorkspaceCreated = useCallback((workspace: Workspace) => {
    workspaceLoadGeneration.current++;
    workspaceIds.current.add(workspace.id); setWorkspaceList((current) => [...current, workspace]); setWorkspaceIdState(workspace.id); setWorkspaceLoadState("ready"); setWorkspaceLoadError(null);
    writeStoredWorkspaceId(workspace.id);
  }, []);
  const engineeringProperties = useEngineeringProperties(workspaceId, setWorkspaceId);
  const routeParams = new URLSearchParams(window.location.search);
  const requestedRecordKind = boundedSearchParam(routeParams, "recordKind");
  const requestedRecordId = boundedSearchParam(routeParams, "recordId");
  const requestedRecordRef = requestedRecordKind && requestedRecordId && PROJECT_BASIS_RECORD_KINDS.has(requestedRecordKind)
    ? `${requestedRecordKind}:${requestedRecordId}`
    : null;
  const requestedModelVersionId = boundedSearchParam(routeParams, "modelVersionId");
  const requestedLiteratureSourceId = boundedSearchParam(routeParams, "sourceId");
  const requestedLiteratureEntryId = boundedSearchParam(routeParams, "entryId");
  const knowledgeStableRef = route.id === "memory-project-basis"
    ? selectedKnowledgeRef ?? requestedRecordRef
    : route.id === "memory-models" && selectedModelVersionId
      ? `model_version:${selectedModelVersionId}`
      : route.id === "memory-literature" && selectedKnowledgeRef
        ? selectedKnowledgeRef
        : route.id === "memory-literature" && requestedLiteratureEntryId
        ? `literature_entry:${requestedLiteratureEntryId}`
        : route.id === "memory-literature" && requestedLiteratureSourceId
          ? `literature_source:${requestedLiteratureSourceId}`
          : null;

  useEffect(() => {
    setSelection(null);
    setSelectedKnowledgeRef(null);
    setSelectedKnowledgeLabel(null);
    setShellRegions({});
    setShellRegionRequest(null);
    setSelectedModelVersionId(null);
    setStudioModelId(null);
  }, [route.id]);

  useEffect(() => {
    setSelectedKnowledgeRef(null);
    setSelectedKnowledgeLabel(null);
    setSelectedModelVersionId(null);
  }, [workspaceId]);

  useEffect(() => {
    if (selection?.kind === "record" && selection.ref.workspaceId !== workspaceId) setWorkspaceId(selection.ref.workspaceId);
  }, [selection, setWorkspaceId, workspaceId]);

  // ADR-061 R07: until the workspace list resolves, New Design has no surface; the shell publishes the list's state.
  // Once it is ready, DesignProject owns the surface, so nothing is published here.
  useEffect(() => {
    if (route.id !== "design-project" || workspaceLoadState === "ready") return;
    if (workspaceLoadState === "empty") publishDesignSurface(null);
    else publishWorkspaceListSurface(workspaceLoadState === "loading" ? "loading" : "unavailable");
  }, [route.id, workspaceLoadState]);

  const requestShellRegionOpen = (region: ShellRegion) => setShellRegionRequest((current) => ({ region, nonce: (current?.nonce ?? 0) + 1 }));

  const stageSidecar = shellRegions.sidecar;
  const semanticSelectionContext = selection?.kind === "bluecad-part" ? <div className="shell-properties__selection"><strong>{selection.partId}</strong><p>{selection.partKind ? `${selection.partKind} · selected BlueCad part` : "Selected BlueCad part"}</p></div> : undefined;
  const knowledgeActions = <JarvisKnowledgeActions workspaceId={workspaceId} routeId={route.id} stableRef={knowledgeStableRef} selectedLabel={selectedKnowledgeLabel} onContextChange={setKnowledgeContext} />;
  const jarvisLocalContext = KNOWLEDGE_ROUTES.has(route.id)
    ? knowledgeActions
    : <>{semanticSelectionContext}<JarvisEngineeringActions controller={engineeringProperties} />{knowledgeActions}</>;
  // 155: pending process proposals stay visible above the composer, not inside the collapsed context.
  const processProposals = route.id === "design-process" && workspaceId ? <ProcessProposals workspaceId={workspaceId} /> : null;
  const jarvisSidecar = useJarvisSidecar(workspaceId, route.id, selection, jarvisLocalContext, knowledgeContext, processProposals, studioModelId);
  const bubu = useBubuIntegration(navigate);

  let content: ReactNode;
  const workspaceRequired = !WORKSPACE_OPTIONAL_ROUTES.has(route.id);
  if (workspaceRequired && workspaceLoadState !== "ready") {
    content = workspaceLoadState === "loading" ? <div className="workspace-bootstrap" aria-live="polite"><p>Loading workspaces…</p></div>
      : workspaceLoadState === "error" ? <WorkspaceBootstrap error={workspaceLoadError} onRetry={() => void loadWorkspaces()} onCreated={handleWorkspaceCreated} />
      : <WorkspaceBootstrap onCreated={handleWorkspaceCreated} />;
  } else if (route.stageKind && (route.id === "design-process" || route.id === "design-bluecad" || route.id === "review")) {
    const Stage = PRIMARY_STAGES[route.stageKind].render;
    content = <Stage workspaceId={workspaceId} onWorkspaceChange={setWorkspaceId} selection={selection} onSelectionChange={setSelection} onShellRegionsChange={setShellRegions} requestShellRegionOpen={requestShellRegionOpen} navigate={navigate} />;
  } else if (route.id === "design-studies") {
    const Stage = PRIMARY_STAGES.studies.render;
    content = <Stage workspaceId={workspaceId} onWorkspaceChange={setWorkspaceId} selection={selection} onSelectionChange={setSelection} onShellRegionsChange={setShellRegions} requestShellRegionOpen={requestShellRegionOpen} navigate={navigate} />;
  } else {
    switch (route.id) {
      case "design-project":
        content = <DesignProject projectId={workspaceId} onProjectChange={setWorkspaceId} search={search} navigate={navigate} engineeringProperties={engineeringProperties} />;
        break;
      case "design-studio":
        content = <Studio workspaceId={workspaceId} navigate={navigate} engineeringProperties={engineeringProperties} onModelSelectionChange={setStudioModelId} onWorkspaceChange={setWorkspaceId} />;
        break;
      case "memory-project-basis":
        content = <><FinalWorkspaceHeader group="memory" active="project-basis" navigate={navigate} /><FinalOperatorReadSurface kind="project-basis" workspaceId={workspaceId} onWorkspaceChange={setWorkspaceId} requestedRecordRef={requestedRecordRef} onRecordSelect={selectKnowledge} jarvis={jarvisSidecar} projectSearch={<ProjectSearchPanel workspaceId={workspaceId} navigate={navigate} />} /></>;
        break;
      case "memory-models":
        content = <><FinalWorkspaceHeader group="memory" active="models" navigate={navigate} /><ModelDossier workspaceId={workspaceId} onWorkspaceChange={setWorkspaceId} requestedModelVersionId={requestedModelVersionId} onModelVersionSelectionChange={selectModelVersion} jarvis={jarvisSidecar} revisionPanel={<ProjectKnowledgePanel workspaceId={workspaceId} readOnly />} /></>;
        break;
      case "memory-literature":
        content = <><FinalWorkspaceHeader group="memory" active="literature" navigate={navigate} /><LiteratureKnowledge kind="literature" workspaceId={workspaceId} jarvis={jarvisSidecar} onWorkspaceChange={setWorkspaceId} requestedSourceId={requestedLiteratureSourceId} requestedEntryId={requestedLiteratureEntryId} onKnowledgeSelectionChange={selectKnowledge} searchPanel={<ProjectSearchPanel workspaceId={workspaceId} navigate={navigate} />} /></>;
        break;
      case "development-roadmap-timeline":
        content = <><FinalWorkspaceHeader group="development" active="roadmap" navigate={navigate} /><DevelopmentRoadmap mode="timeline" workspaceId={workspaceId} onWorkspaceChange={setWorkspaceId} jarvis={jarvisSidecar} /></>;
        break;
      case "development-roadmap-calendar":
        content = <><FinalWorkspaceHeader group="development" active="roadmap" navigate={navigate} /><DevelopmentRoadmap mode="calendar" workspaceId={workspaceId} onWorkspaceChange={setWorkspaceId} jarvis={jarvisSidecar} /></>;
        break;
      case "coding-repository":
        content = <><FinalWorkspaceHeader group="coding" active="repository" navigate={navigate} /><CodingWorkbench mode="repository" workspaceId={workspaceId} jarvis={jarvisSidecar} /></>;
        break;
      case "coding-runtime":
        content = <><FinalWorkspaceHeader group="coding" active="runtime" navigate={navigate} /><CodingWorkbench mode="runtime" workspaceId={workspaceId} jarvis={jarvisSidecar} /></>;
        break;
      case "settings-appearance":
        content = <FinalSettingsSurface section="appearance" navigate={navigate} />;
        break;
      case "settings-ai":
        content = <FinalSettingsSurface section="ai" navigate={navigate} />;
        break;
      case "settings-system":
        content = <FinalSettingsSurface section="system" navigate={navigate} onProjectsChanged={() => void loadWorkspaces(workspaceId)} />;
        break;
      case "runs":
        content = <RunsWorkbench workspaceId={workspaceId} onWorkspaceChange={setWorkspaceId} engineeringProperties={engineeringProperties} />;
        break;
      case "engineering-data":
        content = <EngineeringData workspaceId={workspaceId} onWorkspaceChange={setWorkspaceId} navigate={navigate} />;
        break;
      case "ai-threads":
        content = <AIThreads workspaceId={workspaceId} />;
        break;
      case "not-found":
      default:
        content = <MigrationPendingSurface title="Page not found" description={`No application route matches ${resolved.canonicalPath}.`} navigate={navigate} links={[{ href: "/design/project", label: "Open Design" }, { href: "/memory/models", label: "Open Models" }]} unavailable />;
        break;
    }
  }

  const propertiesContent = <EngineeringPropertiesPanel controller={engineeringProperties} stageContext={stageSidecar} navigate={navigate} />;
  const effectiveShellRegions: ShellRegionContributions = {
    ...shellRegions,
    sidecar: route.primaryNav === "coding" || (IN_PAGE_JARVIS_ROUTES.has(route.id) && workspaceLoadState === "ready") ? undefined : jarvisSidecar,
    ...(route.id === "runs" || route.id === "engineering-data" || route.id === "design-process" ? { dock: <AnalyticsDockContent workspaceId={workspaceId} /> } : {}),
  };

  const projectIndicator = workspaceLoadState === "ready" ? <ProjectSwitcher workspaces={workspaceList} workspaceId={workspaceId} onChange={setWorkspaceId} /> : null;

  return <Layout route={route} navigate={navigate} selection={selection} propertiesContent={propertiesContent} shellRegions={effectiveShellRegions} shellRegionRequest={shellRegionRequest} project={projectIndicator} bubu={bubu.integration}>{bubu.overlay}<PageErrorBoundary key={resolved.canonicalPath}>{content}</PageErrorBoundary></Layout>;
}

export default App;
