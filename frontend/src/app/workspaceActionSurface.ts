import type { ProcessSurface } from "../components/ai/workspaceActionPresentation";

// ADR-061 observe slot: every published surface carries an epoch that moves exactly when a published field changes,
// so a pointer or plan step computed at one epoch can be refused at another. Republishing identical content keeps it.
type Unstamped<T> = Omit<T, "epoch">;
function stamper<T extends { epoch: number }>() {
  let epoch = 0, last: string | null = null;
  const stamp = (next: Unstamped<T>): T => {
    const content = JSON.stringify(next);
    if (content !== last) { epoch += 1; last = content; }
    return { ...next, epoch } as T;
  };
  stamp.invalidate = () => { last = null; };
  return stamp;
}

const SURFACE_EVENT = "jarvis:workspace-surface";
const stampProcess = stamper<ProcessSurface>();
let processSurface: ProcessSurface = stampProcess({ draft_id: null, revision: null, process_selection: [] });

export function publishProcessSurface(next: Unstamped<ProcessSurface>): void {
  processSurface = stampProcess(next);
  window.dispatchEvent(new CustomEvent(SURFACE_EVENT, { detail: processSurface }));
}

export function currentProcessSurface(): ProcessSurface { return processSurface; }

export function listenForProcessSurface(listener: (next: ProcessSurface) => void): () => void {
  const receive = (event: Event) => listener((event as CustomEvent<ProcessSurface>).detail);
  window.addEventListener(SURFACE_EVENT, receive);
  return () => window.removeEventListener(SURFACE_EVENT, receive);
}

// Spec 229 acceptance 6: read-only Design state for later guidance (224/225). It stays in the page beside the
// Process surface: nothing here is sent to the backend or a model, and nothing reads it to act.
export type DesignPanelState = "available" | "empty" | "loading" | "unavailable" | "not_checked";
// `disabled` maps a registered target the owner currently disables to the owner's reason (ADR-061 cluster 4).
export type DesignSurface = Readonly<{
  route_id: "design-project";
  epoch: number;
  workspace_id: string | null;
  example: boolean;
  version_id: string | null;
  version_label: string | null;
  stage: string;
  component_id: string | null;
  component_name: string | null;
  node: string | null;
  panels: Readonly<Record<string, DesignPanelState>>;
  disabled: Readonly<Record<string, string>>;
}>;
const DESIGN_SURFACE_EVENT = "jarvis:design-surface";
const stampDesign = stamper<DesignSurface>();
let designSurface: DesignSurface | null = null;

export function publishDesignSurface(next: Unstamped<DesignSurface> | null): void {
  if (next === null && designSurface !== null) stampDesign.invalidate();
  designSurface = next ? stampDesign(next) : null;
  window.dispatchEvent(new CustomEvent(DESIGN_SURFACE_EVENT, { detail: designSurface }));
}

// Before any project is open the Design landing has no surface of its own; the shell publishes the workspace list's
// state in the same shape so "not yet" (loading) is distinguishable from "not there" (ADR-061 R07).
export function publishWorkspaceListSurface(state: "loading" | "unavailable"): void {
  publishDesignSurface({ route_id: "design-project", workspace_id: null, example: false, version_id: null, version_label: null,
    stage: "", component_id: null, component_name: null, node: null, panels: { workspaces: state }, disabled: {} });
}

export function currentDesignSurface(): DesignSurface | null { return designSurface; }

export function listenForDesignSurface(listener: (next: DesignSurface | null) => void): () => void {
  const receive = (event: Event) => listener((event as CustomEvent<DesignSurface | null>).detail);
  window.addEventListener(DESIGN_SURFACE_EVENT, receive);
  return () => window.removeEventListener(DESIGN_SURFACE_EVENT, receive);
}

export type WorkspaceActionEvent = {
  workspaceId: string; surface: "process" | "bluecad" | "studio"; state: "applied" | "undone";
  draftId?: string | null; resultRevision?: string | null; candidateId?: string | null; childCandidateId?: string | null;
};

export function publishWorkspaceAction(event: WorkspaceActionEvent): void {
  window.dispatchEvent(new CustomEvent("jarvis:workspace-action", { detail: event }));
}
