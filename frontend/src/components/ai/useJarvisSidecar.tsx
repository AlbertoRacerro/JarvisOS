import { useCallback, useContext, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ArrowUp, ChatCircleDots, Info, X } from "@phosphor-icons/react";

import type { KnowledgeContextPreview } from "../../api/knowledgeActions";
import { applyWorkspaceAction, dismissWorkspaceAction, getSurfaceBrief, getWorkspaceAction, undoWorkspaceAction, WorkspaceActionRequestError, type ActionOutcome, type SurfaceRef } from "../../api/workspaceActions";
import { currentProcessSurface, listenForProcessSurface, publishWorkspaceAction } from "../../app/workspaceActionSurface";
import type { StageSelection } from "../../app/selection";
import {
  ThreadsRequestError,
  confirmCloudEscalation,
  draftInteractionEscalation,
  draftRelayEscalation,
  escalateInteraction,
  escalateWithRelay,
  getConversationOptions,
  getRelayStatus,
  listCloudEscalations,
  listRelayRuns,
  submitRelayRun,
  type CloudEscalation,
  type EscalationDraft,
  type RelayEscalationDraft,
  type ConversationRoute,
  type RelayRunRead,
  type RelayStatus,
  listThreads,
  getThread,
  createThread,
  previewThreadContext,
  submitThreadInteraction,
  type ContextPackPreview,
  type ContextSelection,
  type ThreadDetail,
  type ThreadInteraction,
  type ThreadSummary
} from "../../api/threads";
import { publishBubuConversation, publishBubuFailedPrompt, publishBubuRelayEnabled, registerBubuHandlers, type BubuExtras, type BubuTurn } from "../bubu/conversationStore";
import { useGuidedTourActive } from "../bubu/guidedTourActivity";
import { SidecarChrome } from "../shell/ContextualSidecar";
import { ContextMenu, MenuButton, useContextMenu, type ContextMenuItem } from "../ui/ContextMenu";
import JarvisMessageText from "./JarvisMessageText";
import WorkspaceActionCards from "./WorkspaceActionCards";
import { buildSurfaceRef, isToolCallShaped, relayFailureMessage, type ProcessSurface } from "./workspaceActionPresentation";
import { modelDisplayName } from "./modelDisplayName";
import { cloudFailureMessage, formatMoney, sortCreatedChronologically } from "./sidecarPresentation";
import "./JarvisSidecar.css";

const DEFAULT_SELECTION: ContextSelection = {};
const KNOWLEDGE_ROUTES = new Set(["memory-project-basis", "memory-models", "memory-literature"]);
const TERMINAL_FLOW_STATES = new Set(["complete", "partial_terminal", "failed_terminal", "cancelled_terminal"]);
const RESPONDER_STORAGE_KEY = "jarvisos.jarvis.responder";

function productDisplayText(text: string): string {
  return text.replace(/\bBLUECAD\b/g, "BlueCad");
}

function requestId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `jarvis-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

type PendingSubmit = Readonly<{
  workspaceId: string;
  threadId: string;
  requestId: string;
  prompt: string;
  contextEnabled: boolean;
  expectedDigest: string | null;
  routeClass: string;
  knowledgeContext: KnowledgeContextPreview | null;
  surfaceContext: SurfaceRef;
  extras?: BubuExtras;
}>;

type InFlight = Readonly<{ workspaceId: string; threadId: string | null; requestId: string | null; prompt: string; routeClass: string; startedAt: number }>;

function selectionIdentity(selection: StageSelection | null): string {
  if (selection === null) return "none";
  if (selection.kind === "record") {
    return `record:${selection.ref.workspaceId}:${selection.ref.resource}:${selection.ref.recordId}`;
  }
  return `geometry:${selection.viewerSessionId}:${selection.ephemeralObjectId}`;
}

function routeUsable(route: ConversationRoute | undefined): boolean {
  if (!route) return false;
  if (route.execution_class === "synthetic") return true;
  if (route.availability.reason_code === "LLAMACPP_AUTH_REQUIRED") return false;
  return Boolean(route.availability.runtime_reachable && (route.execution_class === "agent" || route.availability.model_installed));
}

function responderLabel(route: ConversationRoute): string {
  if (route.execution_class === "agent") return "Jarvis agent";
  if (route.execution_class === "synthetic") return "Test responder — not AI";
  return `${modelDisplayName(route.model_id)} · direct`;
}

function preferredRoute(routes: ConversationRoute[], current: string): string {
  if (routes.some(route => route.route_class === current && routeUsable(route))) return current;
  let stored: string | null = null;
  try { stored = window.localStorage.getItem(RESPONDER_STORAGE_KEY); } catch { stored = null; }
  const usable = routes.filter(routeUsable);
  return usable.find(route => route.route_class === stored)?.route_class
    ?? usable.find(route => route.execution_class === "agent")?.route_class
    ?? usable.find(route => route.execution_class === "local_compute")?.route_class
    ?? usable[0]?.route_class
    ?? routes.find(route => route.route_class === current)?.route_class
    ?? "";
}

type Readiness = Readonly<{ tone: "ready" | "busy" | "warning" | "down"; label: string; detail: string }>;

function readiness(route: ConversationRoute | undefined, routes: ConversationRoute[], optionsError: boolean, loaded: boolean): Readiness {
  if (optionsError) return { tone: "down", label: "Offline", detail: "The JarvisOS server is not reachable. It may be restarting; this panel retries automatically." };
  if (!loaded) return { tone: "busy", label: "Checking", detail: "Checking which responders are available…" };
  if (!route) return { tone: "down", label: "Unavailable", detail: "No conversation responder is configured on this machine." };
  const llama = routes.find(item => item.route_class === "local:llamacpp");
  if (route.execution_class === "agent") {
    if (!route.availability.runtime_reachable) return { tone: "down", label: "Unavailable", detail: route.availability.message };
    if (llama && !routeUsable(llama)) {
      return llama.availability.reason_code === "LLAMACPP_LOADING"
        ? { tone: "busy", label: "Loading model", detail: `The local model ${modelDisplayName(llama.model_id)} is still loading. Messages will wait for it.` }
        : { tone: "warning", label: "Model unavailable", detail: llama.availability.message };
    }
    return { tone: "ready", label: "Ready", detail: route.availability.model_loaded ? "Jarvis agent is running on the local model." : "Jarvis agent starts with your first message (a few seconds)." };
  }
  if (route.execution_class === "synthetic") return { tone: "warning", label: "Test mode", detail: "Synthetic test responder: replies are canned, not AI answers." };
  if (!routeUsable(route)) return { tone: route.availability.reason_code === "LLAMACPP_LOADING" ? "busy" : "down", label: route.availability.reason_code === "LLAMACPP_LOADING" ? "Loading model" : "Unavailable", detail: route.availability.message };
  return { tone: "ready", label: "Ready", detail: `Answers come directly from the local model ${modelDisplayName(route.model_id)}.` };
}

function relativeTime(iso: string): string {
  const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

function outcomeNotice(interaction: ThreadInteraction): string | null {
  if (interaction.flow_state === "partial_terminal") {
    return interaction.terminal_reason === "output_length_limit"
      ? "This answer stopped at the output limit. You can ask Jarvis to continue."
      : "This answer ended before completion. You can try again or continue from the last visible text.";
  }
  // A failed Hermes agent turn closes its reservation flow as cancelled with reason agent_failed.
  if (interaction.flow_state === "failed_terminal" || interaction.terminal_reason === "agent_failed") {
    if (interaction.terminal_reason?.endsWith("output_budget_exhausted")) return "The model spent its whole output budget before producing a visible answer. Try a shorter or more direct request.";
    return "Jarvis could not complete this request. Check that the selected responder is available, then try again.";
  }
  if (interaction.flow_state === "cancelled_terminal") return "This request was cancelled.";
  return null;
}

function relayRunTone(state: RelayRunRead["state"]): Readiness["tone"] {
  if (state === "queued" || state === "running") return "busy";
  if (state === "completed") return "ready";
  return "down";
}

function EscalateControl({ disabled, onRelay, onApi }: Readonly<{ disabled: boolean; onRelay(): void; onApi(): void }>) {
  const menu = useContextMenu();
  const moreRef = useRef<HTMLButtonElement | null>(null);
  const items: ContextMenuItem[] = [
    { id: "relay", label: "Escalate with Relay", hint: "Subscription-backed cloud agent · no API charge", onSelect: onRelay },
    { id: "api", label: "Escalate with API key…", hint: "Metered provider path with budget and approval gates", onSelect: onApi }
  ];
  return <span className="jarvis-escalate">
    <button type="button" className="jarvis-link-button" disabled={disabled} onClick={onRelay} title="Ask a cloud model through Relay (right-click for more options)" {...menu.targetProps}>Escalate</button>
    <button
      ref={moreRef}
      type="button"
      className="ui-menu-button jarvis-escalate__more"
      aria-label="Escalation options"
      title="Escalation options"
      aria-haspopup="menu"
      aria-expanded={menu.at !== null}
      disabled={disabled}
      onClick={(event) => (menu.at ? menu.close() : menu.openFrom(event.currentTarget))}
    ><span aria-hidden="true">▾</span></button>
    <ContextMenu label="Escalation options" items={items} at={menu.at} onClose={menu.close} triggerRef={moreRef} />
  </span>;
}

function SidecarChromeActions() {
  const chrome = useContext(SidecarChrome);
  if (!chrome) return null;
  return <>
    <button type="button" className="jarvis-icon-button" onClick={chrome.close} aria-label="Close Jarvis" title="Close Jarvis"><X size={15} aria-hidden="true" /></button>
  </>;
}

function ResponderMenu({ items, children, disabled }: Readonly<{ items: ContextMenuItem[]; children: ReactNode; disabled: boolean }>) {
  const chrome = useContext(SidecarChrome);
  const menuItems = [
    ...items,
    ...(chrome ? [{ id: "show-properties", label: "Show properties", onSelect: chrome.toggleProperties }] : [])
  ];
  return <MenuButton label="Responder and send target" items={menuItems} className="jarvis-chip" disabled={disabled}>{children}</MenuButton>;
}

function accessModeLabel(accessMode: "repository" | "derivative"): string {
  return accessMode === "repository" ? "real repository (read-write clone)" : "derivative workspace, no source";
}

function failureMessage(caught: unknown): string {
  if (caught instanceof ThreadsRequestError) {
    if (caught.status === 503) return "Jarvis is temporarily unavailable (the local runtime is busy or starting). Your message is kept — send it again in a moment.";
    return `Jarvis could not complete this request (server error ${caught.status}). Your message is kept; sending it again is safe and will not duplicate it.`;
  }
  return "The JarvisOS server did not answer. Your message is kept; sending it again is safe and will not duplicate it.";
}

export function useJarvisSidecar(
  workspaceId: string | null,
  routeId: string,
  selection: StageSelection | null,
  contextualContent?: ReactNode,
  knowledgeContext?: KnowledgeContextPreview | null,
  pinnedContent?: ReactNode,
  studioModelId: string | null = null
): ReactNode {
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ThreadDetail | null>(null);
  const [preview, setPreview] = useState<ContextPackPreview | null>(null);
  const [projectPackEnabled, setContextEnabled] = useState(false);
  const [routes, setRoutes] = useState<ConversationRoute[]>([]);
  const [processSurface, setProcessSurface] = useState<ProcessSurface>(() => currentProcessSurface());
  const [surfaceBrief, setSurfaceBrief] = useState<Awaited<ReturnType<typeof getSurfaceBrief>> | null>(null);
  const guidedTourActive = useGuidedTourActive();
  const [surfaceBriefRefresh, setSurfaceBriefRefresh] = useState(0);
  const [routesLoaded, setRoutesLoaded] = useState(false);
  const [routeClass, setRouteClass] = useState("");
  const [optionsError, setOptionsError] = useState(false);
  const activeKnowledge = knowledgeContext?.workspace_id === workspaceId && knowledgeContext.route_id === routeId ? knowledgeContext : null;
  const contextEnabled = projectPackEnabled && !activeKnowledge;
  const activeRoute = routes.find(route => route.route_class === routeClass);
  const activeRouteAvailable = routeUsable(activeRoute);

  useEffect(() => {
    let alive = true;
    const refresh = () => void getConversationOptions().then(result => {
      if (!alive) return;
      setRoutes(result.routes);
      setRoutesLoaded(true);
      setOptionsError(false);
      setRouteClass(current => preferredRoute(result.routes, current));
    }).catch(() => { if (alive) setOptionsError(true); });
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { alive = false; window.clearInterval(timer); };
  }, []);

  // Relay availability and the allowed agent list are host configuration, not
  // per-workspace state; fetch once and let the agent select pick up a default.
  useEffect(() => {
    let alive = true;
    void getRelayStatus().then(result => {
      if (!alive) return;
      setRelayStatus(result);
      setRelayAgent(current => current || result.agents[0] || "");
    }).catch(() => { if (alive) setRelayStatus(null); });
    return () => { alive = false; };
  }, []);
  const [previewNonce, setPreviewNonce] = useState(0);
  const [prompt, setPrompt] = useState("");
  const [pending, setPending] = useState<PendingSubmit | null>(null);
  const [inFlight, setInFlight] = useState<InFlight | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [loadingThreads, setLoadingThreads] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cloudResults, setCloudResults] = useState<CloudEscalation[]>([]);
  const [cloudSource, setCloudSource] = useState<string | null>(null);
  const [cloudDraft, setCloudDraft] = useState<EscalationDraft | null>(null);
  const [cloudEditText, setCloudEditText] = useState("");
  const [cloudFamily, setCloudFamily] = useState("");
  const [cloudWorking, setCloudWorking] = useState(false);
  const [cloudPendingStarted, setCloudPendingStarted] = useState<number | null>(null);
  const [relaySource, setRelaySource] = useState<string | null>(null);
  const [relayDraft, setRelayDraft] = useState<RelayEscalationDraft | null>(null);
  const [relayEditText, setRelayEditText] = useState("");
  const [relaySendImages, setRelaySendImages] = useState(true);
  const [relayEscalating, setRelayEscalating] = useState(false);
  const [relayStatus, setRelayStatus] = useState<RelayStatus | null>(null);
  const [relayRuns, setRelayRuns] = useState<RelayRunRead[]>([]);
  const [relayAgent, setRelayAgent] = useState("");
  const [composerTarget, setComposerTarget] = useState<"jarvis" | "relay">("jarvis");
  const [relayAttested, setRelayAttested] = useState(false);
  const [relayWorking, setRelayWorking] = useState(false);
  const submitting = inFlight !== null;
  const workspaceOwner = useRef(0);
  const listOwner = useRef(0);
  const detailOwner = useRef(0);
  const previewOwner = useRef(0);
  const submitOwner = useRef(0);
  const relayEscalationInFlight = useRef(false);
  const selectedThreadRef = useRef<string | null>(null);
  // Spec 229: the workspace the selected thread belongs to. After a project switch the old thread id must not be
  // fetched under the new workspace (it 404s and showed a spurious "could not be loaded" error).
  const threadWorkspaceRef = useRef<string | null>(null);
  const emittedActionIds = useRef(new Set<string>());
  const transcriptRef = useRef<HTMLOListElement | null>(null);
  const selectionKey = useMemo(() => selectionIdentity(selection), [selection]);
  const surfaceContext: SurfaceRef = useMemo(() => buildSurfaceRef(routeId, selection, processSurface, studioModelId), [routeId, selection, processSurface, studioModelId]);
  const surfaceContextKey = JSON.stringify(surfaceContext);
  selectedThreadRef.current = selectedThreadId;

  const publishActionRefresh = (action: ActionOutcome, state: "applied" | "undone") => {
    emittedActionIds.current.add(action.action_id);
    publishWorkspaceAction({ workspaceId: action.workspace_id, surface: action.surface, state, draftId: action.draft_id, resultRevision: action.result_revision, candidateId: action.candidate_id, childCandidateId: action.child_candidate_id });
  };

  const seedEmittedActionIds = (actions: ActionOutcome[]) => {
    for (const action of actions) {
      if (action.state === "applied" || action.state === "undone") emittedActionIds.current.add(action.action_id);
    }
  };

  const updateAction = (action: ActionOutcome) => {
    setDetail(current => current ? { ...current, interactions: current.interactions.map(interaction => ({ ...interaction, actions: (interaction.actions ?? []).map(item => item.action_id === action.action_id ? action : item) })) } : current);
    setRelayRuns(current => current.map(run => ({ ...run, actions: (run.actions ?? []).map(item => item.action_id === action.action_id ? action : item) })));
    if (action.state === "applied" || action.state === "undone") publishActionRefresh(action, action.state);
  };

  const runActionRequest = async (actionId: string, request: (workspaceId: string, actionId: string) => Promise<ActionOutcome>) => {
    if (!workspaceId) return;
    try { updateAction(await request(workspaceId, actionId)); }
    catch (caught) {
      if (caught instanceof WorkspaceActionRequestError && caught.status === 409) {
        try { updateAction(await getWorkspaceAction(workspaceId, actionId)); return; }
        catch { /* Show the original conflict below when the canonical record is unavailable. */ }
      }
      setError(caught instanceof Error ? caught.message : "Workspace action could not be updated.");
    }
  };
  const applyAction = async (actionId: string) => runActionRequest(actionId, applyWorkspaceAction);
  const dismissAction = async (actionId: string) => runActionRequest(actionId, dismissWorkspaceAction);
  const undoAction = async (actionId: string) => runActionRequest(actionId, undoWorkspaceAction);

  useEffect(() => {
    const actions = [...(detail?.interactions.flatMap(item => item.actions ?? []) ?? []), ...relayRuns.flatMap(run => run.actions ?? [])];
    for (const action of actions) {
      if (action.state === "applied" && !emittedActionIds.current.has(action.action_id)) publishActionRefresh(action, "applied");
    }
  }, [detail, relayRuns]);

  useEffect(() => listenForProcessSurface(setProcessSurface), []);

  useEffect(() => {
    let active = true;
    setSurfaceBrief(null);
    if (!workspaceId || guidedTourActive) return;
    const timer = window.setTimeout(() => {
      void getSurfaceBrief(workspaceId, surfaceContext).then((brief) => { if (active) setSurfaceBrief(brief); }).catch(() => { if (active) setSurfaceBrief(null); });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [surfaceContextKey, workspaceId, surfaceBriefRefresh, guidedTourActive]);

  useEffect(() => {
    const refreshBrief = (event: Event) => {
      const detail = (event as CustomEvent<{ workspaceId?: string; surface?: string; draftId?: string | null }>).detail;
      const surface = routeId === "design-process" ? "process" : routeId === "design-bluecad" ? "bluecad" : routeId === "design-studio" ? "studio" : null;
      if (detail?.workspaceId !== workspaceId || detail.surface !== surface) return;
      if (detail.draftId && surfaceContext.draft_id && detail.draftId !== surfaceContext.draft_id) return;
      setSurfaceBriefRefresh(current => current + 1);
    };
    window.addEventListener("jarvis:workspace-action", refreshBrief);
    return () => window.removeEventListener("jarvis:workspace-action", refreshBrief);
  }, [routeId, surfaceContext.draft_id, workspaceId]);

  useEffect(() => {
    workspaceOwner.current += 1;
    listOwner.current += 1;
    detailOwner.current += 1;
    previewOwner.current += 1;
    submitOwner.current += 1;
    const workspaceToken = workspaceOwner.current;
    const listToken = listOwner.current;
    threadWorkspaceRef.current = null;
    setThreads([]);
    setSelectedThreadId(null);
    setDetail(null);
    setPreview(null);
    setPending(null);
    setInFlight(null);
    setPrompt("");
    setLoadingThreads(false);
    setPreviewLoading(false);
    setSubmitting(false);
    setError(null);
    setCloudResults([]);
    setCloudSource(null);
    setCloudDraft(null);
    setRelaySource(null);
    setRelayDraft(null);
    setRelayRuns([]);
    if (!workspaceId) return;

    setLoadingThreads(true);
    void listThreads(workspaceId)
      .then((nextThreads) => {
        if (workspaceOwner.current !== workspaceToken || listOwner.current !== listToken) return;
        setThreads(nextThreads);
        threadWorkspaceRef.current = workspaceId;
        setSelectedThreadId(nextThreads[0]?.id ?? null);
      })
      .catch(() => {
        if (workspaceOwner.current === workspaceToken && listOwner.current === listToken) {
          setError("Your Jarvis conversations could not be loaded. They are safe on the server; reopen this panel to retry.");
        }
      })
      .finally(() => {
        if (workspaceOwner.current === workspaceToken && listOwner.current === listToken) {
          setLoadingThreads(false);
        }
      });
  }, [workspaceId]);

  const loadDetail = useCallback((threadId: string, quiet: boolean) => {
    if (!workspaceId) return;
    const token = ++detailOwner.current;
    const workspaceToken = workspaceOwner.current;
    void getThread(workspaceId, threadId)
      .then((next) => {
        if (detailOwner.current !== token || workspaceOwner.current !== workspaceToken || selectedThreadRef.current !== threadId) return;
        seedEmittedActionIds(next.interactions.flatMap(item => item.actions ?? []));
        setDetail(next);
      })
      .catch(() => {
        if (!quiet && detailOwner.current === token && workspaceOwner.current === workspaceToken) {
          setError("This conversation could not be loaded. It is safe on the server; select it again to retry.");
        }
      });
  }, [workspaceId]);

  useEffect(() => {
    setDetail(null);
    setCloudResults([]);
    setCloudSource(null);
    setCloudDraft(null);
    setRelaySource(null);
    setRelayDraft(null);
    setRelayRuns([]);
    if (!workspaceId || !selectedThreadId || threadWorkspaceRef.current !== workspaceId) return;
    let active = true;
    loadDetail(selectedThreadId, false);
    void listCloudEscalations(workspaceId, selectedThreadId).then(result => {
      if (active) setCloudResults(result);
    }).catch(() => {});
    // Relay history stays visible even if the gateway was turned off later.
    void listRelayRuns(workspaceId, selectedThreadId).then(result => {
      if (active) {
        seedEmittedActionIds(result.flatMap(run => run.actions ?? []));
        setRelayRuns(result);
      }
    }).catch(() => {});
    return () => { active = false; };
  }, [workspaceId, selectedThreadId, loadDetail]);

  // Later turns in a thread continue the same Relay session; keep the run list
  // current while anything is still queued or running, then stop polling.
  const relayRunsActive = relayRuns.some(run => run.state === "queued" || run.state === "running");
  useEffect(() => {
    if (!workspaceId || !selectedThreadId || !relayRunsActive) return;
    const targetThread = selectedThreadId;
    const timer = window.setInterval(() => {
      void listRelayRuns(workspaceId, targetThread).then(result => {
        if (selectedThreadRef.current === targetThread) setRelayRuns(result);
      }).catch(() => {});
    }, 3000);
    return () => window.clearInterval(timer);
  }, [workspaceId, selectedThreadId, relayRunsActive]);

  const sendCloud = async () => {
    if (!workspaceId || !selectedThreadId || !cloudSource || !cloudDraft || cloudWorking) return;
    if (cloudDraft.status !== "ready" || !cloudDraft.text_digest || cloudEditText !== cloudDraft.text) return;
    const targetThread = selectedThreadId;
    setCloudWorking(true);
    setCloudPendingStarted(Date.now());
    setError(null);
    try {
      const result = await escalateInteraction(workspaceId, targetThread, cloudSource,
        cloudDraft.text, cloudDraft.text_digest, cloudFamily || undefined);
      if (selectedThreadRef.current === targetThread) {
        setCloudResults(current => [result, ...current.filter(item => item.id !== result.id)]);
        setCloudSource(null);
        setCloudDraft(null);
      }
    } catch (caught) {
      setError(caught instanceof ThreadsRequestError
        ? caught.detail ?? `Cloud escalation was not dispatched (${caught.status}). Check the provider and budget settings.`
        : "Cloud escalation could not be completed. Check its status before retrying.");
    } finally { setCloudWorking(false); setCloudPendingStarted(null); }
  };

  const prepareEscalation = async (family?: string, sourceInteraction = cloudSource, editedText?: string) => {
    if (!workspaceId || !selectedThreadId || !sourceInteraction || cloudWorking) return;
    setCloudWorking(true);
    setError(null);
    try {
      const draft = await draftInteractionEscalation(workspaceId, selectedThreadId, sourceInteraction, family, editedText);
      setCloudDraft(draft);
      setCloudEditText(draft.text);
      setCloudFamily(family ?? "");
    } catch (caught) {
      setError(caught instanceof ThreadsRequestError ? caught.detail ?? "This turn cannot be escalated." : "Jarvis could not prepare the cloud request.");
    } finally { setCloudWorking(false); }
  };

  const confirmCloud = async (escalationId: string) => {
    if (!workspaceId || !selectedThreadId || cloudWorking) return;
    const targetThread = selectedThreadId;
    setCloudWorking(true);
    setError(null);
    try {
      const result = await confirmCloudEscalation(workspaceId, targetThread, escalationId);
      if (selectedThreadRef.current === targetThread) {
        setCloudResults(current => current.map(item => item.id === result.id ? result : item));
      }
    } catch {
      setError("The cloud confirmation could not be used. The approved derivative or provider policy may have changed.");
    } finally { setCloudWorking(false); }
  };

  // Spec 161: the default escalation goes through Relay; the API path is a deliberate choice.
  const prepareRelayEscalation = async (sourceInteraction: string, editedText?: string) => {
    if (!workspaceId || !selectedThreadId || relayEscalationInFlight.current) return;
    relayEscalationInFlight.current = true;
    setCloudSource(null);
    setCloudDraft(null);
    setRelaySource(sourceInteraction);
    if (editedText === undefined) { setRelayDraft(null); setRelaySendImages(true); }
    setRelayEscalating(true);
    setError(null);
    try {
      const draft = await draftRelayEscalation(workspaceId, selectedThreadId, sourceInteraction, editedText, surfaceContext);
      setRelayDraft(draft);
      setRelayEditText(draft.text);
    } catch (caught) {
      setError(caught instanceof ThreadsRequestError ? caught.detail ?? "This answer cannot be escalated through Relay." : "Jarvis could not prepare the Relay request.");
    } finally { relayEscalationInFlight.current = false; setRelayEscalating(false); }
  };

  const sendRelayEscalation = async () => {
    if (!workspaceId || !selectedThreadId || !relaySource || !relayDraft || relayEscalationInFlight.current) return;
    if (relayDraft.status !== "ready" || !relayDraft.text_digest || relayEditText !== relayDraft.text) return;
    relayEscalationInFlight.current = true;
    const targetThread = selectedThreadId;
    setRelayEscalating(true);
    setError(null);
    try {
      const run = await escalateWithRelay(workspaceId, targetThread, relaySource, relayDraft.text, relayDraft.text_digest, relaySendImages ? relayDraft.image_digests ?? [] : []);
      if (selectedThreadRef.current === targetThread) {
        setRelayRuns(current => [...current.filter(item => item.id !== run.id), run]);
        setRelaySource(null);
        setRelayDraft(null);
      }
    } catch (caught) {
      setError(caught instanceof ThreadsRequestError ? caught.detail ?? "The Relay request was not sent." : "The Relay request could not be sent. Nothing was sent to a paid API.");
    } finally { relayEscalationInFlight.current = false; setRelayEscalating(false); }
  };

  const startApiEscalation = (sourceInteraction: string) => {
    setRelaySource(null);
    setRelayDraft(null);
    setCloudSource(sourceInteraction);
    setCloudDraft(null);
    setCloudEditText("");
    void prepareEscalation(undefined, sourceInteraction);
  };

  const sendRelay = async () => {
    const relayText = prompt.trim();
    if (!workspaceId || !relayAgent || !relayText || !relayAttested || relayWorking) return;
    setRelayWorking(true);
    setError(null);
    try {
      let targetThread = selectedThreadId;
      if (!targetThread) {
        // A new conversation has no thread until its first submission, as with ordinary messages.
        const created = await createThread(workspaceId, relayText.slice(0, 70));
        targetThread = created.id;
        setThreads((current) => [created, ...current]);
        selectedThreadRef.current = created.id;
        threadWorkspaceRef.current = workspaceId;
        setSelectedThreadId(created.id);
      }
      const result = await submitRelayRun(workspaceId, targetThread, {
        prompt: relayText, agent: relayAgent, cloud_safe_attested: relayAttested
      });
      if (selectedThreadRef.current === targetThread) {
        setRelayRuns(current => [...current.filter(item => item.id !== result.id), result]);
        setPrompt("");
        setRelayAttested(false);
      }
    } catch (caught) {
      setError(caught instanceof ThreadsRequestError && caught.status === 409
        ? "A Relay run is already in progress for this conversation. Wait for it to finish, then send again."
        : "The Relay run could not be started. Check that Relay is enabled and the agent is on the allowed list.");
    } finally { setRelayWorking(false); }
  };

  // A turn keeps running on the server while you navigate, refresh or switch
  // conversations; poll the selected conversation until it settles.
  const detailRunning = Boolean(detail?.interactions.some((interaction) => !TERMINAL_FLOW_STATES.has(interaction.flow_state)));
  const inFlightHere = Boolean(inFlight && inFlight.threadId === selectedThreadId);
  useEffect(() => {
    if (!selectedThreadId || !(detailRunning || inFlightHere)) return;
    const timer = window.setInterval(() => loadDetail(selectedThreadId, true), 2500);
    return () => window.clearInterval(timer);
  }, [selectedThreadId, detailRunning, inFlightHere, loadDetail]);

  useEffect(() => {
    if (!inFlight && !detailRunning && !relayRunsActive && cloudPendingStarted === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [inFlight, detailRunning, relayRunsActive, cloudPendingStarted]);

  useEffect(() => {
    const list = transcriptRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [detail, inFlightHere, cloudDraft, cloudSource, cloudResults, relayRuns, relayDraft]);

  useEffect(() => {
    const token = ++previewOwner.current;
    const workspaceToken = workspaceOwner.current;
    setPreview(null);
    setPreviewLoading(false);
    if (!workspaceId || !contextEnabled) return;
    setPreviewLoading(true);
    void previewThreadContext(workspaceId, DEFAULT_SELECTION)
      .then((nextPreview) => {
        if (previewOwner.current !== token || workspaceOwner.current !== workspaceToken) return;
        setPreview(nextPreview);
      })
      .catch(() => {
        if (previewOwner.current === token && workspaceOwner.current === workspaceToken) {
          setError("The project context preview could not be built. Turn project context off, or try again.");
        }
      })
      .finally(() => {
        if (previewOwner.current === token && workspaceOwner.current === workspaceToken) {
          setPreviewLoading(false);
        }
      });
  }, [workspaceId, contextEnabled, routeId, selectionKey, previewNonce]);

  function setSubmitting(value: false) {
    if (!value) setInFlight(null);
  }

  const selectThread = (threadId: string | null) => {
    if (threadId === selectedThreadId) return;
    detailOwner.current += 1;
    submitOwner.current += 1;
    threadWorkspaceRef.current = workspaceId;
    setSelectedThreadId(threadId);
    setDetail(null);
    setPending(null);
    setError(null);
  };

  const chooseResponder = (next: string) => {
    setRouteClass(next);
    setPending(null);
    setError(null);
    try { window.localStorage.setItem(RESPONDER_STORAGE_KEY, next); } catch { /* preference only */ }
  };

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (composerTarget === "relay") { await sendRelay(); return; }
    await sendPrompt(prompt.trim());
  };

  // `bubble` marks a submit from Bubu's speech bubble: same thread, route class and
  // context handling, but it leaves the panel's own draft alone and reports failures
  // back through the shared conversation store instead.
  const sendPrompt = async (text: string, bubble?: BubuExtras) => {
    const fromBubble = bubble !== undefined;
    if (fromBubble) publishBubuFailedPrompt(null);
    // Wait for thread ownership to settle before auto-creating or dispatching.
    // Otherwise a delayed initial list (or explicit creation) can select a
    // different thread while this submit publishes its transcript.
    const unavailable = !activeRoute || !activeRouteAvailable || activeKnowledge?.route_id === "memory-project-basis";
    if (fromBubble && workspaceId && text && (loadingThreads || submitting || unavailable)) {
      setError(loadingThreads || submitting ? "Jarvis is still busy. Try again in a moment." : "Jarvis is not available right now.");
      publishBubuFailedPrompt(text);
    }
    if (!workspaceId || !text || loadingThreads || submitting || unavailable) return;
    const workspaceToken = workspaceOwner.current;
    const creationToken = ++submitOwner.current;
    setError(null);
    setInFlight({ workspaceId, threadId: selectedThreadId, requestId: null, prompt: text, routeClass, startedAt: Date.now() });
    let threadId = selectedThreadId;
    if (!threadId) {
      try {
        const created = await createThread(workspaceId, text.slice(0, 70));
        if (workspaceOwner.current !== workspaceToken || submitOwner.current !== creationToken) return;
        threadId = created.id;
        setThreads((current) => [created, ...current]);
        selectedThreadRef.current = created.id;
        threadWorkspaceRef.current = workspaceId;
        setSelectedThreadId(created.id);
      } catch (caught) {
        if (workspaceOwner.current !== workspaceToken || submitOwner.current !== creationToken) return;
        setError(failureMessage(caught));
        if (fromBubble) publishBubuFailedPrompt(text);
        setSubmitting(false);
        return;
      }
    }

    const reusable = pending
      && pending.workspaceId === workspaceId
      && pending.threadId === threadId
      && pending.prompt === text
      && pending.contextEnabled === contextEnabled
      && pending.routeClass === routeClass
      && JSON.stringify(pending.surfaceContext) === surfaceContextKey
      && pending.knowledgeContext?.context_digest === activeKnowledge?.context_digest;
    const currentDigest = contextEnabled
      ? reusable
        ? pending.expectedDigest
        : preview?.context_digest ?? null
      : null;
    if (contextEnabled && !currentDigest) {
      setSubmitting(false);
      setError("Project context is empty or out of date. Refresh it, or turn project context off.");
      if (fromBubble) publishBubuFailedPrompt(text);
      return;
    }

    const captured: PendingSubmit = reusable
      ? pending
      : {
          workspaceId,
          threadId,
          requestId: requestId(),
          prompt: text,
          contextEnabled,
          expectedDigest: currentDigest,
          routeClass,
          knowledgeContext: activeKnowledge,
          surfaceContext,
          extras: bubble
        };
    setPending(captured);
    setInFlight({ workspaceId, threadId, requestId: captured.requestId, prompt: text, routeClass, startedAt: Date.now() });
    if (!fromBubble) setPrompt("");
    try {
      await submitThreadInteraction(
        captured.workspaceId,
        captured.threadId,
        captured.requestId,
        captured.prompt,
        captured.contextEnabled && captured.expectedDigest
          ? { selection: DEFAULT_SELECTION, expectedDigest: captured.expectedDigest }
          : undefined,
        { routeClass: captured.routeClass, knowledgeContext: captured.knowledgeContext, surfaceContext: captured.surfaceContext, ui_context: captured.extras?.ui_context, images: captured.extras?.images }
      );
      if (workspaceOwner.current !== workspaceToken) return;
      setPending(null);
      setThreads((current) => {
        const hit = current.find((item) => item.id === captured.threadId);
        return hit ? [{ ...hit, last_activity_at: new Date().toISOString() }, ...current.filter((item) => item.id !== captured.threadId)] : current;
      });
      // Publish only into the conversation this turn belongs to.
      if (selectedThreadRef.current === captured.threadId) loadDetail(captured.threadId, false);
      if (captured.contextEnabled) setPreviewNonce((current) => current + 1);
    } catch (caught) {
      if (workspaceOwner.current !== workspaceToken) return;
      if (fromBubble) publishBubuFailedPrompt(captured.prompt);
      else setPrompt((current) => current || captured.prompt);
      if (caught instanceof ThreadsRequestError && caught.status === 409 && (captured.contextEnabled || captured.knowledgeContext)) {
        setPending(null);
        setError("The selected context changed while you were writing. Remove it and add it again, then send.");
        setPreviewNonce((current) => current + 1);
      } else {
        setError(failureMessage(caught));
      }
      if (selectedThreadRef.current === captured.threadId) loadDetail(captured.threadId, true);
    } finally {
      if (workspaceOwner.current === workspaceToken) setSubmitting(false);
    }
  };

  const pendingRetryReady = Boolean(
    pending
      && pending.workspaceId === workspaceId
      && pending.threadId === selectedThreadId
      && pending.prompt === prompt.trim()
      && pending.contextEnabled === contextEnabled
      && pending.routeClass === routeClass
      && JSON.stringify(pending.surfaceContext) === surfaceContextKey
      && pending.knowledgeContext?.context_digest === activeKnowledge?.context_digest
      && (!contextEnabled || pending.expectedDigest)
  );
  const basisDiscussionBlocked = activeKnowledge?.route_id === "memory-project-basis";
  const contextReady = !basisDiscussionBlocked && (!contextEnabled || pendingRetryReady || Boolean(preview?.context_digest));
  const stageContextClassName = KNOWLEDGE_ROUTES.has(routeId)
    ? "jarvis-sidecar__stage-context jarvis-sidecar__stage-context--visible"
    : "jarvis-sidecar__stage-context";
  const state = readiness(activeRoute, routes, optionsError, routesLoaded);
  const usableRoutes = routes.filter(routeUsable);
  const unavailableRoutes = routes.filter((route) => !routeUsable(route));
  const serverHasInFlight = Boolean(inFlight?.requestId && detail?.interactions.some((interaction) => interaction.request_id === inFlight.requestId));
  const showOptimistic = inFlight && inFlight.workspaceId === workspaceId && (inFlight.threadId === selectedThreadId || inFlight.threadId === null) && !serverHasInFlight;
  const hermesCold = activeRoute?.execution_class === "agent" && !activeRoute.availability.model_loaded;

  // Bubu's speech bubble shows the tail of the same conversation through a tiny external store.
  const optimisticVisible = Boolean(showOptimistic);
  const optimisticPrompt = inFlight?.prompt ?? "";
  const bubuTurns = useMemo<BubuTurn[]>(() => {
    const turns: BubuTurn[] = [];
    for (const interaction of detail?.interactions ?? []) {
      const text = interaction.assistant_text;
      turns.push({ id: `${interaction.id}-user`, role: "user", text: interaction.user_text });
      if (!TERMINAL_FLOW_STATES.has(interaction.flow_state)) turns.push({ id: interaction.id, role: "jarvis", text: "", pending: true });
      else if (text) turns.push({ id: interaction.id, role: "jarvis", text: isToolCallShaped(text) ? "Jarvis could not turn this into a valid change, so nothing was changed. Try rephrasing." : productDisplayText(text), note: interaction.images_unsupported ? "This model can't see images; it read the page instead." : undefined });
      else { const notice = outcomeNotice(interaction); if (notice) turns.push({ id: interaction.id, role: "jarvis", text: notice, failed: true }); }
    }
    if (optimisticVisible) {
      turns.push({ id: "in-flight-user", role: "user", text: optimisticPrompt });
      turns.push({ id: "in-flight", role: "jarvis", text: "", pending: true });
    }
    return turns;
  }, [detail, optimisticVisible, optimisticPrompt]);
  const bubuPending = inFlight !== null || detailRunning;
  const bubuHandlers = useRef({ submit: async (_prompt: string, _extras?: BubuExtras) => {}, sleep: () => {}, askClaude: (_turnId: string) => {} });
  bubuHandlers.current = {
    submit: (promptText, extras) => sendPrompt(promptText.trim(), extras ?? {}),
    sleep: () => { publishBubuFailedPrompt(null); setError(null); selectThread(null); },
    // Bubu's "Ask Claude": the same reviewed Relay escalation as the panel's Escalate, started from a bubble turn.
    askClaude: turnId => { void prepareRelayEscalation(turnId); }
  };
  useEffect(() => registerBubuHandlers({ submit: (promptText, extras) => bubuHandlers.current.submit(promptText, extras), sleep: () => bubuHandlers.current.sleep(), askClaude: turnId => bubuHandlers.current.askClaude(turnId) }), []);
  useEffect(() => { publishBubuRelayEnabled(Boolean(relayStatus?.enabled)); }, [relayStatus?.enabled]);
  useEffect(() => { publishBubuConversation({ ready: Boolean(workspaceId), turns: bubuTurns, pending: bubuPending, error }); }, [workspaceId, bubuTurns, bubuPending, error]);
  const sendDisabledReason = !workspaceId ? "Select a workspace to talk to Jarvis."
    : !activeRouteAvailable ? state.detail
    : basisDiscussionBlocked ? null
    : null;
  const contextSummary = activeKnowledge ? `${activeKnowledge.included_count} selected record${activeKnowledge.included_count === 1 ? "" : "s"} attached`
    : contextEnabled ? "Project context attached" : "No project context";
  const workingLabel = (startedAt: number, route: string) => {
    const seconds = Math.max(0, Math.round((now - startedAt) / 1000));
    const agent = routes.find((item) => item.route_class === route)?.execution_class === "agent";
    return `${agent ? "Jarvis agent is working" : "Jarvis is thinking"} · ${seconds}s`;
  };
  const composerLabel = contextEnabled ? pendingRetryReady ? "Retry with original context" : "Send with inspected context" : activeKnowledge ? "Send with selected context" : "Send without project context";

  const elapsedSince = (iso: string) => Math.max(0, Math.floor((now - Date.parse(iso)) / 1000));
  const lastInteractionId = detail?.interactions[detail.interactions.length - 1]?.id;

  const renderRelayTurn = (run: RelayRunRead) => {
    const name = modelDisplayName(run.model, run.agent);
    const pendingRun = run.state === "queued" || run.state === "running";
    const failed = run.state === "failed" || run.state === "denied";
    const cleanResult = (run.result_text ?? "").replace(/```jarvis-actions\s*[\s\S]*?```/gi, "").trim();
    return <li key={`relay-${run.id}`} className="jarvis-turn jarvis-turn--followup">
      <div className={`jarvis-bubble jarvis-bubble--jarvis${pendingRun ? " is-working" : ""}${failed ? " is-failed" : ""}`}>
        <div className="jarvis-bubble__meta"><span className="jarvis-bubble__author">{name}</span><span>via Relay · subscription</span></div>
        {pendingRun ? <p className="jarvis-working" role="status"><span className="jarvis-working__dots" aria-hidden="true"><i /><i /><i /></span>Waiting for {name} via Relay… · {elapsedSince(run.started_at ?? run.created_at)}s</p> : null}
        {cleanResult && !failed ? <JarvisMessageText text={cleanResult} /> : null}
        <WorkspaceActionCards actions={run.actions ?? []} onApply={applyAction} onDismiss={dismissAction} onUndo={undoAction} />
        {failed ? <p className="jarvis-bubble__notice" role="status">Relay could not complete this request: {relayFailureMessage(run.reason_code, run.result_text)}. Nothing was sent to a paid API.</p> : null}
        {run.state === "completed" && !(run.actions ?? []).some(action => action.state === "applied") ? <p className="jarvis-bubble__advisory">Advisory answer · no project change was applied.</p> : null}
        <div className="jarvis-bubble__actions">
          {failed && run.source_interaction_id ? <button type="button" className="jarvis-link-button" disabled={cloudWorking} onClick={() => startApiEscalation(run.source_interaction_id!)}>Escalate with API key…</button> : null}
          {!pendingRun ? <p className="jarvis-bubble__metadata">{name} · Relay ({run.agent}) · subscription, no API charge{run.finished_at && run.started_at ? ` · ${((Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000).toFixed(0)}s` : ""}</p> : null}
          <details className="jarvis-bubble__details"><summary aria-label="Show Relay run information"><Info size={13} aria-hidden="true" /> Info</summary><dl><div><dt>Agent</dt><dd>{run.agent}</dd></div><div><dt>Model</dt><dd>{run.model ?? "agent default"}</dd></div><div><dt>State</dt><dd>{run.state}{run.reason_code ? ` · ${run.reason_code}` : ""}{run.stop_reason ? ` · ${run.stop_reason}` : ""}</dd></div><div><dt>Access</dt><dd>{accessModeLabel(run.access_mode)} · level {run.repository_level}</dd></div><div><dt>Turn</dt><dd>{run.turn_index}</dd></div><div><dt>Run</dt><dd><code>{run.id}</code></dd></div></dl>{run.workspace_path ? <p>Workspace <code>{run.workspace_path}</code> · head {run.head_commit ?? "unknown"}</p> : null}{run.change_summary ? <pre className="jarvis-relay-result">{run.change_summary}</pre> : null}</details>
        </div>
      </div>
    </li>;
  };

  const renderCloudTurn = (item: CloudEscalation) => <li key={`cloud-${item.id}`} className="jarvis-turn jarvis-turn--followup">
    <div className={`jarvis-bubble jarvis-bubble--jarvis${item.state === "failed" ? " is-failed" : item.response_text ? "" : " is-working"}`}>
      <div className="jarvis-bubble__meta"><span className="jarvis-bubble__author">{modelDisplayName(item.model_id)}</span><span>cloud API · {item.state}</span></div>
      {item.response_text && item.state !== "failed" ? <JarvisMessageText text={item.response_text} /> : item.state === "failed" || item.reason_code ? <p className="jarvis-bubble__notice" role="status">{cloudFailureMessage(item.reason_code)}</p> : <p>Waiting for cloud model…</p>}
      <p className="jarvis-bubble__advisory">Advisory only · no local action was applied.</p>
      <div className="jarvis-bubble__actions">
        {item.state === "confirmation_required" ? <button type="button" disabled={cloudWorking} onClick={() => void confirmCloud(item.id)}>Approve packet</button> : null}
        <p className="jarvis-bubble__metadata">{modelDisplayName(item.model_id)} · API · maximum {formatMoney(item.projected_cost_usd, "USD")} · actual {formatMoney(item.accounted_cost_eur, "EUR")}</p>
        <details className="jarvis-bubble__details"><summary aria-label="Show cloud provenance"><Info size={13} aria-hidden="true" /> Info</summary><p>{item.provider_id}/{item.model_id} · {item.task_family} · tier {item.quality_tier}</p><p>State: {item.state}{item.reason_code ? ` · ${item.reason_code}` : ""}</p><p>Tokens: {item.actual_input_tokens ?? "unknown"} in · {item.actual_output_tokens ?? "unknown"} out · derivative {item.derivative_digest} · packet {item.egress_packet_digest ?? "none"}</p></details>
      </div>
    </div>
  </li>;

  const renderRelayEscalationCard = () => <li key="relay-escalation" className="jarvis-turn jarvis-turn--followup">
    <section className="jarvis-escalation-card" aria-label="Relay escalation">
      <strong>{relayDraft?.agent ? `Ask ${modelDisplayName(relayDraft.model, relayDraft.agent)} via Relay` : "Ask a cloud model via Relay"}</strong>
      <p className="jarvis-escalation-card__note">Subscription-backed · no API charge. Only the approved text below leaves this conversation; the answer comes back here as advice.</p>
      {!relayDraft ? <p role="status">{relayEscalating ? "Checking the text and Relay availability…" : error ?? "Could not prepare this request."}</p> : <>
        {relayDraft.reason ? <p className={relayDraft.status === "ready" ? undefined : "jarvis-bubble__notice"}>{relayDraft.reason}</p> : null}
        {relayDraft.status !== "refused" ? <label className="jarvis-sidecar__field"><span>Exact text to send</span><textarea value={relayEditText} rows={4} onChange={event => { setRelayEditText(event.target.value); setRelayDraft({ ...relayDraft, status: relayDraft.status === "unavailable" ? "unavailable" : "edit_required", text_digest: null }); }} /></label> : null}
        {relayDraft.status !== "refused" && (relayDraft.image_count ?? 0) > 0 ? <>
          <label className="jarvis-sidecar__toggle"><input type="checkbox" checked={relaySendImages} disabled={relayEscalating} onChange={event => setRelaySendImages(event.target.checked)} />Send {relayDraft.image_count} screenshot{relayDraft.image_count === 1 ? "" : "s"} to Claude</label>
          <p className="jarvis-escalation-card__note">Screenshots are not screened like text.</p>
        </> : null}
        <div className="jarvis-escalation-card__actions">
          {relayDraft.status === "ready" && relayDraft.text_digest && relayEditText === relayDraft.text
            ? <button type="button" className="jarvis-primary-button" disabled={relayEscalating} onClick={() => void sendRelayEscalation()}>{relayEscalating ? "Sending…" : "Approve and send"}</button>
            : relayDraft.status !== "refused" && relayDraft.status !== "unavailable" && relayEditText.trim()
              ? <button type="button" className="jarvis-primary-button" disabled={relayEscalating} onClick={() => void prepareRelayEscalation(relaySource!, relayEditText)}>{relayEscalating ? "Checking…" : "Review edited text"}</button>
              : null}
          {relayDraft.status === "unavailable" ? <button type="button" className="jarvis-link-button" onClick={() => startApiEscalation(relaySource!)}>Escalate with API key…</button> : null}
          <button type="button" className="jarvis-link-button" onClick={() => { setRelaySource(null); setRelayDraft(null); }}>Cancel</button>
        </div>
      </>}
    </section>
  </li>;

  const renderApiEscalationCard = () => <li key="api-escalation" className="jarvis-turn jarvis-turn--followup">
    <section className="jarvis-escalation-card" aria-label="Governed cloud escalation">
      <strong>Review cloud request · API key</strong>
      {cloudPendingStarted !== null ? <p className="jarvis-working" role="status"><span className="jarvis-working__dots" aria-hidden="true"><i /><i /><i /></span>Checking cloud request… · {Math.max(0, Math.floor((now - cloudPendingStarted) / 1000))}s</p> : null}
      <p className="jarvis-escalation-card__note">Metered provider path. Only the approved text below can leave this conversation.</p>
      {!cloudDraft ? <p role="status">{cloudWorking ? "Checking provider, cost and sensitivity…" : error ?? "Could not prepare this cloud request."}</p> : <>
        <p>{cloudDraft.reason}</p>
        <label className="jarvis-sidecar__field"><span>Exact text for the cloud model</span><textarea value={cloudEditText} onChange={event => { setCloudEditText(event.target.value); setCloudDraft({ ...cloudDraft, status: "edit_required", text_digest: null }); }} rows={4} /></label>
        <p>{cloudDraft.candidate ? `${modelDisplayName(cloudDraft.candidate.model_id)} (${cloudDraft.candidate.provider_id}) · maximum $${cloudDraft.candidate.max_cost_usd} · ${cloudDraft.task_family}${cloudDraft.task_family_inferred ? " (inferred)" : ""}` : `Task type: ${cloudDraft.task_family}`}</p>
        <details><summary>Advanced · task type</summary><label className="jarvis-sidecar__field"><span>Override task type</span><select value={cloudFamily || cloudDraft.task_family} onChange={event => void prepareEscalation(event.target.value, cloudSource, cloudEditText)}>{cloudDraft.family_options.map(family => <option key={family}>{family}</option>)}</select></label></details>
        <div className="jarvis-escalation-card__actions">
          {cloudDraft.status === "ready" && cloudDraft.text_digest && cloudEditText === cloudDraft.text ? <button type="button" className="jarvis-primary-button" disabled={cloudWorking} onClick={() => void sendCloud()}>{cloudWorking ? "Checking and sending…" : "Approve text and escalate"}</button> : cloudDraft.status !== "refused" && cloudEditText.trim() ? <button type="button" className="jarvis-primary-button" disabled={cloudWorking} onClick={() => void prepareEscalation(cloudFamily || undefined, cloudSource, cloudEditText)}>{cloudWorking ? "Checking…" : "Review edited text"}</button> : null}
          <button type="button" className="jarvis-link-button" onClick={() => { setCloudSource(null); setCloudDraft(null); }}>Cancel</button>
        </div>
      </>}
    </section>
  </li>;

  const renderInteraction = (interaction: ThreadInteraction) => {
    const running = !TERMINAL_FLOW_STATES.has(interaction.flow_state);
    const notice = outcomeNotice(interaction);
    const viaAgent = interaction.execution_class === "agent";
    const synthetic = interaction.execution_class === "synthetic";
    const elapsed = interaction.elapsed_ms ?? interaction.latency_ms;
    const providerKind = interaction.execution_class === "external_provider" ? "cloud" : interaction.execution_class === "synthetic" ? "synthetic" : "local";
    const modelName = interaction.model_id ? modelDisplayName(interaction.model_id) : null;
    const unsafeAssistantText = isToolCallShaped(interaction.assistant_text);
    const metadata = [modelName ?? "Model unknown", viaAgent ? "Jarvis agent" : null, providerKind, elapsed != null ? `${(elapsed / 1000).toFixed(1)}s` : null, interaction.cost_estimate_usd ? `$${interaction.cost_estimate_usd.toFixed(4)}` : null].filter(Boolean).join(" · ");
    const escalatable = !running && Boolean(interaction.assistant_text) && (interaction.execution_class === "local_compute" || viaAgent);
    const followups = sortCreatedChronologically([
      ...cloudResults.filter(item => item.source_interaction_id === interaction.id),
      ...relayRuns.filter(run => run.source_interaction_id === interaction.id || (!run.source_interaction_id && interaction.id === lastInteractionId))
    ]).map(item => "response_text" in item ? renderCloudTurn(item) : renderRelayTurn(item));
    return [
      <li key={`${interaction.id}-user`} className="jarvis-turn jarvis-turn--user">
        <div className="jarvis-bubble jarvis-bubble--user"><p>{interaction.user_text}</p></div>
      </li>,
      <li key={interaction.id} className="jarvis-turn">
        <div className={`jarvis-bubble jarvis-bubble--jarvis${running ? " is-working" : ""}${notice && !interaction.assistant_text ? " is-failed" : ""}`}>
          <div className="jarvis-bubble__meta"><span className="jarvis-bubble__author">{synthetic ? "Test responder" : "Jarvis"}</span>{running ? <span>working…</span> : null}</div>
          {running
            ? <p className="jarvis-working"><span className="jarvis-working__dots" aria-hidden="true"><i /><i /><i /></span>{interaction.activity ?? "Thinking…"} · {Math.max(0, Math.floor((now - Date.parse(interaction.created_at)) / 1000))}s</p>
            : interaction.assistant_text ? unsafeAssistantText ? <p role="status">Jarvis could not turn this into a valid change, so nothing was changed. Try rephrasing, or use Escalate for a stronger model.</p> : <JarvisMessageText text={interaction.assistant_text} /> : null}
          {interaction.surface_summary ? <p className="jarvis-surface-summary">{interaction.surface_summary}</p> : null}
          <WorkspaceActionCards actions={interaction.actions ?? []} onApply={applyAction} onDismiss={dismissAction} onUndo={undoAction}
            technicalDetails={interaction.technical_details}
            technicalInfo={unsafeAssistantText ? interaction.assistant_text : undefined} />
          {notice ? <p className="jarvis-bubble__notice" role="status">{notice}</p> : null}
          {interaction.assistant_text_truncated ? <p className="jarvis-bubble__notice">This saved answer was shortened for storage.</p> : null}
          {interaction.proposal_count ? <p className="jarvis-bubble__proposals">{interaction.proposal_count}{interaction.proposals_truncated ? "+" : ""} proposal{interaction.proposal_count === 1 ? "" : "s"} recorded for your review — nothing was applied.</p> : null}
        </div>
        {!running ? <div className="jarvis-bubble__actions">
          {interaction.flow_state === "partial_terminal" ? <button type="button" className="jarvis-link-button" disabled={submitting} onClick={() => setPrompt(`Continue the previous answer from where it stopped. Do not repeat the completed part. Original request: ${interaction.user_text.slice(0, 1500)}\n\nPrevious partial answer:\n${(interaction.assistant_text ?? "").slice(-8500)}`)}>Continue answer</button> : null}
          {interaction.flow_state === "failed_terminal" ? <button type="button" className="jarvis-link-button" disabled={submitting} onClick={() => setPrompt(interaction.user_text)}>Try again</button> : null}
          {escalatable ? <EscalateControl disabled={submitting || cloudWorking || relayEscalating} onRelay={() => void prepareRelayEscalation(interaction.id)} onApi={() => startApiEscalation(interaction.id)} /> : null}
          {interaction.assistant_text ? <p className="jarvis-bubble__metadata">{metadata}{interaction.assistant_text && !synthetic ? " · advisory" : ""}</p> : null}
          <details className="jarvis-bubble__details"><summary aria-label="Show answer information"><Info size={13} aria-hidden="true" /> Info</summary><dl><div><dt>Responder</dt><dd>{viaAgent ? "Jarvis agent (Hermes)" : interaction.route_class ?? "Unknown"}</dd></div><div><dt>Model</dt><dd>{interaction.model_id ?? "Unknown"}</dd></div><div><dt>Provider</dt><dd>{interaction.provider_id ?? "Unknown"}</dd></div><div><dt>Run state</dt><dd>{interaction.flow_state}</dd></div><div><dt>Persistence</dt><dd>{interaction.persistence_state}</dd></div><div><dt>Attempts</dt><dd>{interaction.attempt_count}</dd></div><div><dt>Tokens</dt><dd>{interaction.input_tokens ?? "unknown"} in · {interaction.output_tokens ?? "unknown"} out</dd></div><div><dt>Cost</dt><dd>{interaction.cost_estimate_usd == null ? "unknown" : `$${interaction.cost_estimate_usd}`}</dd></div><div><dt>Latency</dt><dd>{interaction.latency_ms == null ? "unknown" : `${interaction.latency_ms} ms`}</dd></div><div><dt>Flow</dt><dd><code>{interaction.flow_id}</code></dd></div></dl>{interaction.persistence_error ? <p>Persistence diagnostic: {interaction.persistence_error}</p> : null}{interaction.proposal_ids.length ? <p>Proposal refs: {interaction.proposal_ids.join(", ")}</p> : null}<p>Answers are advice: nothing in the project changes until you accept a proposal.</p></details>
        </div> : null}
      </li>,
      ...followups,
      relaySource === interaction.id ? renderRelayEscalationCard() : null,
      cloudSource === interaction.id ? renderApiEscalationCard() : null
    ];
  };

  const responderItems: ContextMenuItem[] = usableRoutes.map(route => ({
    id: route.route_class,
    label: `${route.route_class === routeClass ? "✓ " : ""}${responderLabel(route)}`,
    onSelect: () => chooseResponder(route.route_class)
  }));
  const optionItems: ContextMenuItem[] = [
    ...responderItems,
    ...unavailableRoutes.map(route => ({ id: `off-${route.route_class}`, label: `${responderLabel(route)} — unavailable`, hint: route.availability.message, disabled: true, onSelect: () => undefined })),
    { id: "target-jarvis", label: `${composerTarget === "jarvis" ? "✓ " : ""}Send to Jarvis`, onSelect: () => setComposerTarget("jarvis") },
    ...(relayStatus?.enabled ? relayStatus.agents.map(agent => ({ id: `relay-${agent}`, label: `${composerTarget === "relay" && relayAgent === agent ? "✓ " : ""}Send task to Relay · ${modelDisplayName(null, agent)}`, onSelect: () => { setRelayAgent(agent); setComposerTarget("relay"); } })) : [])
  ];
  const activeResponderName = composerTarget === "relay" ? `Relay · ${modelDisplayName(null, relayAgent)}` : activeRoute ? responderLabel(activeRoute) : "No responder";

  return <div className="jarvis-sidecar" data-testid="jarvis-sidecar">
    <header className="jarvis-sidecar__header">
      <div className="jarvis-sidecar__title">
        <h2 id="jarvis-sidecar-title" tabIndex={-1} data-sidecar-focus>Jarvis</h2>
        <span className={`jarvis-status jarvis-status--${state.tone}`} role="status" title={state.detail} aria-label={state.label} aria-description={state.detail}><i aria-hidden="true" /><span>{state.label}</span></span>
      </div>
      <div className="jarvis-sidecar__header-actions">
        <button type="button" className="jarvis-icon-button jarvis-icon-button--new-conversation" onClick={() => selectThread(null)} disabled={!workspaceId || submitting || selectedThreadId === null} title="Start a new conversation" aria-label="New conversation"><ChatCircleDots size={15} aria-hidden="true" /><span>New conversation</span></button>
        <SidecarChromeActions />
      </div>
    </header>

    <ol className="jarvis-sidecar__transcript" aria-label="Jarvis conversation" aria-live="polite" ref={transcriptRef}>
      {!workspaceId ? <li className="jarvis-sidecar__welcome">Select a workspace to talk to Jarvis.</li>
        : !detail && !showOptimistic ? <li className="jarvis-sidecar__welcome">{loadingThreads ? "Loading conversations…" : "Ask about this workspace."}</li> : null}
      {detail?.interactions.flatMap(renderInteraction)}
      {showOptimistic && inFlight ? [
        <li className="jarvis-turn jarvis-turn--user" key="in-flight-user"><div className="jarvis-bubble jarvis-bubble--user"><p>{inFlight.prompt}</p></div></li>,
        <li className="jarvis-turn" key="in-flight">
          <div className="jarvis-bubble jarvis-bubble--jarvis is-working">
            <div className="jarvis-bubble__meta"><span className="jarvis-bubble__author">Jarvis</span><span>working…</span></div>
            <p className="jarvis-working"><span className="jarvis-working__dots" aria-hidden="true"><i /><i /><i /></span>{workingLabel(inFlight.startedAt, inFlight.routeClass)}</p>
            {hermesCold ? <p className="jarvis-bubble__notice">The first message starts the agent; later replies are faster.</p> : null}
          </div>
        </li>
      ] : null}
    </ol>

    {pinnedContent}
    {error && !relaySource && !cloudSource ? <p className="jarvis-sidecar__error" role="alert">{error}</p> : null}
    {error && (relaySource || cloudSource) && (relayDraft || cloudDraft) ? <p className="jarvis-sidecar__error" role="alert">{error}</p> : null}
    {basisDiscussionBlocked && <p className="jarvis-sidecar__error" role="status">Project Basis discussion is unavailable under the current sensitivity controls. Prepare a written proposal above, or clear selected context to ask a general question.</p>}

    <form onSubmit={(event) => void submit(event)} className="jarvis-sidecar__composer">
      <div className="jarvis-surface-chip" aria-live="polite">
        <span>{surfaceBrief?.summary ? productDisplayText(surfaceBrief.summary) : (routeId === "design-process" || routeId === "design-bluecad" || routeId === "design-studio" ? "Reading workspace context…" : "No workspace context on this page")}</span>
        {surfaceBrief?.text ? <details><summary>Workspace context</summary><p>{productDisplayText(surfaceBrief.text)}</p></details> : null}
      </div>
      {composerTarget === "relay" && relayStatus?.enabled ? <div className="jarvis-sidecar__relay-target">
        <p className="jarvis-sidecar__hint">Task for {modelDisplayName(null, relayAgent)} via Relay · {accessModeLabel(relayStatus.access_mode)} access at level {relayStatus.repository_level}. {relayStatus.blocked_reason ?? "Only submit a task that contains no strategic or domain IP."}</p>
        <label className="jarvis-sidecar__toggle"><input type="checkbox" checked={relayAttested} disabled={relayWorking} onChange={event => setRelayAttested(event.target.checked)} />This task contains no strategic/domain IP (cloud-safe)</label>
      </div> : null}
      <div className="jarvis-sidecar__input">
        <textarea id="jarvis-prompt" aria-label={composerTarget === "relay" ? "Task for the Relay agent" : "Message"} value={prompt} onChange={(event) => setPrompt(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} maxLength={12000} rows={Math.min(8, Math.max(2, prompt.split("\n").length))} disabled={!workspaceId} placeholder={composerTarget === "relay" ? "Task for the Relay agent…" : "Ask Jarvis…"} />
        <button type="submit" disabled={!workspaceId || !prompt.trim() || loadingThreads || (composerTarget === "jarvis" ? (!activeRoute || !activeRouteAvailable || submitting || !contextReady) : (!relayAgent || !relayAttested || relayWorking || Boolean(relayStatus?.blocked_reason)))} className="jarvis-send" aria-label={composerTarget === "relay" ? "Send to Relay agent" : composerLabel} title={composerTarget === "relay" ? "Send to Relay agent" : composerLabel}>{submitting || relayWorking ? <span className="jarvis-working__dots" aria-hidden="true"><i /><i /><i /></span> : <ArrowUp size={16} weight="bold" aria-hidden="true" />}</button>
      </div>
      <div className="jarvis-sidecar__composer-bar">
        <ResponderMenu items={optionItems} disabled={submitting || relayWorking}>{activeResponderName} ▾</ResponderMenu>
        <details className="jarvis-sidecar__context" aria-label="Project context controls"><summary className="jarvis-chip">{contextSummary}</summary>
          <div className="jarvis-sidecar__context-body">
            {contextualContent ? <section className={stageContextClassName} aria-label="Current stage context">{contextualContent}</section> : null}
            <label className="jarvis-sidecar__toggle"><input type="checkbox" checked={contextEnabled} disabled={submitting || Boolean(activeKnowledge)} onChange={(event) => { setContextEnabled(event.target.checked); setError(null); }} />Use inspected project context</label>
            {contextEnabled && previewLoading ? <p className="jarvis-sidecar__hint">Building context preview…</p> : null}
            {contextEnabled && preview ? <details><summary>Context pack · {preview.included_count} records · ~{preview.estimated_token_count} tokens</summary><p>Digest <code>{preview.context_digest ?? "empty"}</code></p><p>{preview.char_count} characters · {preview.dropped_count} dropped</p><ul>{preview.context_sources_manifest.map((source) => <li key={`${source.type}:${source.id}:${source.source}`}>{source.type ?? "record"}: {source.id ?? source.source}</li>)}</ul></details> : null}
            {contextEnabled ? <button type="button" className="jarvis-link-button" onClick={() => setPreviewNonce((current) => current + 1)} disabled={!workspaceId || previewLoading || submitting}>Refresh context preview</button> : <p className="jarvis-sidecar__hint">{activeKnowledge ? `${activeKnowledge.included_count} exact selected records will be sent with your message.` : "Project context is off. Only your message is sent."}</p>}
            {pendingRetryReady && contextEnabled ? <p className="jarvis-sidecar__hint">A retry keeps the originally inspected context digest.</p> : null}
          </div>
        </details>
        {sendDisabledReason && !activeRouteAvailable && composerTarget === "jarvis" ? <small className="jarvis-sidecar__hint">{sendDisabledReason}</small> : null}
      </div>
    </form>

    {workspaceId && threads.length ? <details className="jarvis-sidecar__history"><summary>Conversations · {threads.length}</summary>
      <ul>{threads.map((thread) => <li key={thread.id}><button type="button" aria-current={thread.id === selectedThreadId ? "true" : undefined} disabled={submitting} onClick={() => selectThread(thread.id)}><span>{(thread.title || "Untitled conversation").slice(0, 60)}</span><small>{relativeTime(thread.last_activity_at)}</small></button></li>)}</ul>
    </details> : null}
  </div>;
}
