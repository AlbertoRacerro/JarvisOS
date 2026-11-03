import { Fragment, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { SolverSection } from "../components/process/SolverSection";
import { diagnosticRows, methodLabel, stopReasonText } from "../components/process/solverSettings";
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import ProcessProposals from "../components/process/ProcessProposals";
import { publishProcessSurface } from "../app/workspaceActionSurface";
import BiologyModelLibrary from "../components/process/BiologyModelLibrary";
import PbrInspector from "../components/process/PbrInspector";
import StudioUnitInspector, { StudioProvenance } from "../components/process/StudioUnitInspector";
import { addStudioUnitOp, isStudioUnit, paletteSpec, placedSpec, portLabels } from "../components/process/studioUnitLogic";
import { svgDataUri } from "../components/studio/unitLogic";
import AppLink, { type Navigate } from "../app/AppLink";
import { designViewHref, STAGE_NODE } from "../pages/designView";
import ReactorKinetics from "../components/process/ReactorKinetics";
import ReactorResults from "../components/process/ReactorResults";
import QuantityInput from "../components/process/QuantityInput";
import { ownerLabel, ownerShort } from "../components/process/processOwners";
import { failureTouchesUnit } from "../components/process/pbrLogic";
import ResultProperties from "../components/process/ResultProperties";
import { RunComparisonPanel, SettingsDiff, SolverConsole } from "../components/process/SolverConsole";
import { ObjectResults, OutcomeDetails, ResultsWorkspace, RunStateIndicator, runState } from "../components/process/RunResults";
import { resultsLayout, streamLabel } from "../components/process/resultsLayout";
import { useFlowsheetViewport, ZOOM_MAX, ZOOM_MIN } from "../components/process/useFlowsheetViewport";
import { ContextMenu, MenuButton, useContextMenu } from "../components/ui/ContextMenu";
import type { ContextMenuItem } from "../components/ui/ContextMenu";
import {
  createDraft,
  DRAFT_CHANGED_EVENT,
  DraftApiError,
  executeDraft,
  formatQuantity,
  getDraft,
  getDraftRegistry,
  getDraftRun,
  getRunResults,
  getSolve,
  listDraftRevisions,
  listSolves,
  listDrafts,
  modeKey,
  modeLabel,
  optionLabel,
  optionValue,
  patchDraft,
  startSolve,
  stopSolve,
  restoreDraftRevision,
  unitLabel,
  type DraftReaction,
  type DraftObject,
  type DraftOp,
  type DraftProjection,
  type DraftQuantity,
  type DraftRegistry,
  type DraftRun,
  type SolveEvent,
  type SolveStatus,
  type DraftSummary,
  type Finding,
  type OptionValue,
  type Proposal,
  type RegistryOption,
  type RegistryParam,
  type RegistryUnit,
  type StudioPaletteUnit,
  type RevisionSummary,
  type ResultProperty,
  type ResultsView,
  type StoredQuantity,
} from "../api/processDraft";
import {
  addBend,
  editableSegment,
  midpoint,
  offsetSegment,
  pathData,
  removeVertex,
  routeEditOps,
  routeStream,
  type Anchor,
  type Point,
} from "./processRouting";
import "./ProcessDraftEditor.css";

type Drag = { id: string; pointer: Point; origin: Point; moved: boolean };
// Segment drag on an orthogonal route: moves perpendicular to the segment, layout only.
type RouteDrag = { stream: string; index: number; points: Point[]; pointer: Point; moved: boolean };
type Notice = { tone: "info" | "danger" | "success"; text: string } | null;

const UNIT_W = 76;
const UNIT_H = 44;
const STREAM_R = 7;

const formatCultureValue = (value: { display?: DraftQuantity | null; reason?: string }) =>
  value.display
    ? `${new Intl.NumberFormat("en-US", { maximumSignificantDigits: 5 }).format(value.display.value)} ${unitLabel(value.display.unit)}`
    : value.reason ?? "unknown";

function distinctFindings(findings: Finding[]) {
  const seen = new Set<string>();
  return findings.filter((finding) => {
    const key = JSON.stringify([finding.severity, finding.code, finding.object, finding.message, finding.fix]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function visibleFindings(findings: Finding[]) {
  const unique = distinctFindings(findings);
  const noTextFailures = unique.filter((finding) => finding.severity === "warning"
    && finding.code === "DWSIM_OBJECT_FAILED"
    && finding.message === "DWSIM did not calculate it: no error text");
  if (noTextFailures.length < 2) return unique;
  const failedIds = new Set(noTextFailures);
  const names = [...new Set(noTextFailures.map((finding) => finding.object).filter(Boolean))];
  return [...unique.filter((finding) => !failedIds.has(finding)), {
    severity: "warning",
    code: "DWSIM_OBJECT_FAILED",
    object: "",
    field: "",
    message: `DWSIM did not calculate: ${names.join(", ")}`,
    source: "dwsim",
  } satisfies Finding];
}

function solveFailureMessages(run: DraftRun) {
  const solve = run.solve;
  if (!solve) return [];
  const messages = [...new Set(solve.errors.map((item) => String(item).trim()).filter(Boolean))];
  const objectsWithErrors = solve.failed_objects.filter((item) => String(item.error ?? "").trim());
  for (const item of objectsWithErrors) {
    const error = String(item.error).trim();
    const formatted = `${item.tag}: ${error}`;
    if (!messages.includes(formatted) && !messages.some((message) => message === error)) messages.push(formatted);
  }
  const uncalculated = [...new Set(solve.failed_objects
    .filter((item) => !String(item.error ?? "").trim())
    .map((item) => item.tag)
    .filter(Boolean))];
  if (uncalculated.length) {
    const failedUnits = [...new Set(objectsWithErrors.map((item) => item.tag).filter(Boolean))];
    const cause = failedUnits.length ? `${failedUnits.join(", ")} failed: ` : "DWSIM did not calculate: ";
    messages.push(`Not calculated because ${cause}${uncalculated.join(", ")}`);
  }
  return messages;
}

function mixedFailureMessage(errors: unknown): string {
  if (typeof errors === "string") return errors;
  if (!errors || typeof errors !== "object") return "See the failed segment details.";
  const record = errors as { message?: unknown; errors?: unknown; dwsim_message?: unknown; detail?: unknown;
    findings?: unknown; diffs?: unknown; code?: unknown };
  if (typeof record.message === "string" && record.message) return record.message;
  if (typeof record.dwsim_message === "string") return record.dwsim_message;
  // Failed DWSIM check: the findings name the unit and the problem.
  if (Array.isArray(record.findings)) {
    const text = record.findings.slice(0, 3).map((item) => {
      const row = item as { object?: string; message?: string; code?: string };
      return row.message ? (row.object ? `${row.object}: ${row.message}` : row.message) : row.code ?? "";
    }).filter(Boolean);
    if (text.length) return text.join("; ");
  }
  // Materialization mismatch: what the draft expects against what DWSIM holds.
  if (Array.isArray(record.diffs)) {
    const text = record.diffs.slice(0, 3).map((item) => {
      const row = item as { path?: string; expected?: unknown; actual?: unknown };
      return `${row.path ?? "value"}: draft expects ${String(row.expected)}, DWSIM holds ${String(row.actual)}`;
    });
    if (text.length) return text.join("; ");
  }
  if (Array.isArray(record.errors)) {
    const message = record.errors.find((item) => typeof item === "string");
    if (typeof message === "string") return message;
    const first = record.errors.find((item) => item && typeof item === "object" && "message" in item) as { message?: unknown } | undefined;
    if (typeof first?.message === "string") return first.message;
  }
  if (record.detail && typeof record.detail === "object") return mixedFailureMessage(record.detail);
  if (typeof record.code === "string") return record.code;
  return "See the failed segment details.";
}

function mixedFailureCode(errors: unknown): string | null {
  if (!errors || typeof errors !== "object") return null;
  const record = errors as { code?: unknown; detail?: unknown };
  return typeof record.code === "string" ? record.code : mixedFailureCode(record.detail);
}

const errorText = (cause: unknown) =>
  cause instanceof DraftApiError
    ? `${cause.message}${cause.detail.field ? ` (${String(cause.detail.field)})` : ""} · ${cause.code}`
    : "The request failed.";

const portY = (count: number, port: number) => (port - (count - 1) / 2) * 14;
const energyPortX = (count: number, index: number) => (index - (count - 1) / 2) * 16;
const isEnergy = (item: DraftObject) => item.type === "EnergyStream";
const isOptionParam = (param: RegistryParam) => Boolean(param.options?.length) || param.kind === "bool" || param.kind === "boolean";
const boolOptions: RegistryOption[] = [{ value: true, label: "Yes" }, { value: false, label: "No" }];
const REACTOR_TABS = ["Setup", "Kinetics", "Results"] as const;
const TAG_PREFIX: Record<string, string> = {
  Heater: "H", Cooler: "C", Pump: "P", Valve: "V", Mixer: "M", Flash: "F", Splitter: "SP",
  HeatExchanger: "HX", Recycle: "R", PFR: "PFR", CSTR: "CSTR", DistillationColumn: "T", PhotobioreactorT1: "PBR",
};

function nextTag(objects: DraftObject[], prefix: string) {
  const used = new Set(objects.map((item) => item.tag));
  let index = 1;
  while (used.has(`${prefix}${index}`)) index += 1;
  return `${prefix}${index}`;
}

function nextId(objects: DraftObject[], prefix: string) {
  const used = new Set(objects.map((item) => item.id));
  let index = 1;
  while (used.has(`${prefix}${index}`)) index += 1;
  return `${prefix}${index}`;
}

type FormState = Record<string, { text: string; unit: string }>;

function quantitiesFrom(form: FormState): Record<string, DraftQuantity> | null {
  const result: Record<string, DraftQuantity> = {};
  for (const [key, entry] of Object.entries(form)) {
    if (!entry.text.trim()) continue;
    const value = Number(entry.text);
    if (!Number.isFinite(value)) return null;
    result[key] = { value, unit: entry.unit };
  }
  return result;
}

export default function ProcessDraftEditor({ workspaceId, initialDraftId, navigate, dynamicLink = true, onOpenUnit, onSelectUnit }: Readonly<{
  workspaceId: string; initialDraftId?: string; navigate?: Navigate;
  /** The line pointing to Design › Dynamic; Design's own Plant stage has the stage button instead. */
  dynamicLink?: boolean;
  onOpenUnit?: (draftId: string, revision: string, unit: DraftObject) => void;
  /** Reports the unit selected on the flowsheet (null when nothing, or a stream, is selected). */
  onSelectUnit?: (selection: { draftId: string; revision: string; unit: DraftObject } | null) => void;
}>) {
  const [biologyOpen, setBiologyOpen] = useState(false);
  const [registry, setRegistry] = useState<DraftRegistry | null>(null);
  const [drafts, setDrafts] = useState<DraftSummary[]>([]);
  const [draft, setDraft] = useState<DraftProjection | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState<"validate" | "run" | null>(null);
  const [lastRun, setLastRun] = useState<DraftRun | null>(null);
  const [solve, setSolve] = useState<SolveStatus | null>(null);
  const [solveEvents, setSolveEvents] = useState<SolveEvent[]>([]);
  // Spec 213: a Run reveals the live solver console; the toolbar keeps a shortcut to it while a solve exists.
  const solverConsoleRef = useRef<HTMLDivElement>(null);
  const [revealRunId, setRevealRunId] = useState<string | null>(null);
  // Run opens the Solver panel floating over the page; the operator docks it back under the flowsheet.
  const [consoleFloating, setConsoleFloating] = useState(false);
  const showSolverConsole = useCallback((moveFocus: boolean) => {
    const target = solverConsoleRef.current;
    if (!target) return;
    const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // A floating panel is already in view; only the docked one needs the page to scroll to it.
    if (!target.classList.contains("is-floating")) target.scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
    // Revealing on Run leaves focus on the toolbar; the explicit shortcut moves focus to the console.
    if (moveFocus) target.focus({ preventScroll: true });
  }, []);
  useEffect(() => {
    if (!revealRunId || solve?.run_id !== revealRunId) return;
    setRevealRunId(null);
    const frame = window.requestAnimationFrame(() => showSolverConsole(false));
    return () => window.cancelAnimationFrame(frame);
  }, [revealRunId, solve?.run_id, showSolverConsole]);
  const [solvedRun, setSolvedRun] = useState<DraftRun | null>(null);
  const [resultsView, setResultsView] = useState<ResultsView | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const [revisions, setRevisions] = useState<RevisionSummary[] | null>(null);
  const [drag, setDrag] = useState<Drag | null>(null);
  const [overrides, setOverrides] = useState<Record<string, Point>>({});
  const [routeDrag, setRouteDrag] = useState<RouteDrag | null>(null);
  const [routeOverrides, setRouteOverrides] = useState<Record<string, Point[]>>({});
  const [form, setForm] = useState<FormState>({});
  const [cultureForm, setCultureForm] = useState<FormState>({});
  const [cultureEnabled, setCultureEnabled] = useState(false);
  const [optionForm, setOptionForm] = useState<Record<string, OptionValue>>({});
  const [reactorTab, setReactorTab] = useState<(typeof REACTOR_TABS)[number]>("Setup");
  const reactorTabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const reactorTabBase = useId();
  const [composition, setComposition] = useState<Record<string, string>>({});
  const [mode, setMode] = useState<string>("");
  const [flowBasis, setFlowBasis] = useState<"mass_flow" | "molar_flow">("mass_flow");
  const [stateBasis, setStateBasis] = useState<"temperature" | "vapor_fraction">("temperature");
  const [compositionBasis, setCompositionBasis] = useState<"mass" | "mole">("mass");
  const [contextUnitId, setContextUnitId] = useState<string | null>(null);
  const unitMenu = useContextMenu();
  const [rename, setRename] = useState("");
  const revisionRef = useRef<string>("");
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const svgRef = useRef<SVGSVGElement | null>(null);

  const accept = useCallback((next: DraftProjection) => {
    revisionRef.current = next.revision;
    setDraft(next);
  }, []);

  const loadDraft = useCallback(
    async (draftId: string) => {
      const next = await getDraft(workspaceId, draftId);
      accept(next);
      return next;
    },
    [accept, workspaceId],
  );

  useEffect(() => {
    let live = true;
    setDraft(null);
    setLastRun(null);
    setSolvedRun(null);
    setSolve(null);
    setSolveEvents([]);
    void (async () => {
      try {
        const [reg, rows] = await Promise.all([getDraftRegistry(workspaceId), listDrafts(workspaceId)]);
        if (!live) return;
        setRegistry(reg);
        setDrafts(rows);
        const initial = initialDraftId ? rows.find((row) => row.draft_id === initialDraftId) : rows[0];
        if (initialDraftId && !initial) {
          setNotice({ tone: "danger", text: "The Process draft referenced by this Design version is unavailable." });
          return;
        }
        if (initial) {
          const next = await getDraft(workspaceId, initial.draft_id);
          if (live) accept(next);
        }
      } catch (cause) {
        if (live) setNotice({ tone: "danger", text: errorText(cause) });
      }
    })();
    return () => {
      live = false;
    };
  }, [accept, workspaceId, initialDraftId]);

  // Hermes proposals and Sidecar approvals arrive out of band; keep the canvas current.
  useEffect(() => {
    if (!draft) return;
    const draftId = draft.draft_id;
    const refresh = () => {
      if (drag || routeDrag || busy) return;
      void getDraft(workspaceId, draftId)
        .then((next) => {
          setDraft((current) => {
            if (
              current &&
              current.revision === next.revision &&
              JSON.stringify(current.proposals.map((p) => [p.proposal_id, p.state])) ===
                JSON.stringify(next.proposals.map((p) => [p.proposal_id, p.state])) &&
              current.results.last_attempt?.run_id === next.results.last_attempt?.run_id
            )
              return current;
            revisionRef.current = next.revision;
            return next;
          });
        })
        .catch(() => undefined);
    };
    const timer = window.setInterval(refresh, 3000);
    window.addEventListener(DRAFT_CHANGED_EVENT, refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener(DRAFT_CHANGED_EVENT, refresh);
    };
  }, [busy, drag, draft, routeDrag, workspaceId]);

  // Results shown on the canvas always come from the recorded run bound to a revision.
  const solvedRunId = draft?.results.run_id;
  useEffect(() => {
    if (!draft || !solvedRunId) return setSolvedRun(null);
    if (solvedRun?.run_id === solvedRunId) return;
    void getDraftRun(workspaceId, draft.draft_id, solvedRunId).then(setSolvedRun).catch(() => undefined);
  }, [draft, solvedRun?.run_id, solvedRunId, workspaceId]);

  // The last DWSIM attempt (including refused materializations) is shown after reloads too.
  const lastAttemptId = draft?.results.last_attempt?.run_id;
  useEffect(() => {
    if (!draft || !lastAttemptId || lastRun?.run_id === lastAttemptId) return;
    void getDraftRun(workspaceId, draft.draft_id, lastAttemptId).then(setLastRun).catch(() => undefined);
  }, [draft, lastAttemptId, lastRun?.run_id, workspaceId]);

  // Spec 181: the results view shown is the latest Run attempt's when it holds a non-converged last iterate or
  // was not solved after the converged one; otherwise the converged run (current or stale).
  const lastAttempt = draft?.results.last_attempt;
  const viewRunId = lastAttempt?.action === "run" && lastAttempt.outcome && lastAttempt.outcome.state !== "converged"
    && (lastAttempt.outcome.results_available === "last_iterate" || !solvedRunId) ? lastAttempt.run_id : solvedRunId ?? null;
  useEffect(() => {
    if (!draft || !viewRunId) return setResultsView(null);
    if (resultsView?.run_id === viewRunId) return;
    let live = true;
    void getRunResults(workspaceId, draft.draft_id, viewRunId).then((next) => { if (live) setResultsView(next); }).catch(() => undefined);
    return () => { live = false; };
  }, [draft, resultsView?.run_id, viewRunId, workspaceId]);
  // Setup (species and thermodynamics) opens by default only while the draft declares no compounds.
  const draftKey = draft?.draft_id;
  const hasCompounds = Boolean(draft?.compounds.length);
  useEffect(() => { if (draftKey) setSetupOpen(!hasCompounds); }, [draftKey]); // eslint-disable-line react-hooks/exhaustive-deps
  // Spec 182: Setup is an overlay drawer opened from the toolbar; opening moves focus into it, closing returns it.
  const setupRef = useRef<HTMLElement | null>(null);
  const setupToggleRef = useRef<HTMLButtonElement | null>(null);
  const setupFocus = useRef(false);
  const openSetup = () => { setupFocus.current = true; setSetupOpen(true); };
  const closeSetup = () => { setSetupOpen(false); setupToggleRef.current?.focus({ preventScroll: true }); };
  useEffect(() => {
    if (!setupOpen || !setupFocus.current) return;
    setupFocus.current = false;
    setupRef.current?.focus({ preventScroll: true });
  }, [setupOpen]);

  const apply = useCallback(
    (ops: DraftOp[]) => {
      if (!draft) return Promise.resolve();
      const draftId = draft.draft_id;
      queue.current = queue.current.then(async () => {
        try {
          accept(await patchDraft(workspaceId, draftId, revisionRef.current, ops));
          setNotice(null);
          return true;
        } catch (cause) {
          if (cause instanceof DraftApiError && cause.status === 409) {
            await loadDraft(draftId);
            setNotice({ tone: "danger", text: "The draft changed elsewhere. It was reloaded; re-apply your edit." });
          } else {
            setNotice({ tone: "danger", text: errorText(cause) });
          }
          return false;
        } finally {
          setOverrides({});
          setRouteOverrides({});
        }
      });
      return queue.current;
    },
    [accept, draft, loadDraft, workspaceId],
  );

  const objects = useMemo(() => draft?.objects ?? [], [draft]);
  const byId = useMemo(() => new Map(objects.map((item) => [item.id, item])), [objects]);
  // The draft's streams, units and KPI rows: results tables keep their rows with `empty` cells without a solved run.
  const resultsFrame = useMemo(() => resultsLayout(objects), [objects]);
  const streamName = useCallback((tag: string) => streamLabel(resultsFrame, tag), [resultsFrame]);
  const units = objects.filter((item) => item.kind === "unit");
  const selected = selectedId ? byId.get(selectedId) ?? null : null;
  const selectedUnit = selected?.kind === "unit" ? selected : null;
  useEffect(() => {
    onSelectUnit?.(selectedUnit && draft ? { draftId: draft.draft_id, revision: draft.revision, unit: selectedUnit } : null);
    return () => onSelectUnit?.(null);
  }, [onSelectUnit, selectedUnit, draft]);
  // Spec 182: the inspector exists only while something is selected. When it closes with focus inside it,
  // focus returns to the flowsheet canvas instead of being lost to the document body.
  const inspectorOpen = Boolean(selected);
  const wasInspectorOpen = useRef(false);
  useEffect(() => {
    const closed = wasInspectorOpen.current && !inspectorOpen;
    wasInspectorOpen.current = inspectorOpen;
    if (!closed) return;
    const active = document.activeElement;
    if (!active || active === document.body) svgRef.current?.focus({ preventScroll: true });
  }, [inspectorOpen]);
  // Escape closes the Setup drawer when focus is in it (or nothing is selected), otherwise clears the selection.
  // Form fields keep Escape for themselves; menus stop it before it reaches here.
  const onEditorKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape" || event.defaultPrevented || biologyOpen) return;
    const target = event.target as HTMLElement;
    if (target.closest("input:not([type=checkbox]):not([type=radio]), select, textarea")) return;
    if (setupOpen && (setupRef.current?.contains(target) || !selected)) {
      event.preventDefault();
      closeSetup();
    } else if (selected) {
      event.preventDefault();
      setSelectedId(null);
    }
  };
  useEffect(() => {
    publishProcessSurface({ draft_id: draft?.draft_id ?? null, revision: draft?.revision ?? null,
      process_selection: selected ? [{ kind: selected.kind, id: selected.id, tag: selected.tag }] : [] });
  }, [draft?.draft_id, draft?.revision, selected?.id, selected?.kind, selected?.tag]);
  useEffect(() => () => publishProcessSurface({ draft_id: null, revision: null, process_selection: [] }), []);

  useEffect(() => {
    const refresh = (event: Event) => {
      const detail = (event as CustomEvent<{ workspaceId?: string; surface?: string; draftId?: string | null }>).detail;
      if (detail?.workspaceId !== workspaceId || detail.surface !== "process" || !draft || (detail.draftId && detail.draftId !== draft.draft_id)) return;
      void loadDraft(draft.draft_id).then((next) => setNotice({ tone: "success", text: `Workspace action refreshed draft revision ${next.revision.split(":", 1)[0]}. Results are stale until rerun.` }))
        .catch(() => setNotice({ tone: "danger", text: "The workspace action completed, but the Process draft could not be refreshed." }));
    };
    window.addEventListener("jarvis:workspace-action", refresh);
    return () => window.removeEventListener("jarvis:workspace-action", refresh);
  }, [draft, loadDraft, workspaceId]);
  const contextUnit = contextUnitId ? byId.get(contextUnitId) : undefined;
  const unitSpec = (type: string): RegistryUnit | undefined => registry?.units.find((item) => item.type === type);
  // Spec 190: a placed StudioUnit's ports and parameters come from its own pinned contract.
  const specOf = (unit: DraftObject): RegistryUnit | undefined => placedSpec(unit) ?? unitSpec(unit.type);
  const blockers = (draft?.findings ?? []).filter((item) => item.severity === "blocker");
  // Shown counts equal the visible guidance rows (deduplicated, collapsed groups count once).
  const shownFindings = useMemo(() => visibleFindings(draft?.findings ?? []), [draft]);
  const shownBlockerCount = shownFindings.filter((item) => item.severity === "blocker").length;
  const shownWarningCount = shownFindings.length - shownBlockerCount;
  const countLabel = `${shownBlockerCount ? `${shownBlockerCount} blocker${shownBlockerCount === 1 ? "" : "s"}` : "no blockers"}, ${shownWarningCount} warning${shownWarningCount === 1 ? "" : "s"}`;
  const pending = (draft?.proposals ?? []).filter((item) => item.state === "pending");
  const proposalTargets = useMemo(() => {
    const map = new Map<string, Proposal["changes"]>();
    for (const proposal of pending) for (const change of proposal.changes) map.set(change.target_id, [...(map.get(change.target_id) ?? []), change]);
    return map;
  }, [pending]);

  // Inspector drafts reset whenever the selection or its revision changes. Layout effect: the reset lands
  // before the new revision is painted, so input typed after it appears is never wiped.
  useLayoutEffect(() => {
    setForm({});
    setCultureForm({});
    setOptionForm({});
    setRename(selected?.tag ?? "");
    setMode(selected?.mode ?? "");
    if (selected?.kind === "stream") {
      setFlowBasis(selected.spec?.molar_flow ? "molar_flow" : "mass_flow");
      setStateBasis(selected.spec?.vapor_fraction ? "vapor_fraction" : "temperature");
      setCompositionBasis(selected.spec?.composition_basis ?? "mass");
      setCultureEnabled(Boolean(selected.spec?.culture));
      setCultureForm(Object.fromEntries(Object.entries(selected.spec?.culture ?? {}).map(([key, value]) =>
        [key, { text: String(value.value), unit: value.unit }])));
    }
    setComposition(
      Object.fromEntries((draft?.compounds ?? []).map((name) => [name, String(selected?.spec?.composition?.[name] ?? "")])),
    );
  }, [selected?.id, draft?.revision]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setReactorTab("Setup"); }, [selected?.id]);

  const orientationItems: ContextMenuItem[] = contextUnit ? [
    { id: "mirror-lr", label: contextUnit.flip_x ? "Unmirror left ↔ right" : "Mirror left ↔ right", onSelect: () => void apply([{ op: "set_orientation", id: contextUnit.id, flip_x: !contextUnit.flip_x }]) },
    { id: "mirror-tb", label: contextUnit.flip_y ? "Unmirror top ↕ bottom" : "Mirror top ↕ bottom", onSelect: () => void apply([{ op: "set_orientation", id: contextUnit.id, flip_y: !contextUnit.flip_y }]) },
  ] : [];

  const position = (item: DraftObject): Point => overrides[item.id] ?? { x: item.x, y: item.y };
  const toSvg = (event: { clientX: number; clientY: number }): Point => {
    const svg = svgRef.current;
    const matrix = svg?.getScreenCTM();
    if (!svg || !matrix) return { x: event.clientX, y: event.clientY };
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    return { x: point.x, y: point.y };
  };

  const unitWidth = (unit: DraftObject) => Math.max(UNIT_W, (specOf(unit)?.label.length ?? 8) * 6.2 + 20);
  // Port geometry follows the saved layout orientation; endpoint identity and port order stay authoritative.
  const portAnchor = (unit: DraftObject, end: "source" | "target", port: number, energy: boolean): Anchor => {
    const spec = specOf(unit);
    const u = position(unit);
    if (energy) {
      const inlets = spec?.energy_inlets ?? [];
      const all = inlets.length + (spec?.energy_outlets ?? []).length || 1;
      const index = end === "target" ? port : inlets.length + port;
      const x = u.x + energyPortX(all, index) * (unit.flip_x ? -1 : 1);
      return { at: { x, y: u.y + UNIT_H / 2 * (unit.flip_y ? -1 : 1) }, side: unit.flip_y ? "top" : "bottom" };
    }
    const count = end === "source" ? spec?.outlets.length ?? 1 : spec?.inlets.length ?? 1;
    const source = end === "source";
    const side = source !== Boolean(unit.flip_x) ? "right" : "left";
    const y = u.y + portY(count, port) * (unit.flip_y ? -1 : 1);
    return { at: { x: u.x + (side === "right" ? 1 : -1) * unitWidth(unit) / 2, y }, side };
  };
  // Orthogonal polyline from the source port (or feed marker) to the target port (or product marker).
  const streamRoute = (stream: DraftObject): Point[] | null => {
    if (stream.kind !== "stream" || (!stream.source && !stream.target)) return null;
    const at = position(stream);
    const endAnchor = (end: "source" | "target"): Anchor => {
      const endpoint = stream[end];
      const unit = endpoint ? byId.get(endpoint.unit) : undefined;
      if (endpoint && unit) return portAnchor(unit, end, endpoint.port, isEnergy(stream));
      return end === "source" ? { at: { x: at.x + STREAM_R, y: at.y }, side: "right" } : { at: { x: at.x - STREAM_R, y: at.y }, side: "left" };
    };
    return routeStream(endAnchor("source"), endAnchor("target"), routeOverrides[stream.id] ?? stream.route ?? [], UNIT_H / 2 + 22);
  };
  // A stream connected at both ends sits on its route; a feed or product sits at its own position.
  const markerAt = (stream: DraftObject, route: Point[] | null): Point =>
    route && stream.source && stream.target ? midpoint(route) : position(stream);

  const onPointerDown = (event: ReactPointerEvent, item: DraftObject) => {
    event.stopPropagation();
    setSelectedId(item.id);
    if (item.kind === "stream" && item.source && item.target) return;
    (event.target as Element).setPointerCapture?.(event.pointerId);
    setDrag({ id: item.id, pointer: toSvg(event), origin: position(item), moved: false });
  };
  const onSegmentDown = (event: ReactPointerEvent, stream: DraftObject, points: Point[], index: number) => {
    event.stopPropagation();
    setSelectedId(stream.id);
    if (!editableSegment(points, index)) return;
    (event.target as Element).setPointerCapture?.(event.pointerId);
    setRouteDrag({ stream: stream.id, index, points, pointer: toSvg(event), moved: false });
  };
  // Route edits are layout-only: one set_route op, optimistic, never Validate/Run.
  const saveRoute = (streamId: string, points: Point[] | null) => {
    if (!points) return setNotice({ tone: "danger", text: "A route holds at most 12 bends." });
    setRouteOverrides((current) => ({ ...current, [streamId]: points }));
    void apply(routeEditOps(streamId, points));
  };
  const onPointerMove = (event: ReactPointerEvent) => {
    if (viewport.onGestureMove(event)) return;
    if (routeDrag) {
      const point = toSvg(event);
      const a = routeDrag.points[routeDrag.index];
      const b = routeDrag.points[routeDrag.index + 1];
      const offset = Math.round(a.y === b.y ? point.y - routeDrag.pointer.y : point.x - routeDrag.pointer.x);
      if (!routeDrag.moved && Math.abs(offset) < 3) return;
      if (!routeDrag.moved) setRouteDrag({ ...routeDrag, moved: true });
      const next = offsetSegment(routeDrag.points, routeDrag.index, offset);
      if (next) setRouteOverrides((current) => ({ ...current, [routeDrag.stream]: next }));
      return;
    }
    if (!drag) return;
    const point = toSvg(event);
    const dx = point.x - drag.pointer.x;
    const dy = point.y - drag.pointer.y;
    if (!drag.moved && Math.hypot(dx, dy) < 3) return;
    setDrag({ ...drag, moved: true });
    setOverrides({ [drag.id]: { x: Math.round(drag.origin.x + dx), y: Math.round(drag.origin.y + dy) } });
  };
  const onPointerUp = (event: ReactPointerEvent) => {
    // A background click without pan or pinch clears the selection.
    const click = viewport.onGestureUp(event);
    if (click !== null) {
      if (click) setSelectedId(null);
      return;
    }
    if (routeDrag) {
      const next = routeOverrides[routeDrag.stream];
      setRouteDrag(null);
      if (routeDrag.moved && next) saveRoute(routeDrag.stream, next);
      return;
    }
    if (!drag) return;
    const moved = overrides[drag.id];
    setDrag(null);
    if (drag.moved && moved) void apply([{ op: "move", id: drag.id, x: moved.x, y: moved.y }]);
  };

  const addStudioUnit = (entry: StudioPaletteUnit) => {
    const spot = { x: 80 + (objects.length % 6) * 130, y: 90 + Math.floor(objects.length / 6) * 130 };
    const id = nextId(objects, "u");
    void apply([addStudioUnitOp(entry, id, nextTag(objects, "SU-"), spot)]).then(() => setSelectedId(id));
  };
  const addUnit = (type: string) => {
    const spot = { x: 80 + (objects.length % 6) * 130, y: 90 + Math.floor(objects.length / 6) * 130 };
    const prefix = TAG_PREFIX[type] ?? (type.replace(/[^A-Z]/g, "") || "U");
    const id = nextId(objects, "u");
    void apply([{ op: "add_unit", id, type, tag: nextTag(objects, `${prefix}-`), ...spot }]).then(() => setSelectedId(id));
  };
  const addStream = (streamType: "material" | "energy") => {
    const spot = { x: 80 + (objects.length % 6) * 130, y: (streamType === "energy" ? 120 : 70) + Math.floor(objects.length / 6) * 130 };
    const id = nextId(objects, streamType === "energy" ? "e" : "s");
    const tag = nextTag(objects, streamType === "energy" ? "E" : "S");
    void apply([{ op: "add_stream", id, tag, stream_type: streamType, ...spot }]).then(() => setSelectedId(id));
  };

  // Reattach to the latest solve of this draft after a page reload, including recorded histories.
  const draftIdForSolve = draft?.draft_id;
  useEffect(() => {
    if (!draftIdForSolve) return;
    let live = true;
    void listSolves(workspaceId, draftIdForSolve).then(async (rows) => {
      const latest = rows[0];
      if (!live || !latest) return;
      let cursor = 0;
      const history: SolveEvent[] = [];
      let snapshot: SolveStatus;
      do {
        snapshot = await getSolve(workspaceId, draftIdForSolve, latest.run_id, cursor);
        history.push(...snapshot.events);
        cursor = snapshot.cursor;
      } while (live && !snapshot.live && snapshot.events.length === 500);
      if (!live) return;
      setSolveEvents(history);
      setSolve({ ...snapshot, events: [], cursor });
      if (snapshot.live) setBusy("run");
      else if (snapshot.state === "finished" || snapshot.state === "failed") {
        const run = await getDraftRun(workspaceId, draftIdForSolve, latest.run_id).catch(() => null);
        if (!live || !run) return;
        setLastRun(run);
        if (run.outcome?.state === "converged") setSolvedRun(run);
      }
    }).catch(() => undefined);
    return () => {
      live = false;
    };
  }, [draftIdForSolve, workspaceId]);

  const solveId = solve?.run_id;
  const solveLive = solve?.live ?? false;
  useEffect(() => {
    if (!draftIdForSolve || !solveId || !solveLive) return;
    let cancelled = false;
    let cursor = solve?.cursor ?? 0;
    let timer = 0;
    const poll = async () => {
      try {
        const next = await getSolve(workspaceId, draftIdForSolve, solveId, cursor);
        if (cancelled) return;
        cursor = next.cursor;
        if (next.events.length) setSolveEvents((current) => [...current, ...next.events]);
        setSolve((current) => (current && current.run_id === next.run_id ? { ...next, events: [] } : current));
        if (next.live) {
          timer = window.setTimeout(() => void poll(), 400);
          return;
        }
        setBusy(null);
        if (next.state === "finished" || next.state === "failed") {
          const run = await getDraftRun(workspaceId, draftIdForSolve, solveId).catch(() => null);
          if (cancelled) return;
          if (run) {
            setLastRun(run);
            if (run.outcome?.state === "converged") setSolvedRun(run);
          }
          await loadDraft(draftIdForSolve).catch(() => undefined);
        }
      } catch (cause) {
        if (!cancelled) timer = window.setTimeout(() => void poll(), 1500);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [draftIdForSolve, solveId, solveLive, workspaceId, loadDraft]); // eslint-disable-line react-hooks/exhaustive-deps

  const requestStop = () => {
    if (!draft || !solve) return;
    void stopSolve(workspaceId, draft.draft_id, solve.run_id)
      .then((next) => setSolve((current) => (current && current.run_id === next.run_id ? { ...current, state: next.state } : current)))
      .catch((cause) => setNotice({ tone: "danger", text: errorText(cause) }));
  };

  const act = async (action: "validate" | "run", revision?: string, retryOf?: string) => {
    if (!draft) return;
    setBusy(action);
    setNotice(null);
    if (action === "run") {
      // Spec 210: a Run is a background solve whose iterations are followed live in the solver console.
      try {
        const started = await startSolve(workspaceId, draft.draft_id, revision ?? draft.revision, retryOf);
        setSolveEvents(started.events);
        setSolve(started);
        setRevealRunId(started.run_id);
        setConsoleFloating(true);
        if (!started.live) {
          setBusy(null);
          const run = await getDraftRun(workspaceId, draft.draft_id, started.run_id).catch(() => null);
          if (run) {
            setLastRun(run);
            if (run.outcome?.state === "converged") setSolvedRun(run);
          }
          await loadDraft(draft.draft_id).catch(() => undefined);
        }
      } catch (cause) {
        setNotice({ tone: "danger", text: errorText(cause) });
        setBusy(null);
      }
      return;
    }
    try {
      const result = await executeDraft(workspaceId, draft.draft_id, revision ?? draft.revision, action);
      setLastRun(result.run);
      if (result.run.outcome?.state === "converged") setSolvedRun(result.run);
      accept(result.draft);
    } catch (cause) {
      setNotice({ tone: "danger", text: errorText(cause) });
    } finally {
      setBusy(null);
    }
  };

  // Content bounds for "Fit all"; computed before any early return because the viewport is a hook.
  const bounds = objects.reduce(
    (acc, item) => {
      const point = position(item);
      return { minX: Math.min(acc.minX, point.x - 90), minY: Math.min(acc.minY, point.y - 90), maxX: Math.max(acc.maxX, point.x + 120), maxY: Math.max(acc.maxY, point.y + 90) };
    },
    { minX: 0, minY: 0, maxX: 760, maxY: 360 },
  );
  for (const item of objects)
    for (const point of item.route ?? []) {
      bounds.minX = Math.min(bounds.minX, point.x - 40);
      bounds.minY = Math.min(bounds.minY, point.y - 40);
      bounds.maxX = Math.max(bounds.maxX, point.x + 40);
      bounds.maxY = Math.max(bounds.maxY, point.y + 40);
    }
  const viewport = useFlowsheetViewport(svgRef, { x: bounds.minX, y: bounds.minY, w: bounds.maxX - bounds.minX, h: bounds.maxY - bounds.minY });

  if (!registry) return <div className="draft-editor draft-editor--loading">{notice?.text ?? "Loading process draft…"}</div>;
  // While an existing draft is still being read, the empty state (and its Create button) must not show:
  // it would invite a duplicate draft.
  if (!draft && (initialDraftId || drafts.length)) return <div className="draft-editor draft-editor--empty">
    <p role={notice ? "alert" : "status"}>{notice?.text ?? "Loading process draft…"}</p>
  </div>;
  if (!draft)
    return (
      <div className="draft-editor draft-editor--empty">
        <p>This project has no flowsheet yet. Create one to start drawing the plant; DWSIM computes it only when you press Run.</p>
        <button
          type="button"
          className="primary-button"
          onClick={() =>
            void createDraft(workspaceId, "Process draft").then((created) => {
              accept(created);
              setDrafts([{ draft_id: created.draft_id, name: created.name, revision: created.revision, updated_at: "" }]);
            })
          }
        >
          Create process draft
        </button>
        {notice && <p role="alert">{notice.text}</p>}
      </div>
    );

  const results = draft.results;
  const energyUnsupported = registry.unsupported.EnergyStream;
  const streamResult = (tag: string) => solvedRun?.streams?.[tag]?.display;

  const routes = new Map(objects.filter((item) => item.kind === "stream").map((item) => [item.id, streamRoute(item)]));
  const renderEdges = () =>
    objects
      .filter((item) => item.kind === "stream")
      .map((stream) => {
        const points = routes.get(stream.id);
        if (!points) return null;
        const classes = [
          "draft-edge",
          isEnergy(stream) ? "draft-edge--energy" : "",
          selectedId === stream.id ? "is-selected" : "",
          proposalTargets.has(stream.id) ? "is-proposal-target" : "",
        ].join(" ");
        return (
          <g key={`edge-${stream.id}`} data-testid={`route-${stream.tag}`}>
            <path className={classes} d={pathData(points)} markerEnd={isEnergy(stream) ? "url(#draft-arrow-energy)" : "url(#draft-arrow)"} />
            {points.slice(1).map((point, index) => (
              <line
                key={index}
                className={`draft-edge-hit${editableSegment(points, index) ? (points[index].y === point.y ? " is-row" : " is-col") : ""}`}
                x1={points[index].x}
                y1={points[index].y}
                x2={point.x}
                y2={point.y}
                onPointerDown={(event) => onSegmentDown(event, stream, points, index)}
                onDoubleClick={(event) => {
                  event.stopPropagation();
                  if (editableSegment(points, index)) saveRoute(stream.id, addBend(points, index, toSvg(event)));
                }}
              />
            ))}
            {selectedId === stream.id &&
              points.slice(2, -2).map((point, offset) => (
                <rect
                  key={`bend-${offset}`}
                  className="draft-bend"
                  x={point.x - 3}
                  y={point.y - 3}
                  width={6}
                  height={6}
                  onPointerDown={(event) => event.stopPropagation()}
                  onDoubleClick={(event) => {
                    event.stopPropagation();
                    saveRoute(stream.id, removeVertex(points, offset + 2));
                  }}
                >
                  <title>Double-click to remove this bend</title>
                </rect>
              ))}
          </g>
        );
      });

  const renderBadge = (item: DraftObject, point: Point) => {
    const changes = proposalTargets.get(item.id);
    if (!changes) return null;
    const text = changes.map((change) => `${change.property.replace(/_/g, " ")}: ${formatQuantity(change.current)} → ${formatQuantity(change.proposed)}`);
    const width = Math.max(...text.map((line) => line.length)) * 6.2 + 16;
    const top = point.y - (item.kind === "unit" ? UNIT_H / 2 : STREAM_R) - 14 - text.length * 14;
    return (
      <g className="draft-proposal-badge" data-testid={`proposal-badge-${item.tag}`}>
        <rect x={point.x - width / 2} y={top} width={width} height={text.length * 14 + 8} rx={4} />
        {text.map((line, index) => (
          <text key={line} x={point.x} y={top + 14 + index * 14} textAnchor="middle">
            {line}
          </text>
        ))}
      </g>
    );
  };

  const renderObject = (item: DraftObject) => {
    const findings = draft.findings.filter((finding) => finding.object === item.tag && finding.severity === "blocker");
    const classes = [
      "draft-node",
      `draft-node--${item.kind}`,
      isEnergy(item) ? "draft-node--energy" : "",
      item.kind === "stream" && (item.spec?.culture || solvedRun?.culture?.[item.tag]) ? "is-culture-stream" : "",
      selectedId === item.id ? "is-selected" : "",
      findings.length ? "has-findings" : "",
      proposalTargets.has(item.id) ? "is-proposal-target" : "",
    ].join(" ");
    if (item.kind === "unit") {
      const point = position(item);
      const spec = specOf(item);
      const width = unitWidth(item);
      const inlets = spec?.inlets ?? [];
      const outlets = spec?.outlets ?? [];
      return (
        <g key={item.id} className={classes} onPointerDown={(event) => onPointerDown(event, item)}
          onDoubleClick={() => { if (onOpenUnit) onOpenUnit(draft.draft_id, draft.revision, item); }} data-testid={`node-${item.tag}`}
          role="button" tabIndex={0} aria-label={`${spec?.label ?? item.type} ${item.tag}`}
          data-process-unit-id={item.id}
          onContextMenu={(event) => { setContextUnitId(item.id); setSelectedId(item.id); unitMenu.targetProps.onContextMenu(event); }}
          onKeyDown={(event) => { if (onOpenUnit && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault(); onOpenUnit(draft.draft_id, draft.revision, item);
          } else { setContextUnitId(item.id); setSelectedId(item.id); unitMenu.targetProps.onKeyDown(event); } }}
          onFocus={() => { setSelectedId(item.id); setContextUnitId(item.id); }}>
          <rect x={point.x - width / 2} y={point.y - UNIT_H / 2} width={width} height={UNIT_H} rx={6} />
          <text x={point.x} y={point.y - 3} textAnchor="middle" className="draft-node__tag">{item.tag}</text>
          <text x={point.x} y={point.y + 12} textAnchor="middle" className="draft-node__type">{spec?.label ?? item.type}</text>
          <text x={point.x + width / 2 - 5} y={point.y - UNIT_H / 2 + 10} textAnchor="end" className="draft-owner-badge">
            {ownerShort(spec?.owner)}
          </text>
          {isStudioUnit(item) && item.studio?.contract.symbol ? <image className="draft-studio-symbol" href={svgDataUri(item.studio.contract.symbol)} x={point.x - width / 2 + 2} y={point.y - UNIT_H / 2 + 2} width={width - 4} height={UNIT_H - 4} preserveAspectRatio="xMidYMid meet" /> : null}
          {inlets.map((name, port) => { const anchor = portAnchor(item, "target", port, false); return <rect key={`in-${port}`} className="draft-port" data-port={`in:${name}`} x={anchor.at.x - 2} y={anchor.at.y - 3} width={4} height={6}><title>{isStudioUnit(item) ? portLabels(item, "inlets")[port] : name}</title></rect>; })}
          {outlets.map((name, port) => { const anchor = portAnchor(item, "source", port, false); return <rect key={`out-${port}`} className="draft-port" data-port={`out:${name}`} x={anchor.at.x - 2} y={anchor.at.y - 3} width={4} height={6}><title>{isStudioUnit(item) ? portLabels(item, "outlets")[port] : name}</title></rect>; })}
          {isStudioUnit(item) && <>
            {inlets.map((name, port) => { const anchor = portAnchor(item, "target", port, false); return <text key={`inl-${port}`} className="draft-port-label" x={anchor.at.x + (anchor.side === "left" ? 5 : -5)} y={anchor.at.y + 3} textAnchor={anchor.side === "left" ? "start" : "end"}>{name}</text>; })}
            {outlets.map((name, port) => { const anchor = portAnchor(item, "source", port, false); return <text key={`outl-${port}`} className="draft-port-label" x={anchor.at.x + (anchor.side === "right" ? -5 : 5)} y={anchor.at.y + 3} textAnchor={anchor.side === "right" ? "end" : "start"}>{name}</text>; })}
            <text x={point.x} y={point.y + UNIT_H / 2 + 11} textAnchor="middle" className="draft-studio-limits-label" data-testid={`studio-limits-${item.tag}`}>{`${item.studio?.version_label ?? ""} · steady only · unqualified`}</text>
          </>}
          {[...(spec?.energy_inlets ?? []), ...(spec?.energy_outlets ?? [])].map((name, index, all) => (
            <rect key={`energy-${index}`} className="draft-port--energy" x={point.x + energyPortX(all.length, index) * (item.flip_x ? -1 : 1) - 3} y={point.y + (item.flip_y ? -1 : 1) * (UNIT_H / 2 - 3)} width={6} height={6}>
              <title>{name} (energy)</title>
            </rect>
          ))}
          {renderBadge(item, point)}
        </g>
      );
    }
    const shown = streamResult(item.tag);
    const feed = !item.source;
    const point = markerAt(item, routes.get(item.id) ?? null);
    return (
      <g key={item.id} className={classes} onPointerDown={(event) => onPointerDown(event, item)} data-testid={`node-${item.tag}`}>
        <circle cx={point.x} cy={point.y} r={STREAM_R} className={feed ? "is-feed" : ""} />
        <text x={point.x} y={point.y + 20} textAnchor="middle" className="draft-node__tag">{item.tag}</text>
        {shown && (
          <text x={point.x} y={point.y + 32} textAnchor="middle" className={`draft-node__result${results.state === "stale" ? " is-stale" : ""}`}>
            <tspan x={point.x}>{[shown.temperature, shown.pressure, shown.duty].filter(Boolean).map((value) => formatQuantity(value)).join(" · ")}</tspan>
            {shown.mass_flow && <tspan x={point.x} dy={11}>{formatQuantity(shown.mass_flow)}</tspan>}
          </text>
        )}
        {renderBadge(item, point)}
      </g>
    );
  };

  const streamOptions = (end: "source" | "target", energy: boolean) =>
    units.flatMap((unit) => {
      const spec = specOf(unit);
      const ports = energy
        ? end === "source" ? spec?.energy_outlets ?? [] : spec?.energy_inlets ?? []
        : end === "source" ? spec?.outlets ?? [] : spec?.inlets ?? [];
      return ports.map((name, port) => ({ value: `${unit.id}:${port}`, label: `${unit.tag} · ${name}` }));
    });

  /** A typed unit failure from the last attempt, shown in that unit's own Results tab. */
  const unitFailure = (tag: string) => {
    const solve = lastRun?.mixed_solve;
    if (!lastRun || !solve || lastRun.status === "completed" || !failureTouchesUnit(solve, tag)) return null;
    return { code: mixedFailureCode(solve.errors), message: solve.message || mixedFailureMessage(solve.errors) };
  };

  /** Read-only DWSIM results for the selection, only after a run and bound to its revision. */
  const renderResultSection = (properties: ResultProperty[] | undefined, legacy: React.ReactNode,
                              owner: "dwsim" | "jarvis_bio" = "dwsim", title?: string) => {
    if (!solvedRun || results.state === "none") return null;
    return (
      <fieldset className={`draft-fieldset draft-outputs${results.state === "stale" ? " is-stale" : ""}`} aria-label="Results (read-only)">
        <legend>{title ?? `Results · ${ownerShort(owner)}`}{results.state === "stale" ? " (stale)" : ""}</legend>
        {properties?.length ? <ResultProperties properties={properties} stale={results.state === "stale"} label="Result properties" /> : legacy}
      </fieldset>
    );
  };

  // An energy stream's duty is an input only where the connected unit's mode reads it.
  const dutyIsInput = (stream: DraftObject) => {
    const unit = stream.target ? byId.get(stream.target.unit) : undefined;
    const spec = unit ? specOf(unit) : undefined;
    const reading = spec?.energy_spec_modes;
    if (reading) return Boolean(unit?.mode && reading.includes(unit.mode));
    return !stream.source;
  };

  const renderStreamInspector = (stream: DraftObject) => {
    const units_ = registry.quantity_units;
    const energy = isEnergy(stream);
    const feed = !stream.source;
    const sum = Object.values(composition).reduce((acc, text) => acc + (Number(text) || 0), 0);
    const shown = solvedRun?.streams?.[stream.tag];
    const cultureResult = solvedRun?.culture?.[stream.tag];
    const powerUnits = units_.power?.display ?? [];
    return (
      <>
        {(["source", "target"] as const).map((end) => (
          <label key={end} className="draft-field">
            <span>{end === "source" ? (energy ? "From (energy outlet)" : "From (unit outlet)") : energy ? "To (energy inlet)" : "To (unit inlet)"}</span>
            <select
              aria-label={end === "source" ? "Stream source" : "Stream target"}
              value={stream[end] ? `${stream[end]!.unit}:${stream[end]!.port}` : ""}
              onChange={(event) => {
                const [unit, port] = event.target.value.split(":");
                void apply(
                  event.target.value
                    ? [{ op: "connect", stream: stream.id, end, unit, port: Number(port) }]
                    : [{ op: "disconnect", stream: stream.id, end }],
                );
              }}
            >
              <option value="">{end === "source" ? "— feed (no source)" : "— product (no target)"}</option>
              {streamOptions(end, energy).map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
        ))}
        <div className="draft-route-tools">
          <span className="draft-hint">Route: drag a segment to offset it, double-click to add a bend.</span>
          <button
            type="button"
            disabled={!stream.route?.length && !routeOverrides[stream.id]?.length}
            onClick={() => saveRoute(stream.id, [])}
          >
            Reset route
          </button>
        </div>
        {energy ? (
          dutyIsInput(stream) && powerUnits.length ? (
            <fieldset className="draft-fieldset draft-inputs" aria-label="Inputs">
              <legend>Inputs</legend>
              <QuantityInput
                label="Duty"
                kind="power"
                stored={stream.spec?.duty}
                units={powerUnits}
                value={form.duty}
                onChange={(next) => setForm((current) => ({ ...current, duty: next }))}
              />
              <button
                type="button"
                disabled={!form.duty}
                onClick={() => {
                  const values = quantitiesFrom(form);
                  if (!values) return setNotice({ tone: "danger", text: "Enter numbers only." });
                  void apply([{ op: "set_stream_spec", stream: stream.id, ...values }]);
                }}
              >
                Apply duty
              </button>
            </fieldset>
          ) : (
            <p className="draft-hint">Duty is calculated by DWSIM in the connected unit&apos;s current mode.</p>
          )
        ) : feed ? (
          <>
          <fieldset className="draft-fieldset draft-inputs" aria-label="Inputs">
            <legend>Inputs · feed specification</legend>
            <label className="draft-field"><span>Thermal state</span><select aria-label="Feed thermal state basis" value={stateBasis} onChange={(event) => setStateBasis(event.target.value as typeof stateBasis)}>
              <option value="temperature">Temperature</option><option value="vapor_fraction">Vapor fraction</option>
            </select></label>
            <label className="draft-field"><span>Flow basis</span><select aria-label="Feed flow basis" value={flowBasis} onChange={(event) => setFlowBasis(event.target.value as typeof flowBasis)}>
              <option value="mass_flow">Mass flow</option><option value="molar_flow">Molar flow</option>
            </select></label>
            {registry.stream_specs.filter((spec) => spec.key === "pressure" || spec.key === stateBasis || spec.key === flowBasis).map((spec) => (
              <QuantityInput
                key={spec.key}
                label={spec.label}
                kind={spec.kind}
                stored={stream.spec?.[spec.key as "temperature" | "pressure" | "mass_flow" | "molar_flow" | "vapor_fraction"]}
                units={units_[spec.kind].display}
                value={form[spec.key]}
                onChange={(next) => setForm((current) => ({ ...current, [spec.key]: next }))}
              />
            ))}
            <button
              type="button"
              disabled={!Object.keys(form).length}
              onClick={() => {
                const values = quantitiesFrom(form);
                if (!values) return setNotice({ tone: "danger", text: "Enter numbers only." });
                delete values[stateBasis === "temperature" ? "vapor_fraction" : "temperature"];
                delete values[flowBasis === "mass_flow" ? "molar_flow" : "mass_flow"];
                void apply([{ op: "set_stream_spec", stream: stream.id, ...values }]);
              }}
            >
              Apply conditions
            </button>
            <label className="draft-field"><span>Composition basis</span><select aria-label="Composition basis" value={compositionBasis} onChange={(event) => setCompositionBasis(event.target.value as typeof compositionBasis)}>
              <option value="mass">Mass fractions</option><option value="mole">Mole fractions</option>
            </select></label>
            <table className="draft-composition" aria-label={`Composition (${compositionBasis} fractions)`}>
              <thead><tr><th>Compound</th><th>{compositionBasis === "mass" ? "Mass fraction" : "Mole fraction"}</th></tr></thead>
              <tbody>
                {draft.compounds.map((name) => (
                  <tr key={name}>
                    <td>{name}</td>
                    <td>
                      <input
                        inputMode="decimal"
                        aria-label={`${name} ${compositionBasis} fraction`}
                        value={composition[name] ?? ""}
                        onChange={(event) => setComposition((current) => ({ ...current, [name]: event.target.value }))}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot><tr><td>Sum</td><td className={Math.abs(sum - 1) > 1e-6 ? "is-invalid" : ""}>{sum.toPrecision(6)}</td></tr></tfoot>
            </table>
            {!draft.compounds.length && <p className="draft-hint">Declare compounds in Thermo first.</p>}
            <button
              type="button"
              disabled={!draft.compounds.length}
              onClick={() => {
                const entries = Object.entries(composition).filter(([, text]) => text.trim() !== "");
                if (entries.some(([, text]) => !Number.isFinite(Number(text))))
                  return setNotice({ tone: "danger", text: "Composition fractions must be numbers." });
                void apply([{ op: "set_stream_spec", stream: stream.id, composition_basis: compositionBasis,
                  composition: Object.fromEntries(entries.map(([name, text]) => [name, Number(text)])) }]);
              }}
            >
              Apply {compositionBasis} composition
            </button>
          </fieldset>
          <fieldset className="draft-fieldset draft-inputs" aria-label="Culture medium">
            <legend>Culture medium</legend>
            <label className="draft-check">
              <input type="checkbox" aria-label="This feed carries a culture" checked={cultureEnabled}
                onChange={(event) => {
                  setCultureEnabled(event.target.checked);
                  if (!event.target.checked && stream.spec?.culture)
                    void apply([{ op: "set_stream_culture", stream: stream.id, culture: null }]);
                }} />
              <span>This feed carries a culture</span>
            </label>
            {cultureEnabled && <>
              {([
                ["biomass", "Biomass X", "mass_concentration"],
                ["nitrogen", "Dissolved nitrogen (as N)", "mass_concentration"],
                ["phosphorus", "Dissolved phosphorus (as P)", "mass_concentration"],
                ["oxygen", "Dissolved O₂", "mass_concentration"],
                ["dic", "DIC", "molar_concentration"],
                ["ph", "pH", "ph"],
                ["salinity", "Salinity", "salinity"],
              ] as const).map(([key, label, kind]) => (
                <QuantityInput key={key} label={label} kind={kind}
                  stored={stream.spec?.culture?.[key]}
                  units={units_[kind]?.display ?? []}
                  value={cultureForm[key]}
                  onChange={(next) => setCultureForm((current) => ({ ...current, [key]: next }))} />
              ))}
              <button type="button" onClick={() => {
                const quantities = quantitiesFrom(cultureForm);
                if (!quantities) return setNotice({ tone: "danger", text: "Culture values must be numbers." });
                const culture = Object.fromEntries(Object.entries(quantities).filter(([key]) =>
                  ["biomass", "nitrogen", "phosphorus", "oxygen", "dic", "ph", "salinity"].includes(key)));
                if (!culture.biomass || !culture.salinity)
                  return setNotice({ tone: "danger", text: "A culture feed needs biomass and salinity." });
                void apply([{ op: "set_stream_culture", stream: stream.id, culture }]);
              }}>Apply culture</button>
            </>}
          </fieldset>
          </>
        ) : (
          <p className="draft-hint">Computed by DWSIM from its upstream unit. Only feed streams take specifications.</p>
        )}
        {!feed && cultureResult && (
          <fieldset className={`draft-fieldset draft-culture-results${results.state === "stale" ? " is-stale" : ""}`} aria-label="Culture results read-only">
            <legend>Culture · Jarvis{results.state === "stale" ? " (stale)" : ""}</legend>
            {cultureResult.status === "failed" ? <p>{cultureResult.message}</p> : <>
              <dl className="draft-results-inline">
                {Object.entries(cultureResult.values).map(([key, value]) => (
                  <div key={key}><dt>{key}</dt><dd>{formatCultureValue(value)}</dd></div>
                ))}
              </dl>
              {cultureResult.pH_reason && <p className="draft-hint">pH: {cultureResult.pH_reason}</p>}
              <p className="draft-hint">{cultureResult.fidelity}</p>
              {cultureResult.caveats?.map((caveat) => <p key={caveat} className="draft-hint">{caveat}</p>)}
            </>}
          </fieldset>
        )}
        {renderResultSection(
          shown?.properties,
          shown?.display && (
            <dl className={`draft-results-inline${results.state === "stale" ? " is-stale" : ""}`}>
              {Object.entries(shown.display).map(([key, value]) => (
                <div key={key}><dt>{key.replace("_", " ")}</dt><dd>{formatQuantity(value)}</dd></div>
              ))}
              {shown.vapor_fraction != null && <div><dt>vapor fraction</dt><dd>{shown.vapor_fraction.toPrecision(4)}</dd></div>}
            </dl>
          ),
          shown?.owner === "jarvis_bio" ? "jarvis_bio" : "dwsim",
        )}
      </>
    );
  };

  const optionControl = (param: RegistryParam, stored: OptionValue | undefined) => {
    const choices = param.options?.length ? param.options : boolOptions;
    const current = param.key in optionForm ? optionForm[param.key] : stored ?? (param.default as OptionValue | null) ?? undefined;
    return (
      <label key={param.key} className="draft-field">
        <span>{param.label}</span>
        <select
          aria-label={param.label}
          value={current === undefined ? "" : String(current)}
          onChange={(event) => {
            const picked = choices.find((choice) => String(optionValue(choice)) === event.target.value);
            if (picked !== undefined) setOptionForm((form_) => ({ ...form_, [param.key]: optionValue(picked) }));
          }}
        >
          {current === undefined && <option value="">Choose…</option>}
          {choices.map((choice) => (
            <option key={String(optionValue(choice))} value={String(optionValue(choice))}>{optionLabel(choice)}</option>
          ))}
        </select>
      </label>
    );
  };

  const renderUnitInspector = (unit: DraftObject) => {
    const spec = specOf(unit);
    if (!spec) return <p>Unsupported unit type.</p>;
    if (isStudioUnit(unit)) return <StudioUnitInspector key={`${unit.id}:${draft.revision}`} unit={unit} objects={objects} status={draft.studio_units?.[unit.id]}
      provenance={resultsView?.studio_units?.[unit.tag]} failure={unitFailure(unit.tag)} resultsState={results.state} apply={apply} navigate={navigate}
      showError={(message) => setNotice({ tone: "danger", text: message })} />;
    if (unit.type === "PhotobioreactorT1") return <PbrInspector workspaceId={workspaceId} draftId={draft.draft_id} revision={draft.revision} unit={unit} spec={spec} registry={registry}
      result={solvedRun?.units?.[unit.tag]} results={results} lastRun={lastRun} failure={unitFailure(unit.tag)} feedBasis={draft.pbr_feed_basis?.[unit.tag]}
      findings={draft.findings} apply={apply} libraryOpen={biologyOpen} openLibrary={() => setBiologyOpen(true)}
      showError={(message) => setNotice({ tone: "danger", text: message })} />;
    const reactor = unit.type === "PFR" || unit.type === "CSTR";
    const panel = (index: number) => reactor ? { role: "tabpanel", id: `${reactorTabBase}-panel-${index}`, "aria-labelledby": `${reactorTabBase}-tab-${index}`,
      hidden: reactorTab !== REACTOR_TABS[index], tabIndex: 0, className: "reactor-inspector-panel" } : {};
    const kineticsRecord = (lastRun?.units?.[unit.tag] ?? solvedRun?.units?.[unit.tag])?.kinetics;
    const typedRateLaw = reactor && (unit.reactions ?? []).some((id) => Boolean((draft.reactions?.[id] as DraftReaction | undefined)?.rate_law));
    const kineticsFailed = Boolean(reactor && kineticsRecord && !kineticsRecord.verified);
    const modes = spec.modes.map(modeKey);
    const activeMode = mode || unit.mode || "";
    const params = spec.params.filter(
      (param) => (param.classification ?? "input") === "input" && (!param.modes.length || param.modes.includes(activeMode)),
    );
    const quantities = params.filter((param) => !isOptionParam(param));
    const options = params.filter(isOptionParam);
    const connected = (end: "source" | "target", port: number, energy: boolean) =>
      objects.find((item) => item.kind === "stream" && isEnergy(item) === energy && item[end]?.unit === unit.id && item[end]?.port === port)?.tag ?? "—";
    const reported = solvedRun?.units?.[unit.tag];
    // A consumed Recycle is a DWSIM block whose result Jarvis synthesized: the result owner wins.
    const reportedOwner = reported?.owner ?? spec.owner;
    const optionsChanged = Object.keys(optionForm).length > 0;
    return (
      <>
        {reactor && <div className="reactor-tabs" role="tablist" aria-label="Reactor sections">{REACTOR_TABS.map((name, index) =>
          <button key={name} ref={(node) => { reactorTabRefs.current[index] = node; }} type="button" role="tab" id={`${reactorTabBase}-tab-${index}`}
            aria-selected={reactorTab === name} aria-controls={`${reactorTabBase}-panel-${index}`} tabIndex={reactorTab === name ? 0 : -1}
            onClick={() => setReactorTab(name)} onKeyDown={(event) => {
              const last = REACTOR_TABS.length - 1;
              const next = event.key === "ArrowRight" ? (index === last ? 0 : index + 1) : event.key === "ArrowLeft" ? (index === 0 ? last : index - 1)
                : event.key === "Home" ? 0 : event.key === "End" ? last : null;
              if (next === null) return;
              event.preventDefault(); setReactorTab(REACTOR_TABS[next]); reactorTabRefs.current[next]?.focus();
            }}>{name}</button>)}</div>}
        <div {...panel(0)}>
        <p className="draft-owner-line"><span className="draft-owner-badge-label">{ownerLabel(spec.owner)}</span> · Culture rule: {spec.culture_rule}</p>
        <dl className="draft-ports">
          {spec.inlets.map((name, port) => (<div key={`in${port}`}><dt>{name}</dt><dd>{connected("target", port, false)}</dd></div>))}
          {spec.outlets.map((name, port) => (<div key={`out${port}`}><dt>{name}</dt><dd>{connected("source", port, false)}</dd></div>))}
          {(spec.energy_inlets ?? []).map((name, port) => (<div key={`ein${port}`} className="is-energy"><dt>{name}</dt><dd>{connected("target", port, true)}</dd></div>))}
          {(spec.energy_outlets ?? []).map((name, port) => (<div key={`eout${port}`} className="is-energy"><dt>{name}</dt><dd>{connected("source", port, true)}</dd></div>))}
        </dl>
        {(modes.length > 0 || params.length > 0 || spec.reactions) && (
          <fieldset className="draft-fieldset draft-inputs" aria-label="Inputs">
            <legend>Inputs</legend>
            {modes.length > 0 && (
              <label className="draft-field">
                <span>Mode</span>
                <select aria-label="Unit mode" value={activeMode} onChange={(event) => { setMode(event.target.value); setForm({}); setOptionForm({}); }}>
                  {spec.modes.map((item) => (<option key={modeKey(item)} value={modeKey(item)}>{modeLabel(item)}</option>))}
                </select>
              </label>
            )}
            {quantities.map((param) => (
              <QuantityInput
                key={param.key}
                label={param.label}
                kind={param.kind}
                stored={activeMode === (unit.mode ?? "") ? unit.params?.[param.key] : undefined}
                units={registry.quantity_units[param.kind]?.display ?? []}
                value={form[param.key]}
                onChange={(next) => setForm((current) => ({ ...current, [param.key]: next }))}
              />
            ))}
            {options.map((param) => optionControl(param, unit.options?.[param.key]))}
            <button
              type="button"
              disabled={!Object.keys(form).length && activeMode === (unit.mode ?? "") && !optionsChanged}
              onClick={() => {
                const values = quantitiesFrom(form);
                if (!values) return setNotice({ tone: "danger", text: "Enter numbers only." });
                void apply([
                  {
                    op: "set_unit_params",
                    unit: unit.id,
                    ...(activeMode && activeMode !== unit.mode ? { mode: activeMode } : {}),
                    values,
                    ...(optionsChanged ? { options: optionForm } : {}),
                  },
                ]);
              }}
            >
              Apply inputs
            </button>
          </fieldset>
        )}
        </div>
        {reactor && <div {...panel(1)}><ReactorKinetics unit={unit} objects={objects} reactions={draft.reactions ?? {}} compounds={draft.compounds} registry={registry}
          findings={draft.findings} results={results} record={kineticsRecord} script={draft.kinetics_scripts?.[unit.tag]} apply={apply}
          showError={(message) => setNotice({ tone: "danger", text: message })} /></div>}
        <div {...panel(2)}>
        {reportedOwner === "jarvis_bio" && reported && (
          <fieldset className={`draft-fieldset draft-outputs${results.state === "stale" ? " is-stale" : ""}`} aria-label="Jarvis unit results">
            <legend>Results · Jarvis{results.state === "stale" ? " (stale)" : ""}</legend>
            <dl className="draft-results-inline">
              {Object.entries(reported.reported ?? {}).map(([key, value]) => (
                <div key={key}><dt>{key.replace(/_/g, " ")}</dt><dd>
                  {typeof value.value === "number" ? Number(value.value.toPrecision(6)) : value.value}
                  {value.units ? ` ${value.units}` : ""}
                </dd></div>
              ))}
            </dl>
            {reported.label && <p>{reported.label}</p>}
            {reported.fidelity && <p className="draft-hint">{reported.fidelity}</p>}
          </fieldset>
        )}
        {unit.type === "SpecifiedSeparator" && (
          <p className="draft-hint" data-testid="separator-derived-split">
            Derived split: R/f = {(() => {
              const recovery = Number(unit.params?.biomass_recovery?.si);
              const factor = Number(unit.params?.concentration_factor?.si);
              return Number.isFinite(recovery) && Number.isFinite(factor) && factor > 0
                ? `${(recovery / factor).toPrecision(4)}% carrier to concentrate` : "—";
            })()}
          </p>
        )}
        {reactor && (kineticsRecord || typedRateLaw) && <ReactorResults unit={unit} record={kineticsRecord} reported={reported?.reported} results={results} />}
        {reportedOwner === "dwsim" && !kineticsFailed && renderResultSection(
          reported?.properties,
          reported && Object.keys(reported.reported ?? {}).length > 0 && (
            <dl className={`draft-results-inline${results.state === "stale" ? " is-stale" : ""}`}>
              {Object.entries(reported.reported).slice(0, 8).map(([key, value]) => (
                <div key={key}><dt>{key}</dt><dd>{Number.isFinite(Number(value.value)) && value.value !== "" ? Number(Number(value.value).toPrecision(6)) : value.value} {value.units}</dd></div>
              ))}
            </dl>
          ),
          "dwsim",
          reactor && (kineticsRecord || typedRateLaw) ? "All DWSIM unit properties" : undefined,
        )}
        </div>
      </>
    );
  };

  const renderRun = (run: DraftRun) => {
    const outcome = run.outcome;
    const message = outcome?.message?.trim() ?? "";
    // Engine output beyond the outcome message; with an outcome it is folded into its Details, never repeated.
    const fresh = (text: unknown) => text != null && String(text).trim() !== "" && String(text).trim() !== message;
    const solveMessages = run.solve && run.status !== "completed" ? solveFailureMessages(run).filter(fresh) : [];
    const dwsimMessage = fresh(run.error_detail?.dwsim_message);
    const runError = run.status !== "materialization_failed" && fresh(run.error);
    const alert = outcome ? undefined : "alert";
    const engineOutput = dwsimMessage || runError || solveMessages.length ? (
      <>
        {dwsimMessage && run.error_detail && <p className="draft-dwsim-error" role={alert}>DWSIM: {String(run.error_detail.dwsim_message)}</p>}
        {runError && <p className="draft-dwsim-error" role={alert}>{run.error}</p>}
        {solveMessages.length > 0 && <div className="draft-dwsim-error" role={alert}>
          <strong>Solve failed</strong>
          {solveMessages.map((text, index) => <p key={`${text}-${index}`}>{text}</p>)}
        </div>}
      </>
    ) : null;
    return (
      <section className={`draft-run draft-run--${run.status}${outcome ? ` draft-run--outcome-${outcome.state}` : ""}`} aria-label="Last DWSIM attempt">
        <header>
          <strong>{run.action === "validate" ? "Validate" : run.mixed_solve ? "Mixed solve" : "Run"}{outcome ? "" : `: ${run.status.replace(/_/g, " ")}`}</strong>
          <span>revision {run.draft_revision.split(":")[0]}{run.compile_seconds != null ? ` · ${run.compile_seconds} s` : ""}</span>
        </header>
        {outcome && <OutcomeDetails outcome={outcome} streamName={streamName} details={engineOutput} />}
        {run.retry_of && <SettingsDiff rows={run.retry_settings_diff ?? []} caption={`Retry of run ${run.retry_of.slice(0, 8)}`} />}
        {run.action === "run" && (
          <div className="draft-solver-actions">
            <button type="button" disabled={busy !== null || blockers.length > 0}
              title="Solve the current revision again, linked to this run; change solver settings in Setup first to retry with them"
              onClick={() => void act("run", undefined, run.run_id)}>Retry on current revision</button>
          </div>
        )}
        {run.mixed_solve && run.action === "run" && (
          <section className="draft-mixed-summary" aria-label="Mixed solve summary">
            {/* The outcome above already states converged / not converged / failed; only a culture-only loop needs a word. */}
            {(!outcome || (run.status === "completed" && run.mixed_solve.culture_only)) && <p role="status">
              {run.status === "completed" && run.mixed_solve.culture_only
                ? "Converged · culture-only loop, solved as a culture fixed point on DWSIM flows (no cross-engine tear to iterate)"
                : run.status === "completed"
                ? `Converged in ${run.mixed_solve.history?.length ?? 0} ${(run.mixed_solve.history?.length ?? 0) === 1 ? "iteration" : "iterations"} · max residual ${(() => {
                  const last = run.mixed_solve.history?.[Math.max(0, (run.mixed_solve.history?.length ?? 1) - 1)]?.max_normalized_residual;
                  return last == null ? "empty" : Number(last).toPrecision(4);
                })()}`
                : run.status === "unconverged"
                  ? `Not converged (${stopReasonText(run.mixed_solve.reason)}) — last iterate`
                  : `Segment failed: ${run.mixed_solve.failed_units?.length ? run.mixed_solve.failed_units.join(", ") : run.mixed_solve.failed_segment ?? "unknown"} · ${mixedFailureCode(run.mixed_solve.errors) ? `${mixedFailureCode(run.mixed_solve.errors)} · ` : ""}${run.mixed_solve.message || mixedFailureMessage(run.mixed_solve.errors)}`}
            </p>}
            {fresh(run.mixed_solve.diagnosis) && <p className="draft-hint">{run.mixed_solve.diagnosis}</p>}
            <dl className="draft-solver-diagnostics" aria-label="Solver diagnostics">
              {!outcome && <div><dt>Stop reason</dt><dd>{stopReasonText(run.mixed_solve.reason)}</dd></div>}
              {!run.mixed_solve.diagnostics && run.mixed_solve.method && <div><dt>Method</dt><dd>{methodLabel(run.mixed_solve.method)}</dd></div>}
              {diagnosticRows(run.mixed_solve.diagnostics).filter((row) => !(outcome?.iterations != null && row.label === "Iterations"))
                .map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}
            </dl>
            {run.mixed_solve.diagnostics?.recommendation && <p className="draft-hint" role="note">{run.mixed_solve.diagnostics.recommendation}</p>}
            <details>
              <summary>{run.mixed_solve.culture_only
                ? "Convergence · culture-only (no tear iteration)"
                : <>Convergence · {run.mixed_solve.history?.length ?? 0} {(run.mixed_solve.history?.length ?? 0) === 1 ? "iteration" : "iterations"}</>}</summary>
              <div className="draft-convergence-table">
                <table>
                  <thead><tr><th>Iteration</th><th>Max normalized residual</th><th>ω</th><th>Worst field</th></tr></thead>
                  <tbody>{(run.mixed_solve.history ?? []).map((row) => (
                    <tr key={row.iteration}><td>{row.iteration}</td>
                      <td>{row.max_normalized_residual == null
                        ? `no finite value${row.non_finite ? ` (${row.non_finite}; null/value mismatch${row.pattern_mismatch_fields?.length ? `: ${row.pattern_mismatch_fields.join(", ")}` : ""})` : ""}`
                        : Number(row.max_normalized_residual).toPrecision(5)}</td>
                      <td>{row.omega.toPrecision(3)}</td>
                      <td>{row.worst_field || "—"}</td></tr>
                  ))}</tbody>
                </table>
              </div>
              <div className="draft-segment-list" aria-label="Owner and segment listing">
                {(run.mixed_solve.partition?.segments ?? []).filter((segment) => Array.isArray(segment?.units)).map((segment) => (
                  <p key={segment.id}><strong>DWSIM · segment {segment.id + 1}</strong> · level {segment.level}: {segment.units.join(", ")}</p>
                ))}
                <p><strong>Jarvis</strong>: {objects.filter((item) => item.kind === "unit" && specOf(item)?.owner === "jarvis_bio").map((item) => item.tag).join(", ") || "culture propagation"}</p>
              </div>
            </details>
          </section>
        )}
        {run.mixed_solve && run.action === "run" && run.status !== "completed" && Object.keys(run.streams ?? {}).length > 0 && (
          <details className="draft-last-iterate">
            <summary>Not converged — last iterate results</summary>
            <div className="draft-convergence-table">
              <table>
                <thead><tr><th>Stream</th><th>Owner</th><th>Temperature</th><th>Pressure</th><th>Mass flow</th></tr></thead>
                <tbody>{Object.entries(run.streams ?? {}).map(([tag, stream]) => (
                  <tr key={tag}><td>{tag}</td><td>{ownerShort(stream.owner)}</td>
                    <td>{formatQuantity(stream.display?.temperature)}</td>
                    <td>{formatQuantity(stream.display?.pressure)}</td>
                    <td>{formatQuantity(stream.display?.mass_flow)}</td></tr>
                ))}</tbody>
              </table>
            </div>
          </details>
        )}
        {run.status === "materialization_mismatch" && (
          <>
            <p>DWSIM did not reproduce the draft exactly, so nothing was solved. Differences:</p>
            <table className="draft-diffs">
              <thead><tr><th>Path</th><th>Draft expects</th><th>DWSIM holds</th></tr></thead>
              <tbody>
                {(run.materialization_diffs ?? []).map((diff) => (
                  <tr key={diff.path}><td>{diff.path}</td><td>{String(diff.expected)}</td><td>{String(diff.actual)}</td></tr>
                ))}
              </tbody>
            </table>
            <button type="button" disabled={busy !== null} onClick={() => void act(run.action, run.draft_revision)}>Retry</button>
          </>
        )}
        {run.status === "materialization_failed" && (
          <p>DWSIM refused step {String(run.error_detail?.step ?? "?")}.{outcome ? "" : ` ${run.error ?? ""}`}{" "}
            <button type="button" disabled={busy !== null} onClick={() => void act(run.action, run.draft_revision)}>Retry</button></p>
        )}
        {!outcome && engineOutput}
        {run.dwsim_check && run.dwsim_check.findings.length > 0 && (
          <ul className="draft-findings">
            {run.dwsim_check.findings.map((finding, index) => (
              <li key={index} data-severity={finding.severity}><strong>{finding.object || "Flowsheet"}</strong> {finding.message} {finding.fix && <em>{finding.fix}</em>}</li>
            ))}
          </ul>
        )}
      </section>
    );
  };

  const renderResults = () => {
    // Before the first run the same tables show the draft's rows with `empty` cells under a grey "No run yet".
    const stale = resultsView != null && resultsView.run_id === results.run_id && results.state === "stale";
    return (
      <>
        <ResultsWorkspace view={resultsView} layout={resultsFrame} stale={stale} compounds={draft.compounds} onSelectTag={selectTag}
          staleNote={stale && resultsView ? `${results.edits_since} edit(s) since run ${resultsView.run_id.slice(0, 8)} on revision ${resultsView.draft_revision.split(":")[0]}. Values below describe that revision, not the current draft.` : undefined} />
        {resultsView && Object.entries(resultsView.studio_units ?? {}).map(([tag, provenance]) => <StudioProvenance key={tag} tag={tag} provenance={provenance} />)}
        {solvedRun && resultsView?.run_id === solvedRun.run_id && (
          <dl className="draft-provenance">
            <div><dt>Revision</dt><dd>{solvedRun.draft_revision}</dd></div>
            <div><dt>Materialization</dt><dd title={solvedRun.materialization_fingerprint}>{solvedRun.materialization_fingerprint?.slice(0, 19) ?? "—"}…</dd></div>
            <div><dt>Compiler / DWSIM</dt><dd>{solvedRun.compiler_version} / {solvedRun.dwsim_version} · MCP {solvedRun.mcp_sha256.slice(0, 8)}</dd></div>
          </dl>
        )}
      </>
    );
  };

  // The toolbar shows the last attempt's outcome only while it describes the revision on screen.
  const outcomeHere = results.last_attempt?.outcome && results.last_attempt.draft_revision === draft.revision ? results.last_attempt.outcome : null;
  const selectTag = (tag: string) => setSelectedId(objects.find((item) => item.tag === tag)?.id ?? null);
  const thermoFieldset = (
    <fieldset className="draft-fieldset" aria-label="Thermo">
      <legend>Thermo</legend>
      <label className="draft-field">
        <span>Property package</span>
        <select
          aria-label="Property package"
          value={draft.property_package ?? ""}
          onChange={(event) => void apply([{ op: "set_thermo", property_package: event.target.value }])}
        >
          <option value="" disabled>Choose…</option>
          {registry.property_packages.map((name) => (<option key={name} value={name}>{name}</option>))}
        </select>
      </label>
      <div className="draft-compounds" role="group" aria-label="Compounds">
        {registry.compounds.map((name) => (
          <label key={name}>
            <input
              type="checkbox"
              checked={draft.compounds.includes(name)}
              onChange={(event) =>
                void apply([{ op: "set_thermo", compounds: event.target.checked ? [...draft.compounds, name] : draft.compounds.filter((item) => item !== name) }])
              }
            />
            {name}
          </label>
        ))}
      </div>
      <p className="draft-hint">Reactions are defined on each reactor (select a PFR or CSTR).</p>
    </fieldset>
  );

  const solverPanel = (
    <div id="process-solver-console" ref={solverConsoleRef} tabIndex={-1} aria-label="Solver"
      className={`draft-console-anchor draft-solver-panel${consoleFloating ? " is-floating" : ""}`}
      onKeyDown={(event) => { if (consoleFloating && event.key === "Escape") setConsoleFloating(false); }}>
      <header className="draft-solver-panel__head">
        <h3>Solver</h3>
        {consoleFloating ? <button type="button" onClick={() => setConsoleFloating(false)}>Dock below flowsheet</button> : null}
      </header>
      <SolverSection solver={draft.solver} onApply={(solver) => apply([{ op: "set_solver", solver }])} />
      {/* Before any run the console shows its readout with `empty` values; the last outcome replaces it after a run. */}
      {solve ? <SolverConsole solve={solve} events={solveEvents} onStop={requestStop} streamName={streamName} />
        : !lastRun && <SolverConsole solve={null} events={[]} onStop={requestStop} />}
      {lastRun && (!solve || !solve.live) && renderRun(lastRun)}
    </div>
  );

  return (
    <div className="draft-editor" onKeyDown={onEditorKeyDown}>
      <div className="draft-toolbar" role="toolbar" aria-label="Process actions">
        <span className="draft-revision">revision {draft.seq}</span>
        <button type="button" onClick={() => setBiologyOpen((open) => !open)}>Biology models…</button>
        <button type="button" ref={setupToggleRef} className="draft-setup-toggle" aria-expanded={setupOpen} aria-controls="draft-setup-drawer"
          onClick={() => (setupOpen ? closeSetup() : openSetup())}>{setupOpen ? "Close setup" : "Setup"}</button>
        <button type="button" className="draft-run-button" disabled={busy !== null || blockers.length > 0} onClick={() => void act("run")}>
          {busy === "run"
            ? registry.units.some((unit) => unit.owner === "jarvis_bio") && objects.some((item) => item.kind === "unit" && specOf(item)?.owner === "jarvis_bio")
              ? "Running mixed solve (DWSIM + Jarvis)…" : "Running…"
            : "Run"}
        </button>
        <button type="button" className="draft-console-link" aria-controls="process-solver-console" aria-expanded={consoleFloating}
          onClick={() => { setConsoleFloating(true); window.requestAnimationFrame(() => showSolverConsole(true)); }}>
          {solve?.state === "running" || solve?.state === "stopping" ? "Live solver console" : "Solver console"}
        </button>
        <RunStateIndicator blockerCount={shownBlockerCount} reason={outcomeHere ? outcomeHere.reason : null}
          state={runState(busy, blockers.length, outcomeHere, Boolean(outcomeHere))} />
        <span className={`draft-readiness ${blockers.length ? "has-blockers" : "is-ready"}`} data-testid="readiness-chip">
          {blockers.length ? `${shownBlockerCount} blocker${shownBlockerCount === 1 ? "" : "s"}` : "No blockers"} · {shownWarningCount} warning{shownWarningCount === 1 ? "" : "s"}
        </span>
        <span className={`draft-state draft-state--${results.state}`} data-testid="results-state">
          {/* Spec 229: names its subsystem, so it is never read as the Dynamic Studio prediction. */}
          Steady-state solve · {results.state === "none" ? "No results" : results.state === "current" ? "Results current" : `Results stale (${results.edits_since} edits)`}
        </span>
        <details className="draft-secondary"><summary>More controls</summary>
          <label className="draft-field draft-field--inline"><span>Draft</span><select aria-label="Process draft" value={draft.draft_id} onChange={(event) => {
            setSolve(null);
            setSolveEvents([]);
            setLastRun(null);
            setSolvedRun(null);
            setBusy(null);
            void loadDraft(event.target.value);
          }}>
            {drafts.map((row) => (<option key={row.draft_id} value={row.draft_id}>{row.name}</option>))}
          </select></label>
          <button type="button" onClick={() => void createDraft(workspaceId, `Process draft ${drafts.length + 1}`).then((created) => { accept(created); setDrafts((rows) => [{ draft_id: created.draft_id, name: created.name, revision: created.revision, updated_at: "" }, ...rows]); })}>New draft</button>
          <button type="button" disabled={busy !== null || blockers.length > 0} title={blockers.length ? "Resolve the findings first" : "Compile this revision into DWSIM and run its check"} onClick={() => void act("validate")}>{busy === "validate" ? "Validating…" : "Validate (DWSIM)"}</button>
          <button type="button" className="draft-history-toggle" onClick={() => void (revisions ? setRevisions(null) : listDraftRevisions(workspaceId, draft.draft_id).then(setRevisions))}>{revisions ? "Hide history" : "History"}</button>
        </details>
      </div>
      {biologyOpen && <BiologyModelLibrary workspaceId={workspaceId} onClose={() => setBiologyOpen(false)} />}
      {notice && <p className={`draft-notice draft-notice--${notice.tone}`} role="alert">{notice.text}</p>}
      <div className={`draft-body${selected ? " draft-body--inspector" : ""}${selected && (selected.type === "PhotobioreactorT1" || selected.type === "PFR" || selected.type === "CSTR") ? " draft-body--wide-inspector" : ""}`}>
        <aside className="draft-palette" aria-label="Palette">
          <h3>Add</h3>
          <button type="button" onClick={() => addStream("material")}>Material stream</button>
          <button
            type="button"
            className="draft-palette__energy"
            disabled={energyUnsupported !== undefined}
            title={energyUnsupported}
            onClick={() => addStream("energy")}
          >
            Energy stream
          </button>
          {registry.units.some((unit) => unit.owner === "jarvis_bio") && <>
            <h3>Jarvis units</h3>
            {registry.units.filter((unit) => unit.owner === "jarvis_bio").map((unit) => (
              <button key={unit.type} type="button" onClick={() => addUnit(unit.type)}>{unit.label}</button>
            ))}
          </>}
          <h3>Studio units</h3>
          {(registry.studio_units ?? []).length ? (registry.studio_units ?? []).map((entry) => (
            <button key={entry.model_spec_id} type="button" data-testid="palette-studio-unit" title={`${entry.label} ${entry.version_label}: ${paletteSpec(entry).inlets.join(", ")} to ${paletteSpec(entry).outlets.join(", ")}. ${entry.limits}`}
              onClick={() => addStudioUnit(entry)}>{entry.label} <small>{entry.version_label}</small></button>
          )) : <small className="draft-hint">Published process units from Studio appear here.</small>}
          {[...new Set(registry.units.filter((unit) => unit.owner !== "jarvis_bio").map((unit) => unit.owner))].map((owner) => <Fragment key={owner}>
            <h3>{ownerShort(owner)} units</h3>
            {registry.units.filter((unit) => unit.owner === owner).map((unit) => (
              <button key={unit.type} type="button" onClick={() => addUnit(unit.type)}>{unit.label}</button>
            ))}
          </Fragment>)}
          <details className="draft-unsupported-group"><summary>Not yet supported ({Object.keys(registry.unsupported).length})</summary>
            <ul className="draft-unsupported">
              {Object.entries(registry.unsupported).map(([type, reason]) => (
                <li key={type}>
                  <button type="button" disabled aria-disabled="true" title={reason} data-unsupported={type}>{type}</button>
                  <small>{reason}</small>
                </li>
              ))}
            </ul>
          </details>
        </aside>
        <div className="draft-canvas-wrap">
          <div className="draft-canvas-frame">
          <svg
            ref={viewport.attach}
            tabIndex={-1}
            className="draft-canvas"
            role="img"
            aria-label="Process flowsheet draft"
            data-testid="flowsheet-canvas"
            viewBox={`${viewport.viewBox.x} ${viewport.viewBox.y} ${viewport.viewBox.w} ${viewport.viewBox.h}`}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onPointerDown={viewport.onBackgroundDown}
          >
            <defs>
              <marker id="draft-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" />
              </marker>
              <marker id="draft-arrow-energy" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" />
              </marker>
            </defs>
            {renderEdges()}
            {objects.map(renderObject)}
          </svg>
          <div className="draft-viewport-controls" role="toolbar" aria-label="Flowsheet view">
            <button type="button" aria-label="Zoom out" disabled={viewport.zoom <= ZOOM_MIN + 1e-9} onClick={viewport.zoomOut}>−</button>
            <output data-testid="zoom-level" aria-label="Zoom level">{Math.round(viewport.zoom * 100)}%</output>
            <button type="button" aria-label="Zoom in" disabled={viewport.zoom >= ZOOM_MAX - 1e-9} onClick={viewport.zoomIn}>+</button>
            <button type="button" aria-pressed={viewport.fitted} onClick={viewport.fitAll}>Fit all</button>
            <button type="button" onClick={viewport.reset}>Reset</button>
          </div>
          {setupOpen && (
            <section id="draft-setup-drawer" ref={setupRef} className="draft-setup-drawer" aria-labelledby="draft-setup-title" tabIndex={-1}>
              <header><h3 id="draft-setup-title">Setup · species and thermodynamics</h3>
                <button type="button" onClick={closeSetup} aria-label="Close setup">Close</button></header>
              {thermoFieldset}
              <p className="draft-hint">Solver method, iterations and tolerances are set in the Solver panel under the flowsheet.</p>
            </section>
          )}
          </div>
          <ContextMenu label="Unit orientation" items={orientationItems} at={unitMenu.at} onClose={unitMenu.close} />
        </div>
        {selected ? (
          <aside className="draft-inspector" aria-label="Inspector" data-testid="draft-inspector">
            <div className="draft-inspector__title"><h3>{selected.kind === "unit" ? specOf(selected)?.label : "Material stream"} {selected.tag}</h3>
              {selected.kind === "unit" && <MenuButton label={`Orientation for ${selected.tag}`} items={[
                { id: "mirror-lr", label: selected.flip_x ? "Unmirror left ↔ right" : "Mirror left ↔ right", onSelect: () => void apply([{ op: "set_orientation", id: selected.id, flip_x: !selected.flip_x }]) },
                { id: "mirror-tb", label: selected.flip_y ? "Unmirror top ↕ bottom" : "Mirror top ↕ bottom", onSelect: () => void apply([{ op: "set_orientation", id: selected.id, flip_y: !selected.flip_y }]) },
              ]}>Orientation ▾</MenuButton>}
              <button type="button" className="draft-inspector__close" aria-label="Close inspector" title="Close inspector (Esc)" onClick={() => setSelectedId(null)}>×</button>
            </div>
            <div className="draft-rename">
              {selected.kind === "unit" && onOpenUnit ? <button type="button" onClick={() => onOpenUnit(draft.draft_id, draft.revision, selected)}>Deepen in Design</button> : null}
              <input aria-label="Tag" value={rename} onChange={(event) => setRename(event.target.value)} />
              <button type="button" disabled={!rename || rename === selected.tag} onClick={() => void apply([{ op: "rename", id: selected.id, tag: rename }])}>Rename</button>
              <button type="button" className="draft-delete" onClick={() => { void apply([{ op: "delete", id: selected.id }]); setSelectedId(null); }}>Delete</button>
            </div>
            {selected.type !== "EnergyStream" && <ObjectResults view={resultsView} layout={resultsFrame} compounds={draft.compounds}
              kind={selected.kind === "stream" ? "stream" : "unit"} tag={selected.tag}
              stale={resultsView != null && resultsView.run_id === results.run_id && results.state === "stale"} onSelectTag={selectTag} />}
            {selected.kind === "stream" ? renderStreamInspector(selected) : renderUnitInspector(selected)}
          </aside>
        ) : null}
      </div>
      {/* Below the fixed-height flowsheet box, reachable with normal page scrolling. */}
      <div className="draft-below">
        <ProcessProposals workspaceId={workspaceId} draftId={draft.draft_id} proposals={draft.proposals} onDecided={() => void loadDraft(draft.draft_id)} />
        {/* Warnings alone stay folded so a runnable flowsheet is not buried under them; blockers open the list. */}
        <details key={blockers.length ? "blocked" : "runnable"} className="draft-findings-panel" aria-label="Draft findings" open={blockers.length > 0}>
          <summary><h3>Readiness guidance: {countLabel}</h3></summary>
          <p className="draft-hint">Blockers stop Run. Warnings allow a solve but may make its result physically meaningless.</p>
          <ul className="draft-findings">
            {shownFindings.map((finding, index) => (
              <li key={index} data-severity={finding.severity}>
                <button type="button" onClick={() => setSelectedId(objects.find((item) => item.tag === finding.object)?.id ?? null)}>
                  <strong>{finding.object || "Draft"}</strong> {finding.message}
                </button>
              </li>
            ))}
          </ul>
        </details>
        {/* One Solver panel: the settings the next Run uses, the live console and the last outcome. Run opens it
            floating over the page (portalled to the shell root, so no page container can clip or cover it while the
            shell's styles still apply); docking it puts it back here under the flowsheet. The portal wrapper keeps the
            editor's scoped styles. */}
        {consoleFloating ? createPortal(<div className="draft-editor draft-solver-portal">{solverPanel}</div>,
          document.querySelector(".application-shell") ?? document.body) : solverPanel}
        <RunComparisonPanel key={draft.draft_id} workspaceId={workspaceId} draftId={draft.draft_id} refreshKey={lastRun?.run_id ?? ""} streamName={streamName} />
        {renderResults()}
        {revisions && (
          <section className="draft-history" aria-label="Revision history">
            <ol>
              {revisions.map((row) => (
                <li key={row.revision}>
                  <span>#{row.seq} · {row.actor} · {row.ops.join(", ") || "created"}</span>
                  {row.revision !== draft.revision && (
                    <button type="button" onClick={() => void restoreDraftRevision(workspaceId, draft.draft_id, draft.revision, row.revision).then((next) => { accept(next); setRevisions(null); }).catch((cause) => setNotice({ tone: "danger", text: errorText(cause) }))}>
                      Restore
                    </button>
                  )}
                </li>
              ))}
            </ol>
          </section>
        )}
      </div>
      {/* Dynamic simulation is its own Design stage; the standalone editor points there with this draft. */}
      {dynamicLink ? <p className="draft-dynamic-link">Simulate this draft over time (scenarios, runs and results) in{" "}
        {navigate ? <AppLink href={designViewHref({ stage: "Dynamic", componentId: null, node: STAGE_NODE.Dynamic, draftId: draft.draft_id }, "")} navigate={navigate}>Design › Dynamic</AppLink>
          : "Design › Dynamic"}.</p> : null}
    </div>
  );
}
