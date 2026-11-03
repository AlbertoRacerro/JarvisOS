import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, ReactNode } from "react";
import type {
  EditorCommand,
  EditorObjectRead,
  EditorProjectionRead,
  RevisionRead,
} from "../api/generated/dwsimEditor";
import {
  createDwsimCase,
  DwsimEditorError,
  dwsimRevisionDownloadUrl,
  getDwsimProjection,
  importDwsimCase,
  listDwsimCases,
  listDwsimRevisions,
  restoreDwsimRevision,
  runDwsimCommand,
} from "../api/dwsimEditor";
import type { EditorCaseRead } from "../api/generated/dwsimEditor";
import type { PrimaryStageProps } from "./registry";
import ProcessDraftEditor from "./ProcessDraftEditor";
import "./ProcessStage.css";

import NumericInput from "../components/ui/NumericInput";
const unitTypes = [
  "Mixer",
  "Splitter",
  "Heater",
  "Cooler",
  "Pump",
  "Valve",
  "Tank",
  "HeatExchanger",
];
type Point = { x: number; y: number };
type Drag = { id: string; origin: Point; pointer: Point };
type PanGesture = { clientX: number; clientY: number; origin: Point };
const unavailableText = (error: DwsimEditorError) =>
  error.status === 503
    ? `DWSIM unavailable · ${error.message} (${error.code})`
    : `${error.message} (${error.code})`;
const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const display = (value: unknown) =>
  typeof value === "string" || typeof value === "number"
    ? String(value)
    : value == null
      ? "—"
      : JSON.stringify(value);
const optionalNumber = (value: string) => {
  if (!value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};
const projectedBoundaryResidual = (projection: EditorProjectionRead) => {
  const objects = Array.isArray(projection.objects) ? projection.objects : [];
  const units = new Set(
    objects
      .filter((item) => item?.category === "unit")
      .map((item) => item.native_id)
      .filter((id): id is string => typeof id === "string"),
  );
  const streams = new Map(
    objects
      .filter(
        (item) =>
          item?.category === "material_stream" &&
          typeof item.native_id === "string",
      )
      .map((item) => [item.native_id as string, item]),
  );
  const inlets = new Set<string>();
  const outlets = new Set<string>();
  for (const edge of Array.isArray(projection.connections)
    ? projection.connections
    : []) {
    if (edge.kind !== "material") continue;
    if (streams.has(edge.source_native_id) && units.has(edge.target_native_id))
      inlets.add(edge.source_native_id);
    if (units.has(edge.source_native_id) && streams.has(edge.target_native_id))
      outlets.add(edge.target_native_id);
  }
  if (!inlets.size || !outlets.size) return null;
  const flow = (id: string) => {
    const value = record(streams.get(id)?.results).mass_flow_kg_s;
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  };
  const inletFlows = [...inlets].map(flow);
  const outletFlows = [...outlets].map(flow);
  if (inletFlows.some((value) => value === null) || outletFlows.some((value) => value === null))
    return null;
  return (
    (inletFlows as number[]).reduce((sum, value) => sum + value, 0) -
    (outletFlows as number[]).reduce((sum, value) => sum + value, 0)
  );
};
function readProjectKnowledgeHandoff() {
  const params = new URLSearchParams(window.location.search);
  const revisionId = params.get("project_knowledge_revision_id")?.trim();
  const basisDigest = params.get("project_knowledge_basis_digest")?.trim();
  const validationSetDigest = params
    .get("project_knowledge_validation_set_digest")
    ?.trim();
  const requirementIds = params
    .getAll("project_knowledge_requirement_id")
    .map((value) => value.trim())
    .filter(Boolean);
  const present = Array.from(params.keys()).some((key) =>
    key.startsWith("project_knowledge_"),
  );
  return present
    ? {
        revisionId,
        basisDigest,
        validationSetDigest,
        requirementIds,
        valid: Boolean(
          revisionId &&
          basisDigest &&
          validationSetDigest &&
          requirementIds.length,
        ),
      }
    : null;
}

type ProcessMode = "draft" | "native";
const EnvironmentPanel = lazy(() => import("./EnvironmentPanel"));

function ProcessModeTabs({ mode, onChange }: { mode: ProcessMode; onChange(next: ProcessMode): void }) {
  return (
    <div className="process-mode-tabs" role="tablist" aria-label="Process editor">
      <button type="button" role="tab" aria-selected={mode === "draft"} onClick={() => onChange("draft")}>
        Flowsheet draft
      </button>
      <button type="button" role="tab" aria-selected={mode === "native"} onClick={() => onChange("native")}>
        Native DWSIM cases (advanced)
      </button>
    </div>
  );
}

/**
 * Spec 155: the Jarvis-owned draft is the primary editor; DWSIM is contacted only on
 * Validate/Run. The 149 native case editor stays available for imported cases and dynamics.
 */
function ProcessStage(props: PrimaryStageProps) {
  const { workspaceId, navigate, onShellRegionsChange } = props;
  const [mode, setMode] = useState<ProcessMode>("draft");
  const [environmentOpen, setEnvironmentOpen] = useState(false);
  useEffect(() => {
    if (mode === "draft") onShellRegionsChange({});
  }, [mode, onShellRegionsChange]);
  const tabs = <ProcessModeTabs mode={mode} onChange={setMode} />;
  if (mode === "native") return <NativeDwsimCaseEditor {...props} modeTabs={tabs} />;
  return (
    <section className="process-stage design-stage" aria-labelledby="process-draft-title" data-testid="process-draft-surface">
      <header className="design-stage__header process-stage__header">
        <div className="design-stage__title-row">
          <div>
            <p className="eyebrow">Design · Process</p>
            <h1 id="process-draft-title">Process workspace</h1>
            <p className="panel-subtitle">
              Your plant flowsheet. Every edit is saved immediately; DWSIM computes it only when you press Run.
            </p>
          </div>
        </div>
        <details className="process-advanced"><summary>Advanced · Native DWSIM cases</summary>{tabs}</details>
        <div className="process-environment-toolbar"><button type="button" onClick={() => setEnvironmentOpen(true)}>Environment…</button></div>
      </header>
      {workspaceId ? <ProcessDraftEditor workspaceId={workspaceId} navigate={navigate} /> : <p>Select a workspace to edit its process draft.</p>}
      {environmentOpen && workspaceId ? <Suspense fallback={<p role="status">Loading environment profiles…</p>}><EnvironmentPanel workspaceId={workspaceId} onClose={() => setEnvironmentOpen(false)} /></Suspense> : null}
    </section>
  );
}

function NativeDwsimCaseEditor({
  workspaceId,
  navigate,
  onShellRegionsChange,
  modeTabs,
}: PrimaryStageProps & { modeTabs: ReactNode }) {
  const [cases, setCases] = useState<EditorCaseRead[]>([]);
  const [caseId, setCaseId] = useState("");
  const [projection, setProjection] = useState<EditorProjectionRead | null>(
    null,
  );
  const [revisions, setRevisions] = useState<RevisionRead[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [runtimeUnavailable, setRuntimeUnavailable] = useState<{
    code: string;
    message: string;
  } | null>(null);
  const [tag, setTag] = useState("");
  const [unitType, setUnitType] = useState(unitTypes[0]);
  const [rename, setRename] = useState("");
  const [temperature, setTemperature] = useState("");
  const [pressure, setPressure] = useState("");
  const [massFlow, setMassFlow] = useState("");
  const [molarFlow, setMolarFlow] = useState("");
  const [composition, setComposition] = useState("");
  const [unitProperties, setUnitProperties] = useState("{}");
  const [compoundText, setCompoundText] = useState("Water");
  const [packageName, setPackageName] = useState("Steam Tables");
  const [connectUnit, setConnectUnit] = useState("");
  const [connectStream, setConnectStream] = useState("");
  const [connectRole, setConnectRole] = useState<
    "feed" | "product" | "energy_feed" | "energy_product"
  >("feed");
  const [connectPort, setConnectPort] = useState("0");
  const [controllerTag, setControllerTag] = useState("");
  const [controllerSp, setControllerSp] = useState("");
  const [controllerKp, setControllerKp] = useState("");
  const [controllerKi, setControllerKi] = useState("");
  const [controllerKd, setControllerKd] = useState("");
  const [eventSet, setEventSet] = useState("default");
  const [eventTag, setEventTag] = useState("");
  const [eventProperty, setEventProperty] = useState("");
  const [eventValue, setEventValue] = useState("");
  const [eventAt, setEventAt] = useState("0");
  const [eventDescription, setEventDescription] = useState("");
  const [runDuration, setRunDuration] = useState("60");
  const [runStep, setRunStep] = useState("");
  const [runMethod, setRunMethod] = useState<"" | "ExplicitEuler" | "RungeKutta4" | "ImplicitEuler" | "AdaptiveRK45">("");
  const [runSchedule, setRunSchedule] = useState("");
  const [stateName, setStateName] = useState("");
  const [restoreState, setRestoreState] = useState("");
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const [placeX, setPlaceX] = useState("40");
  const [placeY, setPlaceY] = useState("40");
  const [drag, setDrag] = useState<Drag | null>(null);
  const [panGesture, setPanGesture] = useState<PanGesture | null>(null);
  const [ghost, setGhost] = useState<Point | null>(null);
  const [waitCancelled, setWaitCancelled] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const flight = useRef(false);
  const selected = useMemo(
    () =>
      projection?.objects.find((item) => item.native_id === selectedId) ?? null,
    [projection, selectedId],
  );
  useEffect(() => {
    onShellRegionsChange({
      sidecar: selected ? (
        <div className="dwsim-sidecar-inspector">
          <strong>
            {selected.tag ?? selected.type ?? "Unnamed DWSIM object"}
          </strong>
          <dl>
            <div>
              <dt>Type</dt>
              <dd>{selected.type ?? "—"}</dd>
            </div>
            <div>
              <dt>Native ID</dt>
              <dd>{selected.native_id ?? "—"} · case-local</dd>
            </div>
            <div>
              <dt>Category</dt>
              <dd>{selected.category}</dd>
            </div>
            <div>
              <dt>Calculation state</dt>
              <dd>
                {selected.calculated == null
                  ? "Unknown"
                  : selected.calculated
                    ? "Calculated"
                    : "Not calculated"}
              </dd>
            </div>
            {selected.errors && (
              <div>
                <dt>Errors</dt>
                <dd>{selected.errors}</dd>
              </div>
            )}
          </dl>
          <p>
            Engineering graph and results are read from DWSIM revision{" "}
            {projection?.revision.slice(0, 18)}.
          </p>
        </div>
      ) : undefined,
    });
  }, [onShellRegionsChange, projection?.revision, selected]);
  const safeObjects = useMemo(
    () =>
      Array.isArray(projection?.objects)
        ? projection.objects.filter((item) => item && typeof item === "object")
        : [],
    [projection],
  );
  const geometryIds = useMemo(
    () =>
      new Set(
        safeObjects
          .map((item) => item.native_id)
          .filter(
            (id): id is string => typeof id === "string" && id.length > 0,
          ),
      ),
    [safeObjects],
  );
  const connections = useMemo(() => {
    const all = Array.isArray(projection?.connections)
      ? projection.connections
      : [];
    const valid = all.filter(
      (edge) =>
        edge &&
        typeof edge === "object" &&
        typeof edge.source_native_id === "string" &&
        typeof edge.target_native_id === "string" &&
        geometryIds.has(edge.source_native_id) &&
        geometryIds.has(edge.target_native_id),
    );
    return { valid, invalidCount: all.length - valid.length };
  }, [geometryIds, projection]);
  const missingCoordinateCount = safeObjects.filter(
    (item) =>
      typeof item.x !== "number" ||
      !Number.isFinite(item.x) ||
      typeof item.y !== "number" ||
      !Number.isFinite(item.y),
  ).length;
  const unknownCategoryCount = safeObjects.filter(
    (item) =>
      !["unit", "material_stream", "energy_stream", "other"].includes(
        item.category,
      ),
  ).length;

  const loadCase = useCallback(
    async (id: string) => {
      if (!workspaceId || !id) return;
      setCaseId(id);
      setLoading(true);
      try {
        const [view, history] = await Promise.all([
          getDwsimProjection(workspaceId, id),
          listDwsimRevisions(workspaceId, id),
        ]);
        setProjection(view);
        setRevisions(history);
        setRuntimeUnavailable(null);
        setCaseId(id);
        setSelectedId(null);
        setMessage("");
      } catch (cause) {
        if (cause instanceof DwsimEditorError) {
          if (cause.status === 503)
            setRuntimeUnavailable({ code: cause.code, message: cause.message });
          setProjection(null);
          setMessage(unavailableText(cause));
        } else
          setMessage(
            cause instanceof Error
              ? cause.message
              : "Could not load DWSIM projection.",
          );
      } finally {
        setLoading(false);
      }
    },
    [workspaceId],
  );

  const refreshCases = useCallback(async () => {
    if (!workspaceId) return;
    try {
      const rows = await listDwsimCases(workspaceId);
      setCases(rows);
      if (
        rows.length &&
        (!caseId || !rows.some((item) => item.case_id === caseId))
      )
        await loadCase(rows[0].case_id);
      if (!rows.length) {
        setCaseId("");
        setProjection(null);
        setRevisions([]);
      }
    } catch (cause) {
      if (cause instanceof DwsimEditorError && cause.status === 503)
        setRuntimeUnavailable({ code: cause.code, message: cause.message });
      setMessage(
        cause instanceof Error
          ? unavailableText(
              cause instanceof DwsimEditorError
                ? cause
                : new DwsimEditorError(500, "request_failed", cause.message),
            )
          : "Could not list DWSIM cases.",
      );
    }
  }, [caseId, loadCase, workspaceId]);
  useEffect(() => {
    void refreshCases();
  }, [refreshCases]);

  type CommandPayload = EditorCommand extends infer Command
    ? Command extends EditorCommand
      ? Omit<Command, "expected_revision">
      : never
    : never;
  const command = useCallback(
    async (payload: CommandPayload) => {
      if (
        !workspaceId ||
        !caseId ||
        !projection ||
        !Array.isArray(projection.editable_commands) ||
        !projection.editable_commands.includes(payload.kind)
      )
        return;
      if (flight.current) return;
      flight.current = true;
      setLoading(true);
      setWaitCancelled(false);
      setMessage(`Applying ${payload.kind}…`);
      try {
        const result = await runDwsimCommand(workspaceId, caseId, {
          ...payload,
          expected_revision: projection.revision,
        } as EditorCommand);
        setProjection(result.projection);
        setRuntimeUnavailable(null);
        setMessage(
          `${payload.kind} read back as revision ${result.projection.revision.slice(0, 12)}.`,
        );
        try {
          setRevisions(await listDwsimRevisions(workspaceId, caseId));
        } catch (cause) {
          setMessage(
            `${payload.kind} read back as revision ${result.projection.revision.slice(0, 12)}. Revision history refresh failed: ${cause instanceof Error ? cause.message : "unknown error"}`,
          );
        }
      } catch (cause) {
        if (cause instanceof DwsimEditorError && cause.status === 409) {
          await loadCase(caseId);
          setMessage(
            "Changed elsewhere. The stale edit was discarded; review the refreshed server projection before editing again.",
          );
        } else if (cause instanceof DwsimEditorError) {
          if (cause.status === 503)
            setRuntimeUnavailable({ code: cause.code, message: cause.message });
          setMessage(unavailableText(cause));
        } else
          setMessage(
            cause instanceof Error ? cause.message : "DWSIM command failed.",
          );
      } finally {
        flight.current = false;
        setLoading(false);
      }
    },
    [caseId, loadCase, projection, workspaceId],
  );

  const createCase = async () => {
    if (!workspaceId || loading || flight.current || runtimeUnavailable) return;
    flight.current = true;
    setLoading(true);
    try {
      const created = await createDwsimCase(
        workspaceId,
        tag.trim() || "DWSIM case",
      );
      setCases((all) => [created, ...all]);
      setTag("");
      await loadCase(created.case_id);
    } catch (cause) {
      if (cause instanceof DwsimEditorError && cause.status === 503)
        setRuntimeUnavailable({ code: cause.code, message: cause.message });
      setMessage(
        cause instanceof DwsimEditorError
          ? unavailableText(cause)
          : "Could not create DWSIM case.",
      );
    } finally {
      flight.current = false;
      setLoading(false);
    }
  };
  const can = (kind: string) =>
    Boolean(
      Array.isArray(projection?.editable_commands) &&
      projection.editable_commands.includes(kind) &&
      !loading &&
      !flight.current &&
      !runtimeUnavailable,
    );
  const unsupported = (kind: string) =>
    (projection?.unsupported_commands &&
    typeof projection.unsupported_commands === "object"
      ? projection.unsupported_commands[kind]
      : undefined) ??
    (runtimeUnavailable
      ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
      : "Command is not available in this projection.");
  const dynamicsProjection = record(projection?.dynamics);
  const dynamicsUnavailableReason =
    typeof dynamicsProjection.unavailable_reason === "string"
      ? dynamicsProjection.unavailable_reason
      : null;
  const canDynamic = (kind: string) =>
    !dynamicsUnavailableReason && can(kind);
  const dynamicDraftDisabled =
    !projection || loading || Boolean(runtimeUnavailable) || Boolean(dynamicsUnavailableReason);
  const projectedSavedStates = Array.isArray(dynamicsProjection.saved_states)
    ? dynamicsProjection.saved_states.filter(
        (name): name is string => typeof name === "string",
      )
    : [];
  const pointAt = (event: ReactPointerEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    return {
      x: Math.round(
        (((event.clientX - bounds.left) * 1000) / bounds.width - pan.x) / zoom,
      ),
      y: Math.round(
        (((event.clientY - bounds.top) * 700) / bounds.height - pan.y) / zoom,
      ),
    };
  };
  const paletteCreate = (
    kind: "create_unit" | "create_material_stream" | "create_energy_stream",
    selectedType?: string,
  ) => {
    const p = { x: Number(placeX) || 0, y: Number(placeY) || 0 };
    const nextTag =
      tag.trim() ||
      `${selectedType ?? (kind === "create_unit" ? "Unit" : kind === "create_energy_stream" ? "Energy" : "Stream")}${safeObjects.length + 1}`;
    if (kind === "create_unit")
      void command({
        kind,
        unit_type: selectedType ?? unitType,
        tag: nextTag,
        ...p,
      });
    else if (kind === "create_material_stream")
      void command({
        kind,
        tag: nextTag,
        ...p,
        temperature: null,
        pressure: null,
        mass_flow: null,
        composition: null,
      });
    else void command({ kind, tag: nextTag, ...p });
    setTag("");
  };
  const submitInspector = () => {
    if (!projection || !selected?.tag) return;
    if (rename.trim() && rename.trim() !== selected.tag) {
      void command({
        kind: "rename",
        object: selected.tag,
        new_tag: rename.trim(),
      });
      return;
    }
    if (selected.category === "material_stream") {
      const parseQuantity = (value: string, unit: string) =>
        value.trim() ? { value: Number(value), unit } : null;
      let fractions: Record<string, number> | null = null;
      if (composition.trim()) {
        try {
          fractions = JSON.parse(composition) as Record<string, number>;
        } catch {
          setMessage(
            "Composition must be a JSON object of compound mass fractions.",
          );
          return;
        }
      }
      void command({
        kind: "set_stream_conditions",
        stream: selected.tag,
        temperature: parseQuantity(temperature, "K"),
        pressure: parseQuantity(pressure, "Pa"),
        mass_flow: parseQuantity(massFlow, "kg/s"),
        molar_flow: parseQuantity(molarFlow, "mol/s"),
        composition: fractions,
      });
    } else if (selected.category === "unit") {
      try {
        void command({
          kind: "set_unit_properties",
          unit: selected.tag,
          properties: JSON.parse(unitProperties) as Record<string, unknown>,
        });
      } catch {
        setMessage("Unit properties must be valid JSON.");
      }
    }
  };
  const restoreRevision = async (revision: string) => {
    if (!workspaceId || !caseId || !projection || loading || runtimeUnavailable)
      return;
    if (flight.current) return;
    flight.current = true;
    setLoading(true);
    setMessage("Restoring revision…");
    try {
      const result = await restoreDwsimRevision(
        workspaceId,
        caseId,
        projection.revision,
        revision,
      );
      setProjection(result.projection);
      setRevisions(await listDwsimRevisions(workspaceId, caseId));
      setMessage(
        `Restored as revision ${result.projection.revision.slice(0, 12)}.`,
      );
    } catch (cause) {
      if (cause instanceof DwsimEditorError && cause.status === 409) {
        await loadCase(caseId);
        setMessage(
          "Changed elsewhere. Restore was discarded; the current case was refreshed.",
        );
      } else
        setMessage(
          cause instanceof DwsimEditorError
            ? unavailableText(cause)
            : "Restore failed.",
        );
    } finally {
      flight.current = false;
      setLoading(false);
    }
  };
  const selectedIdText = selected?.native_id
    ? `${selected.native_id} · case-local`
    : "—";
  const solve = record(projection?.last_solve);
  const returnedResidual = solve.mass_balance_residual_kg_s;
  const boundaryResidual = projection
    ? projectedBoundaryResidual(projection)
    : null;
  const solveObjects = Array.isArray(solve.object_calculation)
    ? solve.object_calculation
        .filter((item) => item && typeof item === "object")
        .map(record)
    : [];
  const selectedResults = record(selected?.results);
  const handoff = readProjectKnowledgeHandoff();

  return (
    <section
      className="process-stage design-stage"
      aria-labelledby="process-stage-title"
      data-testid="process-editor-surface"
    >
      <header className="design-stage__header process-stage__header">
        <div className="design-stage__title-row">
          <div>
            <p className="eyebrow">Design · DWSIM</p>
            <h1 id="process-stage-title">Process workspace</h1>
            <p className="panel-subtitle">
              Interactive editor backed by the current immutable DWSIM case
              revision.
            </p>
          </div>
          <span
            className={`design-stage__truth-state ${runtimeUnavailable ? "is-unavailable" : ""}`}
          >
            {runtimeUnavailable
              ? "DWSIM unavailable"
              : projection
                ? "DWSIM available"
                : "DWSIM status unknown"}
          </span>
        </div>
        {modeTabs}
        <div className="dwsim-truth">
          <label>
            Case
            <select
              aria-label="DWSIM case"
              value={caseId}
              disabled={loading}
              onChange={(e) => void loadCase(e.target.value)}
            >
              <option value="">Select a case…</option>
              {cases.map((item) => (
                <option key={item.case_id} value={item.case_id}>
                  {item.case_id.slice(0, 8)} · rev{" "}
                  {
                    item.revision.split(":")[
                      item.revision.split(":").length - 1
                    ]
                  }
                </option>
              ))}
            </select>
          </label>
          <label>
            New case name
            <input
              value={tag}
              onChange={(e) => setTag(e.target.value)}
              aria-label="New case name"
              disabled={loading || Boolean(runtimeUnavailable)}
              title={
                runtimeUnavailable
                  ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                  : undefined
              }
            />
          </label>
          <button
            type="button"
            disabled={loading || Boolean(runtimeUnavailable)}
            title={
              runtimeUnavailable
                ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                : "Create an empty native DWSIM case"
            }
            onClick={() => void createCase()}
          >
            New blank case
          </button>
          <button
            type="button"
            disabled={loading || Boolean(runtimeUnavailable)}
            title={
              runtimeUnavailable
                ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                : "Import a native DWSIM case"
            }
            onClick={() => fileRef.current?.click()}
          >
            Import .dwxmz / .dwxml
          </button>
          <button
            type="button"
            disabled={loading || !caseId}
            onClick={() => void loadCase(caseId)}
          >
            Refresh projection
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".dwxmz,.dwxml"
            hidden
            onChange={(e) => {
              const input = e.currentTarget;
              const file = input.files?.[0];
              if (
                !file ||
                !workspaceId ||
                loading ||
                flight.current ||
                runtimeUnavailable
              )
                return;
              flight.current = true;
              setLoading(true);
              void importDwsimCase(workspaceId, file)
                .then(async (created) => {
                  setCases((all) => [created, ...all]);
                  await loadCase(created.case_id);
                })
                .catch((cause: unknown) => {
                  if (cause instanceof DwsimEditorError && cause.status === 503)
                    setRuntimeUnavailable({
                      code: cause.code,
                      message: cause.message,
                    });
                  setMessage(
                    cause instanceof DwsimEditorError
                      ? unavailableText(cause)
                      : "Import failed.",
                  );
                })
                .finally(() => {
                  flight.current = false;
                  setLoading(false);
                  input.value = "";
                });
            }}
          />
        </div>
        <div className="dwsim-truth__meta">
          <span>
            Revision · <code>{projection?.revision.slice(0, 18) ?? "—"}</code>
          </span>
          <span>DWSIM · {projection?.dwsim_version ?? "—"}</span>
          <span>Last solve · {display(solve.solve_status ?? "not run")}</span>
          {typeof returnedResidual === "number" &&
          Number.isFinite(returnedResidual) ? (
            <span>
              Residual · {display(returnedResidual)} kg/s · source:
              last_solve.mass_balance_residual_kg_s
            </span>
          ) : boundaryResidual !== null ? (
            <span>
              Residual · {display(boundaryResidual)} kg/s · source: projected
              boundary streams (inlet total − outlet total)
            </span>
          ) : (
            <span>
              Residual · unavailable · source: {display(solve.mass_balance_status ?? "not returned")}
            </span>
          )}
        </div>
      </header>
      {projection?.last_solve && (
        <details className="dwsim-solve-results">
          <summary>
            Solve read-back · per-object calculation ({solveObjects.length})
          </summary>
          <ul>
            {solveObjects.map((item, index) => (
              <li key={`${display(item.tag)}:${index}`}>
                <strong>{display(item.tag)}</strong> ·{" "}
                {item.calculated === true
                  ? "calculated"
                  : item.calculated === false
                    ? "not calculated"
                    : "calculation state unknown"}
                {item.errors ? ` · ${display(item.errors)}` : ""}
              </li>
            ))}
          </ul>
          {typeof solve.mass_balance_status === "string" && (
            <p>
              Mass balance status · {display(solve.mass_balance_status)}
              {solve.mass_balance_error
                ? ` · ${display(solve.mass_balance_error)}`
                : ""}
            </p>
          )}
        </details>
      )}
      {handoff && (
        <div
          className="final-fusion__context-strip"
          role="status"
          aria-label="Project Knowledge recomputation handoff"
        >
          {handoff.valid ? (
            <>
              Project Knowledge recomputation request context · revision{" "}
              {handoff.revisionId} · basis {handoff.basisDigest} · validation
              set {handoff.validationSetDigest} · requirements{" "}
              {handoff.requirementIds.join(", ")}. Context is inspectable only;
              This editor cannot launch recomputation from the handoff.
            </>
          ) : (
            <>
              Incomplete Project Knowledge recomputation handoff ignored. No
              Process action is authorized from partial local URL context.
            </>
          )}
        </div>
      )}
      {runtimeUnavailable && (
        <div className="dwsim-error" role="alert">
          <strong>DWSIM unavailable · {runtimeUnavailable.code}</strong>
          <span>
            {runtimeUnavailable.message}. Editing controls are disabled until
            the runtime is available.
          </span>
        </div>
      )}
      {message && (
        <div className="dwsim-message" role="status" aria-live="polite">
          {message}
          {loading && flight.current && (
            <button
              type="button"
              onClick={() => {
                setWaitCancelled(true);
                setMessage(
                  "UI wait canceled; DWSIM command continues on the server.",
                );
              }}
            >
              Cancel UI wait
            </button>
          )}
          {waitCancelled && (
            <span>
              Server command remains in flight; its projection will still be
              applied when it returns.
            </span>
          )}
        </div>
      )}
      <div className="dwsim-workbench">
        <aside className="dwsim-palette" aria-label="DWSIM palette">
          <h2>Palette</h2>
          <label>
            Tag
            <input
              value={tag}
              onChange={(e) => setTag(e.target.value)}
              aria-label="Object tag"
              disabled={Boolean(runtimeUnavailable) || loading}
            />
          </label>
          <label>
            Unit type
            <select
              value={unitType}
              onChange={(e) => setUnitType(e.target.value)}
              disabled={Boolean(runtimeUnavailable) || loading}
            >
              {unitTypes.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </label>
          <label>
            Canvas X
            <NumericInput
              aria-label="Canvas X"
              value={placeX}
              onValueChange={(text) => setPlaceX(text)}
              disabled={Boolean(runtimeUnavailable) || loading}
            />
          </label>
          <label>
            Canvas Y
            <NumericInput
              aria-label="Canvas Y"
              value={placeY}
              onValueChange={(text) => setPlaceY(text)}
              disabled={Boolean(runtimeUnavailable) || loading}
            />
          </label>
          <button
            disabled={!can("create_unit")}
            title={
              !can("create_unit")
                ? unsupported("create_unit")
                : "Add selected unit at the canvas coordinates"
            }
            onClick={() => paletteCreate("create_unit")}
          >
            Add unit
          </button>
          <button
            disabled={!can("create_material_stream")}
            title={
              !can("create_material_stream")
                ? unsupported("create_material_stream")
                : "Add a material stream at the canvas coordinates"
            }
            onClick={() => paletteCreate("create_material_stream")}
          >
            Material stream
          </button>
          <button
            disabled={!can("create_energy_stream")}
            title={
              !can("create_energy_stream")
                ? unsupported("create_energy_stream")
                : "Add an energy stream at the canvas coordinates"
            }
            onClick={() => paletteCreate("create_energy_stream")}
          >
            Energy stream
          </button>
          <h3>Available unit types</h3>
          <div className="dwsim-palette__types">
            {unitTypes.map((item) => (
              <button
                key={item}
                disabled={!can("create_unit")}
                title={
                  !can("create_unit")
                    ? unsupported("create_unit")
                    : `Add ${item}`
                }
                onClick={() => paletteCreate("create_unit", item)}
              >
                {item}
              </button>
            ))}
          </div>
          <h3>Command availability</h3>
          <div className="dwsim-command-catalogue">
            {(projection?.editable_commands ?? []).map((kind) => (
              <span key={kind} title="Available in current server projection">
                {kind} · available
              </span>
            ))}
            {Object.entries(projection?.unsupported_commands ?? {}).map(
              ([kind, reason]) => (
                <span key={kind} title={reason}>
                  {kind} · unsupported: {reason}
                </span>
              ),
            )}
          </div>
        </aside>
        <main className="dwsim-canvas-wrap">
          <div className="dwsim-canvas-toolbar">
            <button
              type="button"
              onClick={() => {
                setZoom(1);
                setPan({ x: 0, y: 0 });
              }}
            >
              Fit view
            </button>
            <button
              type="button"
              aria-label="Zoom out"
              onClick={() => setZoom((z) => Math.max(0.4, z - 0.1))}
            >
              −
            </button>
            <span>{Math.round(zoom * 100)}%</span>
            <button
              type="button"
              aria-label="Zoom in"
              onClick={() => setZoom((z) => Math.min(2, z + 0.1))}
            >
              +
            </button>
            <button
              disabled={!can("solve")}
              title={!can("solve") ? unsupported("solve") : "Run DWSIM solve"}
              onClick={() => void command({ kind: "solve" })}
            >
              Solve
            </button>
          </div>
          {projection ? (
            <svg
              className="dwsim-canvas"
              role="group"
              aria-label="DWSIM process canvas. Select an object to inspect; drag an object to move it or drag the empty canvas to pan."
              viewBox="0 0 1000 700"
              onPointerDown={(event) => {
                if (event.target === event.currentTarget) {
                  setPanGesture({
                    clientX: event.clientX,
                    clientY: event.clientY,
                    origin: pan,
                  });
                  event.currentTarget.setPointerCapture(event.pointerId);
                }
              }}
              onPointerMove={(event) => {
                if (panGesture) {
                  const bounds = event.currentTarget.getBoundingClientRect();
                  setPan({
                    x:
                      panGesture.origin.x +
                      ((event.clientX - panGesture.clientX) * 1000) /
                        bounds.width,
                    y:
                      panGesture.origin.y +
                      ((event.clientY - panGesture.clientY) * 700) /
                        bounds.height,
                  });
                } else if (drag) setGhost(pointAt(event));
              }}
              onPointerUp={(event) => {
                if (panGesture) {
                  setPanGesture(null);
                  event.currentTarget.releasePointerCapture?.(event.pointerId);
                  return;
                }
                if (!drag) return;
                const p = pointAt(event);
                setGhost(null);
                setDrag(null);
                event.currentTarget.releasePointerCapture?.(event.pointerId);
                const dragged =
                  Math.hypot(
                    event.clientX - drag.pointer.x,
                    event.clientY - drag.pointer.y,
                  ) > 3;
                if (
                  dragged &&
                  drag.id &&
                  (p.x !== drag.origin.x || p.y !== drag.origin.y)
                ) {
                  const object = safeObjects.find(
                    (item) => item.native_id === drag.id,
                  );
                  if (object?.tag)
                    void command({
                      kind: "move",
                      object: object.tag,
                      x: p.x,
                      y: p.y,
                    });
                }
              }}
            >
              <g transform={`translate(${pan.x} ${pan.y}) scale(${zoom})`}>
                {connections.valid.map((edge, index) => {
                  const a = safeObjects.find(
                    (item) => item.native_id === edge.source_native_id,
                  );
                  const b = safeObjects.find(
                    (item) => item.native_id === edge.target_native_id,
                  );
                  if (
                    !a ||
                    !b ||
                    a.x == null ||
                    a.y == null ||
                    b.x == null ||
                    b.y == null
                  )
                    return null;
                  return (
                    <path
                      key={`${edge.source_native_id}:${edge.source_port}:${edge.target_native_id}:${edge.target_port}:${index}`}
                      className={`dwsim-edge dwsim-edge--${edge.kind}`}
                      d={`M ${a.x + 55} ${a.y + 25} C ${a.x + 120} ${a.y + 25}, ${b.x - 50} ${b.y + 25}, ${b.x} ${b.y + 25}`}
                    />
                  );
                })}
                {safeObjects.map((object, index) => {
                  const id = object.native_id;
                  if (
                    !id ||
                    typeof object.x !== "number" ||
                    !Number.isFinite(object.x) ||
                    typeof object.y !== "number" ||
                    !Number.isFinite(object.y) ||
                    ![
                      "unit",
                      "material_stream",
                      "energy_stream",
                      "other",
                    ].includes(object.category)
                  )
                    return null;
                  const pos =
                    drag?.id === id && ghost
                      ? ghost
                      : { x: object.x, y: object.y };
                  const w =
                    object.width && object.width > 20
                      ? Math.min(150, object.width)
                      : object.category === "unit"
                        ? 96
                        : 92;
                  const h =
                    object.height && object.height > 20
                      ? Math.min(90, object.height)
                      : 48;
                  return (
                    <g
                      key={id}
                      className={`dwsim-node dwsim-node--${object.category}`}
                      transform={`translate(${pos.x},${pos.y})`}
                      role="button"
                      tabIndex={0}
                      aria-label={`${object.tag ?? object.type ?? "DWSIM object"}, ${object.category}`}
                      onClick={() => {
                        setSelectedId(id);
                        setRename(object.tag ?? "");
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setSelectedId(id);
                          setRename(object.tag ?? "");
                        }
                      }}
                      onPointerDown={(e) => {
                        if (e.button !== 0 || loading || runtimeUnavailable)
                          return;
                        e.stopPropagation();
                        const start = { x: object.x ?? 0, y: object.y ?? 0 };
                        setSelectedId(id);
                        setRename(object.tag ?? "");
                        setDrag({
                          id,
                          origin: start,
                          pointer: { x: e.clientX, y: e.clientY },
                        });
                        setGhost(start);
                        e.currentTarget.ownerSVGElement?.setPointerCapture(
                          e.pointerId,
                        );
                      }}
                    >
                      <rect
                        width={w}
                        height={h}
                        rx={object.category === "unit" ? 8 : 20}
                        className={selectedId === id ? "is-selected" : ""}
                      />
                      <text x={w / 2} y={h / 2 - 2} textAnchor="middle">
                        {object.tag ?? object.type ?? `Object ${index + 1}`}
                      </text>
                      <text
                        className="dwsim-node__type"
                        x={w / 2}
                        y={h / 2 + 13}
                        textAnchor="middle"
                      >
                        {object.type ?? object.category}
                      </text>
                      {object.errors && (
                        <text
                          className="dwsim-node__badge dwsim-node__badge--error"
                          x={w - 6}
                          y={9}
                        >
                          !
                        </text>
                      )}
                      {object.calculated === true && (
                        <text className="dwsim-node__badge" x={w - 8} y={h - 7}>
                          ✓
                        </text>
                      )}
                    </g>
                  );
                })}
              </g>
            </svg>
          ) : (
            <div className="dwsim-empty">
              <strong>
                {runtimeUnavailable
                  ? "No process topology is loaded."
                  : loading
                    ? "Loading DWSIM projection…"
                    : "Create or import a DWSIM case to begin."}
              </strong>
              <span>
                A graph appears only after the server returns a real DWSIM
                projection.
              </span>
            </div>
          )}
          {connections.invalidCount > 0 && (
            <div className="dwsim-warning" role="status">
              {connections.invalidCount} connection record(s) reference missing
              or invalid objects and were not drawn.
            </div>
          )}
          {missingCoordinateCount > 0 && (
            <div className="dwsim-warning" role="status">
              {missingCoordinateCount} projected object(s) have missing or
              invalid coordinates and were not drawn.
            </div>
          )}
          {(unknownCategoryCount > 0 ||
            safeObjects.some((item) => item.category === "other")) && (
            <div className="dwsim-warning" role="status">
              {unknownCategoryCount +
                safeObjects.filter((item) => item.category === "other")
                  .length}{" "}
              unrecognized object category object(s) are shown with neutral
              styling or omitted from the canvas.
            </div>
          )}
          <details className="dwsim-evidence">
            <summary>
              Read-back evidence · projection SHA-256 {projection?.case_sha256}
            </summary>
            <pre>{JSON.stringify(projection, null, 2)}</pre>
          </details>
        </main>
        <aside
          className="dwsim-inspector"
          aria-label="Selected DWSIM object properties"
        >
          <h2>Inspector</h2>
          {selected ? (
            <>
              <strong>
                {selected.tag ?? selected.type ?? "Unnamed object"}
              </strong>
              <dl>
                <dt>Type</dt>
                <dd>{selected.type ?? "—"}</dd>
                <dt>Native ID</dt>
                <dd>{selectedIdText}</dd>
                <dt>Calculation</dt>
                <dd>
                  {selected.calculated == null
                    ? "Unknown"
                    : selected.calculated
                      ? "Calculated"
                      : "Not calculated"}
                </dd>
                {selected.errors && (
                  <>
                    <dt>Errors</dt>
                    <dd className="dwsim-error-text">{selected.errors}</dd>
                  </>
                )}
              </dl>
              <label>
                Tag
                <input
                  aria-label="Rename selected object"
                  value={rename}
                  onChange={(e) => setRename(e.target.value)}
                  disabled={loading || Boolean(runtimeUnavailable)}
                  title={
                    runtimeUnavailable
                      ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                      : undefined
                  }
                />
              </label>
              <button
                disabled={!can("rename")}
                title={
                  !can("rename") ? unsupported("rename") : "Rename in DWSIM"
                }
                onClick={() =>
                  void command({
                    kind: "rename",
                    object: selected.tag ?? "",
                    new_tag: rename.trim(),
                  })
                }
              >
                Rename
              </button>
              {selected.category === "material_stream" && (
                <>
                  <h3>Stream conditions</h3>
                  <p>
                    Server results · T {display(selectedResults.temperature_K)}{" "}
                    K · P {display(selectedResults.pressure_Pa)} Pa · mass flow{" "}
                    {display(selectedResults.mass_flow_kg_s)} kg/s · molar flow{" "}
                    {display(selectedResults.molar_flow_mol_s)} mol/s ·
                    composition{" "}
                    {display(
                      selectedResults.composition ??
                        selectedResults.mass_fractions,
                    )}
                  </p>
                  <p>
                    Inputs · T K · pressure Pa · mass flow kg/s · molar flow
                    mol/s
                  </p>
                  <label>
                    Temperature (K)
                    <NumericInput
                      value={temperature}
                      onValueChange={(text) => setTemperature(text)}
                      disabled={loading || Boolean(runtimeUnavailable)}
                      title={
                        runtimeUnavailable
                          ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                          : undefined
                      }
                    />
                  </label>
                  <label>
                    Pressure (Pa)
                    <NumericInput
                      value={pressure}
                      onValueChange={(text) => setPressure(text)}
                      disabled={loading || Boolean(runtimeUnavailable)}
                      title={
                        runtimeUnavailable
                          ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                          : undefined
                      }
                    />
                  </label>
                  <label>
                    Mass flow (kg/s)
                    <NumericInput
                      value={massFlow}
                      onValueChange={(text) => setMassFlow(text)}
                      disabled={loading || Boolean(runtimeUnavailable)}
                      title={
                        runtimeUnavailable
                          ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                          : undefined
                      }
                    />
                  </label>
                  <label>
                    Molar flow (mol/s)
                    <NumericInput
                      value={molarFlow}
                      onValueChange={(text) => setMolarFlow(text)}
                      disabled={loading || Boolean(runtimeUnavailable)}
                      title={
                        runtimeUnavailable
                          ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                          : undefined
                      }
                    />
                  </label>
                  <label>
                    Mass fractions JSON
                    <input
                      value={composition}
                      onChange={(e) => setComposition(e.target.value)}
                      placeholder='{"Water":1}'
                      disabled={loading || Boolean(runtimeUnavailable)}
                      title={
                        runtimeUnavailable
                          ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                          : undefined
                      }
                    />
                  </label>
                  <button
                    disabled={!can("set_stream_conditions")}
                    title={
                      !can("set_stream_conditions")
                        ? unsupported("set_stream_conditions")
                        : "Apply stream conditions"
                    }
                    onClick={submitInspector}
                  >
                    Apply stream conditions
                  </button>
                </>
              )}
              {selected.category === "unit" && (
                <>
                  <h3>Unit properties</h3>
                  <label>
                    Property values JSON
                    <textarea
                      rows={4}
                      value={unitProperties}
                      onChange={(e) => setUnitProperties(e.target.value)}
                      disabled={loading || Boolean(runtimeUnavailable)}
                      title={
                        runtimeUnavailable
                          ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                          : undefined
                      }
                    />
                  </label>
                  <button
                    disabled={!can("set_unit_properties")}
                    title={
                      !can("set_unit_properties")
                        ? unsupported("set_unit_properties")
                        : "Apply DWSIM properties"
                    }
                    onClick={submitInspector}
                  >
                    Apply unit properties
                  </button>
                </>
              )}
              {selected.results && (
                <details>
                  <summary>Read-only results</summary>
                  <pre>{JSON.stringify(selected.results, null, 2)}</pre>
                </details>
              )}
              <button
                disabled={!can("delete_object")}
                title={
                  !can("delete_object")
                    ? unsupported("delete_object")
                    : "Delete object"
                }
                onClick={() =>
                  selected.tag &&
                  void command({ kind: "delete_object", object: selected.tag })
                }
              >
                Delete object
              </button>
            </>
          ) : (
            <p>Select an object in the server-backed canvas to inspect it.</p>
          )}
          <h3>Thermodynamics</h3>
          <p>Current package · {projection?.property_package ?? "not set"}</p>
          <label>
            Compounds (comma separated)
            <input
              value={compoundText}
              onChange={(e) => setCompoundText(e.target.value)}
              disabled={loading || Boolean(runtimeUnavailable)}
              title={
                runtimeUnavailable
                  ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                  : undefined
              }
            />
          </label>
          <button
            disabled={!can("add_compounds")}
            title={
              !can("add_compounds")
                ? unsupported("add_compounds")
                : "Add compounds"
            }
            onClick={() =>
              void command({
                kind: "add_compounds",
                compounds: compoundText
                  .split(",")
                  .map((v) => v.trim())
                  .filter(Boolean),
              })
            }
          >
            Add compounds
          </button>
          <label>
            Property package
            <input
              value={packageName}
              onChange={(e) => setPackageName(e.target.value)}
              disabled={loading || Boolean(runtimeUnavailable)}
              title={
                runtimeUnavailable
                  ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                  : undefined
              }
            />
          </label>
          <button
            disabled={!can("set_property_package")}
            title={
              !can("set_property_package")
                ? unsupported("set_property_package")
                : "Set property package"
            }
            onClick={() =>
              void command({ kind: "set_property_package", name: packageName })
            }
          >
            Set property package
          </button>
          <p>Compounds · {projection?.compounds.join(", ") || "none"}</p>
          <h3>Connect objects</h3>
          <label>
            Unit
            <select
              aria-label="Connect unit"
              value={connectUnit}
              onChange={(e) => setConnectUnit(e.target.value)}
              disabled={loading || Boolean(runtimeUnavailable)}
              title={
                runtimeUnavailable
                  ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                  : undefined
              }
            >
              {safeObjects
                .filter((o) => o.category === "unit" && o.tag)
                .map((o) => (
                  <option key={o.native_id} value={o.tag!}>
                    {o.tag}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Stream
            <select
              aria-label="Connect stream"
              value={connectStream}
              onChange={(e) => setConnectStream(e.target.value)}
              disabled={loading || Boolean(runtimeUnavailable)}
              title={
                runtimeUnavailable
                  ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                  : undefined
              }
            >
              {safeObjects
                .filter(
                  (o) =>
                    (o.category === "material_stream" ||
                      o.category === "energy_stream") &&
                    o.tag,
                )
                .map((o) => (
                  <option key={o.native_id} value={o.tag!}>
                    {o.tag}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Role
            <select
              aria-label="Connect role"
              value={connectRole}
              onChange={(e) =>
                setConnectRole(e.target.value as typeof connectRole)
              }
            >
              <option value="feed">feed</option>
              <option value="product">product</option>
              <option value="energy_feed">energy_feed</option>
              <option value="energy_product">energy_product</option>
            </select>
          </label>
          <label>
            Port
            <NumericInput
              aria-label="Connect port"
              min={0}
              value={connectPort}
              onValueChange={(text) => setConnectPort(text)}
              disabled={loading || Boolean(runtimeUnavailable)}
              title={
                runtimeUnavailable
                  ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                  : undefined
              }
            />
          </label>
          <button
            disabled={!can("connect") || !connectUnit || !connectStream}
            title={
              !can("connect")
                ? unsupported("connect")
                : "Connect selected unit and stream"
            }
            onClick={() =>
              void command({
                kind: "connect",
                unit: connectUnit,
                stream: connectStream,
                role: connectRole,
                port: Math.max(0, Number(connectPort) || 0),
              })
            }
          >
            Connect
          </button>
          <button
            disabled={!can("disconnect")}
            title={
              !can("disconnect")
                ? unsupported("disconnect")
                : "Disconnect objects"
            }
          >
            Disconnect
          </button>
        </aside>
      </div>
      <section className="dwsim-dynamics" aria-label="DWSIM dynamics controls">
        <h2>Dynamics</h2>
        <p>
          Dynamics data is read from the current server projection. Commands use
          its revision and replace this view with persisted readback.
        </p>
        {!projection ? (
          <p role="status">Dynamics unavailable · no process projection loaded.</p>
        ) : dynamicsUnavailableReason ? (
          <p role="status">Dynamics projection unavailable · {dynamicsUnavailableReason}</p>
        ) : (
          <div className="dwsim-dynamics__readback">
            <div><strong>Current schedule</strong><pre>{display(dynamicsProjection.current_schedule)}</pre></div>
            <div><strong>Controllers</strong><pre>{JSON.stringify(Array.isArray(dynamicsProjection.controllers) ? dynamicsProjection.controllers : [], null, 2)}</pre></div>
            <div><strong>Event sets</strong><pre>{JSON.stringify(Array.isArray(dynamicsProjection.event_sets) ? dynamicsProjection.event_sets : [], null, 2)}</pre></div>
            <div><strong>Saved states</strong><pre>{JSON.stringify(projectedSavedStates, null, 2)}</pre></div>
            <div><strong>Last dynamic run</strong><pre>{JSON.stringify(dynamicsProjection.last_dynamic_run ?? null, null, 2)}</pre></div>
          </div>
        )}
        <div className="dwsim-dynamics__controls">
          <fieldset disabled={dynamicDraftDisabled}>
            <legend>Controller</legend>
            <label>Tag<input value={controllerTag} onChange={(e) => setControllerTag(e.target.value)} /></label>
            <label>Set point<NumericInput value={controllerSp} onValueChange={(text) => setControllerSp(text)} /></label>
            <label>Kp<NumericInput value={controllerKp} onValueChange={(text) => setControllerKp(text)} /></label>
            <label>Ki<NumericInput value={controllerKi} onValueChange={(text) => setControllerKi(text)} /></label>
            <label>Kd<NumericInput value={controllerKd} onValueChange={(text) => setControllerKd(text)} /></label>
            <button type="button" disabled={!canDynamic("controller_set") || !controllerTag.trim() || [controllerSp, controllerKp, controllerKi, controllerKd].every((value) => optionalNumber(value) === null)} title={!canDynamic("controller_set") ? dynamicsUnavailableReason ?? unsupported("controller_set") : "Apply controller settings"} onClick={() => void command({ kind: "controller_set", tag: controllerTag.trim(), sp: optionalNumber(controllerSp), kp: optionalNumber(controllerKp), ki: optionalNumber(controllerKi), kd: optionalNumber(controllerKd), out_min: null, out_max: null, reverse_acting: null, active: null, manual_override: null, execution_order: null })}>Apply controller</button>
          </fieldset>
          <fieldset disabled={dynamicDraftDisabled}>
            <legend>Schedule event</legend>
            <label>Event set<input value={eventSet} onChange={(e) => setEventSet(e.target.value)} /></label>
            <label>Event target<input value={eventTag} onChange={(e) => setEventTag(e.target.value)} /></label>
            <label>Property<input value={eventProperty} onChange={(e) => setEventProperty(e.target.value)} /></label>
            <label>Value<NumericInput value={eventValue} onValueChange={(text) => setEventValue(text)} /></label>
            <label>At (s)<NumericInput min={0} value={eventAt} onValueChange={(text) => setEventAt(text)} /></label>
            <label>Description<input value={eventDescription} onChange={(e) => setEventDescription(e.target.value)} /></label>
            <button type="button" disabled={!canDynamic("event_add") || !eventSet.trim() || !eventTag.trim() || !eventProperty.trim() || optionalNumber(eventValue) === null || (optionalNumber(eventAt) ?? -1) < 0} title={!canDynamic("event_add") ? dynamicsUnavailableReason ?? unsupported("event_add") : "Add event to the DWSIM schedule"} onClick={() => void command({ kind: "event_add", event_set: eventSet.trim(), schedule: null, tag: eventTag.trim(), property: eventProperty.trim(), value: optionalNumber(eventValue) ?? 0, units: null, at_s: optionalNumber(eventAt) ?? 0, transition: "step", description: eventDescription.trim() || null })}>Add event</button>
            <label>Remove by description<input aria-label="Event description to remove" value={eventDescription} onChange={(e) => setEventDescription(e.target.value)} /></label>
            <button type="button" disabled={!canDynamic("event_remove") || !eventSet.trim() || !eventDescription.trim()} title={!canDynamic("event_remove") ? dynamicsUnavailableReason ?? unsupported("event_remove") : "Remove matching event"} onClick={() => void command({ kind: "event_remove", event_set: eventSet.trim(), schedule: null, description: eventDescription.trim() })}>Remove event</button>
          </fieldset>
          <fieldset disabled={dynamicDraftDisabled}>
            <legend>Run dynamics</legend>
            <label>Schedule (optional)<input value={runSchedule} onChange={(e) => setRunSchedule(e.target.value)} /></label>
            <label>Duration (s)<NumericInput min={0.001} max={3600} value={runDuration} onValueChange={(text) => setRunDuration(text)} /></label>
            <label>Step (s, optional)<NumericInput min={0.001} value={runStep} onValueChange={(text) => setRunStep(text)} /></label>
            <label>Integration method<select value={runMethod} onChange={(e) => setRunMethod(e.target.value as typeof runMethod)}><option value="">Use DWSIM current setup</option><option>ExplicitEuler</option><option>RungeKutta4</option><option>ImplicitEuler</option><option>AdaptiveRK45</option></select></label>
            <button type="button" disabled={!canDynamic("dynamics_run") || (optionalNumber(runDuration) ?? 0) <= 0 || (optionalNumber(runDuration) ?? 0) > 3600 || (runStep.trim() !== "" && (optionalNumber(runStep) ?? 0) <= 0)} title={!canDynamic("dynamics_run") ? dynamicsUnavailableReason ?? unsupported("dynamics_run") : "Run using DWSIM dynamics"} onClick={() => void command({ kind: "dynamics_run", schedule: runSchedule.trim() || null, duration_s: optionalNumber(runDuration) ?? 60, step_s: optionalNumber(runStep), integrator: null, method: runMethod || null, max_wall_time_s: 120, max_steps: 20000, variables: null })}>Run dynamics</button>
          </fieldset>
          <fieldset disabled={dynamicDraftDisabled}>
            <legend>Stored states</legend>
            <label>New state name<input value={stateName} onChange={(e) => setStateName(e.target.value)} /></label>
            <button type="button" disabled={!canDynamic("state_save") || !stateName.trim()} title={!canDynamic("state_save") ? dynamicsUnavailableReason ?? unsupported("state_save") : "Save state through DWSIM"} onClick={() => void command({ kind: "state_save", name: stateName.trim() })}>Save current state</button>
            <label>Restore state<select value={restoreState} onChange={(e) => setRestoreState(e.target.value)}><option value="">Select a projected saved state</option>{projectedSavedStates.map((name) => <option key={name}>{name}</option>)}</select></label>
            <button type="button" disabled={!canDynamic("state_restore") || !restoreState} title={!canDynamic("state_restore") ? dynamicsUnavailableReason ?? unsupported("state_restore") : "Restore selected projected state"} onClick={() => void command({ kind: "state_restore", name: restoreState })}>Restore state</button>
          </fieldset>
        </div>
      </section>
      <section className="dwsim-revisions">
        <h2>Revision history</h2>
        {[...revisions].reverse().map((revision) => (
          <div key={revision.revision}>
            <span>
              #{revision.seq} · {revision.command_kind} ·{" "}
              {revision.revision.slice(0, 16)}
            </span>
            <a
              href={
                caseId && workspaceId
                  ? dwsimRevisionDownloadUrl(
                      workspaceId,
                      caseId,
                      revision.revision,
                    )
                  : undefined
              }
              aria-disabled={!caseId || !workspaceId}
            >
              Download
            </a>
            <button
              disabled={
                loading ||
                runtimeUnavailable !== null ||
                !projection ||
                revision.revision === projection.revision
              }
              title={
                runtimeUnavailable
                  ? `${runtimeUnavailable.code}: ${runtimeUnavailable.message}`
                  : "Restore this revision with current revision CAS"
              }
              onClick={() => void restoreRevision(revision.revision)}
            >
              Restore
            </button>
          </div>
        ))}
      </section>
    </section>
  );
}

export default ProcessStage;
