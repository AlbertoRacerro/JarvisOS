import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { Navigate } from "../../app/AppLink";
import {
  createRunnerJob,
  listModelImplementations,
  listParameters,
  previewModelBindings,
  runRunnerJob,
  type BindingPreviewResponse,
  type ModelImplementation,
  type ModelInputVariable,
  type Parameter,
  type RunnerJobRunResponse
} from "../../api/client";
import type { SimulationRunDetail } from "../../api/runs";
import InlineNotice from "../ui/InlineNotice";
import { isStudioKind } from "../../api/studio";
import {
  reconstructPreviousRunBaseline,
  type PreviousRunBaseline,
  type PreviousRunLoadability
} from "./previousRunLoad";

import NumericInput from "../ui/NumericInput";
export type WorkingBinding = Readonly<{ value: string; parameterId: string }>;
type BindingMap = Record<string, WorkingBinding>;
type PreviewPhase = "idle" | "checking" | "ready" | "unavailable" | "local-invalid";
type RunSnapshot = Readonly<{
  requestKey: string;
  revision: number;
  bindings: BindingMap;
  payload: Record<string, unknown>;
}>;

export type EngineeringActionBasis = "working-baseline";
export type EngineeringActionOperation = Readonly<{
  variableName: string;
  label: string;
  unit: string;
  expectedBinding: WorkingBinding;
  proposedBinding: WorkingBinding;
  basis: EngineeringActionBasis;
  basisLabel: string;
  reason: string;
}>;
export type EngineeringWorkingAction = Readonly<{
  id: string;
  workspaceId: string;
  modelVersionId: string;
  contractDigest: string;
  workingRevision: number;
  operations: EngineeringActionOperation[];
}>;
export type EngineeringActionApplyResult = "applied" | "stale" | "invalid";
export type PreviousRunApplyResult =
  | Readonly<{ status: "loaded" }>
  | Readonly<{ status: "pending-model-switch" }>
  | Readonly<{ status: "confirm-required" }>
  | Readonly<{ status: "stale" }>
  | Readonly<{ status: "incompatible"; reason: string }>;

type PendingPreviousRunLoad = Readonly<{
  generation: number;
  expectedRevision: number;
  baseline: PreviousRunBaseline;
}>;

export type EngineeringPropertiesController = Readonly<{
  workspaceId: string | null;
  implementations: ModelImplementation[];
  selected: ModelImplementation | null;
  selectedId: string;
  setSelectedId(value: string): void;
  parameters: Parameter[];
  baseline: BindingMap;
  working: BindingMap;
  previousRunBaseline: PreviousRunBaseline | null;
  previousRunLoadability(run: SimulationRunDetail): PreviousRunLoadability;
  loadPreviousSuccessfulRun(run: SimulationRunDetail, expectedRevision: number, replaceDirty?: boolean): PreviousRunApplyResult;
  preview: BindingPreviewResponse | null;
  previewPhase: PreviewPhase;
  previewMessage: string | null;
  revision: number;
  dirtyNames: ReadonlySet<string>;
  undoAvailable: boolean;
  runLabel: string;
  setRunLabel(value: string): void;
  runBusy: boolean;
  runMessage: string | null;
  runResult: RunnerJobRunResponse | null;
  pendingRun: RunSnapshot | null;
  applyWorkingAction(action: EngineeringWorkingAction): EngineeringActionApplyResult;
  updateValue(variable: ModelInputVariable, value: string): void;
  selectParameter(variable: ModelInputVariable, parameterId: string): void;
  undo(): void;
  revertField(name: string): void;
  revertAll(): void;
  startRun(): void;
  retryRunStart(): void;
  reload(): Promise<void>;
}>;

const EMPTY_BINDING: WorkingBinding = { value: "", parameterId: "" };
const MAX_UNDO = 20;
const CATEGORY_LABELS: Record<ModelInputVariable["category"], string> = {
  design: "Design",
  operating: "Operating",
  property: "Properties",
  model_parameter: "Model parameters",
  equipment: "Equipment"
};

function cloneBindings(bindings: BindingMap): BindingMap {
  return Object.fromEntries(Object.entries(bindings).map(([name, item]) => [name, { ...item }]));
}

function emptyBindings(variables: ModelInputVariable[]): BindingMap {
  return Object.fromEntries(variables.map((variable) => [variable.name, { ...EMPTY_BINDING }]));
}

function sameBinding(left: WorkingBinding | undefined, right: WorkingBinding | undefined): boolean {
  return (left?.value ?? "") === (right?.value ?? "") && (left?.parameterId ?? "") === (right?.parameterId ?? "");
}

function finiteBinding(binding: WorkingBinding | undefined): boolean {
  const value = binding?.value.trim() ?? "";
  return Boolean(value) && Number.isFinite(Number(value));
}

function buildPayload(variables: ModelInputVariable[], bindings: BindingMap, parameters: Parameter[] = [], studio = false): {
  payload: Record<string, Record<string, unknown>>;
  invalidNames: string[];
} {
  const invalidNames: string[] = [];
  const entries = variables.flatMap((variable) => {
    const binding = bindings[variable.name] ?? EMPTY_BINDING;
    const value = binding.value.trim();
    if (!value) return [];
    const number = Number(value);
    if (!Number.isFinite(number)) {
      invalidNames.push(variable.name);
      return [];
    }
    const linked = studio && binding.parameterId ? parameters.find((parameter) => parameter.id === binding.parameterId) : null;
    const item: Record<string, unknown> = { value: number, unit: linked?.unit ?? variable.unit };
    if (binding.parameterId) item.source_parameter_id = binding.parameterId;
    return [[variable.name, item] as const];
  });
  return { payload: Object.fromEntries(entries), invalidNames };
}

function newRequestKey(): string {
  if (!globalThis.crypto?.randomUUID) throw new Error("Secure run request identity is unavailable in this browser.");
  return globalThis.crypto.randomUUID();
}

export function useEngineeringProperties(
  workspaceId: string | null,
  onWorkspaceChange: (workspaceId: string | null) => void
): EngineeringPropertiesController {
  const [implementations, setImplementations] = useState<ModelImplementation[]>([]);
  const [parameters, setParameters] = useState<Parameter[]>([]);
  const [selectedId, setSelectedIdState] = useState("");
  const [baseline, setBaseline] = useState<BindingMap>({});
  const [working, setWorking] = useState<BindingMap>({});
  const [previousRunBaseline, setPreviousRunBaseline] = useState<PreviousRunBaseline | null>(null);
  const [undoStack, setUndoStack] = useState<BindingMap[]>([]);
  const [revision, setRevision] = useState(0);
  const [preview, setPreview] = useState<BindingPreviewResponse | null>(null);
  const [previewPhase, setPreviewPhase] = useState<PreviewPhase>("idle");
  const [previewMessage, setPreviewMessage] = useState<string | null>(null);
  const [runLabel, setRunLabel] = useState("");
  const [runBusy, setRunBusy] = useState(false);
  const [runMessage, setRunMessage] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<RunnerJobRunResponse | null>(null);
  const [pendingRun, setPendingRun] = useState<RunSnapshot | null>(null);

  const loadGeneration = useRef(0);
  const previousRunLoadGeneration = useRef(0);
  const pendingPreviousRunLoadRef = useRef<PendingPreviousRunLoad | null>(null);
  const previewGeneration = useRef(0);
  const revisionRef = useRef(revision);
  const selectedIdRef = useRef(selectedId);
  revisionRef.current = revision;
  selectedIdRef.current = selectedId;

  const eligible = useMemo(
    () => implementations.filter((item) => Boolean(item.input_contract?.variables.length && item.input_contract_sha256)),
    [implementations]
  );
  const selected = useMemo(() => eligible.find((item) => item.id === selectedId) ?? null, [eligible, selectedId]);
  const variables = selected?.input_contract?.variables ?? [];

  useEffect(() => {
    previousRunLoadGeneration.current += 1;
    pendingPreviousRunLoadRef.current = null;
    setPreviousRunBaseline(null);
  }, [workspaceId]);

  const reload = useCallback((): Promise<void> => {
    const generation = ++loadGeneration.current;
    if (!workspaceId) {
      setImplementations([]);
      setParameters([]);
      return Promise.resolve();
    }
    return Promise.all([listModelImplementations(workspaceId), listParameters(workspaceId)])
      .then(([nextImplementations, nextParameters]) => {
        if (generation !== loadGeneration.current) return;
        setImplementations(nextImplementations);
        setParameters(nextParameters);
      })
      .catch((error: Error) => {
        if (generation !== loadGeneration.current) return;
        setPreviewPhase("unavailable");
        setPreviewMessage(error.message);
      });
  }, [workspaceId]);

  useEffect(() => { void reload(); }, [reload]);

  useEffect(() => {
    if (!eligible.some((item) => item.id === selectedId)) setSelectedIdState(eligible[0]?.id ?? "");
  }, [eligible, selectedId]);

  useEffect(() => {
    const pending = pendingPreviousRunLoadRef.current;
    const pendingMatches = Boolean(
      pending &&
      selected &&
      pending.generation === previousRunLoadGeneration.current &&
      pending.baseline.workspaceId === workspaceId &&
      pending.baseline.modelVersionId === selected.id &&
      pending.baseline.contractDigest === selected.input_contract_sha256 &&
      pending.expectedRevision === revisionRef.current
    );
    const next = pendingMatches ? cloneBindings(pending!.baseline.bindings) : emptyBindings(variables);
    setBaseline(next);
    setWorking(next);
    setPreviousRunBaseline(pendingMatches ? pending!.baseline : null);
    setUndoStack([]);
    setPreview(null);
    setPreviewPhase(selected ? "checking" : "idle");
    setPreviewMessage(null);
    setRunResult(null);
    setRunMessage(null);
    setPendingRun(null);
    setRunLabel("");
    setRevision((current) => current + 1);
    previewGeneration.current += 1;
    if (pendingMatches || (pending && selected?.id === pending.baseline.modelVersionId)) pendingPreviousRunLoadRef.current = null;
  }, [selected?.id]);

  useEffect(() => {
    if (!workspaceId || !selected) {
      setPreview(null);
      setPreviewPhase("idle");
      return;
    }
    const built = buildPayload(variables, working, parameters, isStudioKind(selected.implementation_kind));
    if (built.invalidNames.length) {
      previewGeneration.current += 1;
      setPreview(null);
      setPreviewPhase("local-invalid");
      setPreviewMessage("One or more values are not finite numbers.");
      return;
    }

    const generation = ++previewGeneration.current;
    const requestedRevision = revision;
    const requestedModel = selected.id;
    const requestedDigest = selected.input_contract_sha256 ?? "";
    setPreviewPhase("checking");
    setPreviewMessage(null);
    const timer = window.setTimeout(() => {
      previewModelBindings(workspaceId, requestedModel, built.payload)
        .then((response) => {
          if (
            generation !== previewGeneration.current ||
            requestedRevision !== revisionRef.current ||
            requestedModel !== selectedIdRef.current
          ) return;
          if (response.model_version_id !== requestedModel || response.contract_sha256 !== requestedDigest) {
            setPreview(null);
            setPreviewPhase("unavailable");
            setPreviewMessage("The model contract changed during preflight. Refresh the engineering context.");
            return;
          }
          setPreview(response);
          setPreviewPhase("ready");
        })
        .catch((error: Error) => {
          if (generation !== previewGeneration.current || requestedRevision !== revisionRef.current) return;
          setPreview(null);
          setPreviewPhase("unavailable");
          setPreviewMessage(`Preflight unavailable: ${error.message}`);
        });
    }, 120);
    return () => window.clearTimeout(timer);
  }, [workspaceId, selected?.id, selected?.input_contract_sha256, revision]);

  const dirtyNames = useMemo(
    () => new Set(variables.filter((variable) => !sameBinding(working[variable.name], baseline[variable.name])).map((variable) => variable.name)),
    [variables, working, baseline]
  );

  const commitWorking = (next: BindingMap) => {
    setUndoStack((stack) => [...stack.slice(-(MAX_UNDO - 1)), cloneBindings(working)]);
    setWorking(cloneBindings(next));
    setRevision((current) => current + 1);
    setRunResult(null);
  };

  const previousRunLoadability = (run: SimulationRunDetail): PreviousRunLoadability => {
    if (!workspaceId) return { loadable: false, reason: "Run did not succeed" };
    const result = reconstructPreviousRunBaseline(workspaceId, run, eligible);
    if (!result.loadable) return result;
    return result;
  };

  const adoptPreviousRunBaseline = (nextBaseline: PreviousRunBaseline): PreviousRunApplyResult => {
    if (
      !workspaceId ||
      nextBaseline.workspaceId !== workspaceId ||
      selectedIdRef.current !== nextBaseline.modelVersionId ||
      selected?.input_contract_sha256 !== nextBaseline.contractDigest
    ) return { status: "stale" };
    const next = cloneBindings(nextBaseline.bindings);
    setBaseline(next);
    setWorking(next);
    setPreviousRunBaseline(nextBaseline);
    setUndoStack([]);
    setRunResult(null);
    setRunMessage(null);
    setPendingRun(null);
    setRevision((current) => current + 1);
    setPreview(null);
    setPreviewPhase("checking");
    setPreviewMessage(null);
    previewGeneration.current += 1;
    return { status: "loaded" };
  };

  const loadPreviousSuccessfulRun = (
    run: SimulationRunDetail,
    expectedRevision: number,
    replaceDirty = false
  ): PreviousRunApplyResult => {
    if (!workspaceId || expectedRevision !== revisionRef.current) return { status: "stale" };
    const loadability = previousRunLoadability(run);
    if (!loadability.loadable) return { status: "incompatible", reason: loadability.reason };
    if (dirtyNames.size > 0 && !replaceDirty) return { status: "confirm-required" };
    if (expectedRevision !== revisionRef.current) return { status: "stale" };

    const generation = ++previousRunLoadGeneration.current;
    if (loadability.baseline.modelVersionId === selectedIdRef.current) {
      pendingPreviousRunLoadRef.current = null;
      return adoptPreviousRunBaseline(loadability.baseline);
    }

    pendingPreviousRunLoadRef.current = {
      generation,
      expectedRevision,
      baseline: loadability.baseline
    };
    setSelectedIdState(loadability.baseline.modelVersionId);
    return { status: "pending-model-switch" };
  };

  const setSelectedId = (value: string) => {
    previousRunLoadGeneration.current += 1;
    pendingPreviousRunLoadRef.current = null;
    setPreviousRunBaseline(null);
    setSelectedIdState(value);
  };

  const applyWorkingAction = (action: EngineeringWorkingAction): EngineeringActionApplyResult => {
    if (
      !workspaceId ||
      !selected ||
      workspaceId !== action.workspaceId ||
      selected.id !== action.modelVersionId ||
      selected.input_contract_sha256 !== action.contractDigest ||
      revision !== action.workingRevision
    ) return "stale";

    const next = cloneBindings(working);
    for (const operation of action.operations) {
      const variable = variables.find((item) => item.name === operation.variableName);
      if (!variable || variable.unit !== operation.unit) return "invalid";
      if (!sameBinding(working[operation.variableName], operation.expectedBinding)) return "stale";
      if (!finiteBinding(operation.proposedBinding)) return "invalid";

      if (!sameBinding(operation.proposedBinding, baseline[operation.variableName])) return "invalid";
      if (operation.proposedBinding.parameterId) {
        const parameter = parameters.find((item) =>
          item.id === operation.proposedBinding.parameterId &&
          item.unit === variable.unit &&
          item.value != null &&
          Number.isFinite(Number(item.value)) &&
          String(item.value) === operation.proposedBinding.value &&
          item.status !== "superseded"
        );
        if (!parameter) return "invalid";
      }
      next[operation.variableName] = { ...operation.proposedBinding };
    }

    if (!action.operations.length) return "invalid";
    commitWorking(next);
    return "applied";
  };

  const updateValue = (variable: ModelInputVariable, value: string) => {
    const next = { ...working, [variable.name]: { value, parameterId: "" } };
    commitWorking(next);
  };

  const selectParameter = (variable: ModelInputVariable, parameterId: string) => {
    const parameter = parameters.find((item) => item.id === parameterId && (isStudioKind(selected?.implementation_kind) || item.unit === variable.unit));
    const nextBinding = parameter
      ? { parameterId: parameter.id, value: parameter.value ?? "" }
      : { parameterId: "", value: working[variable.name]?.value ?? "" };
    commitWorking({ ...working, [variable.name]: nextBinding });
  };

  const undo = () => {
    const previous = undoStack[undoStack.length - 1];
    if (!previous) return;
    setUndoStack((stack) => stack.slice(0, -1));
    setWorking(cloneBindings(previous));
    setRevision((current) => current + 1);
  };

  const revertField = (name: string) => commitWorking({ ...working, [name]: { ...(baseline[name] ?? EMPTY_BINDING) } });
  const revertAll = () => commitWorking(baseline);

  const executeSnapshot = async (snapshot: RunSnapshot) => {
    setRunBusy(true);
    setRunMessage(null);
    try {
      const created = await createRunnerJob(workspaceId!, snapshot.payload);
      if (created.runner_job.status !== "queued") {
        const reconciled: RunnerJobRunResponse = {
          runner_job: created.runner_job,
          simulation_run: created.simulation_run,
          output: null,
          error: null
        };
        setRunResult(reconciled);

        if (created.runner_job.status === "succeeded") {
          setPendingRun(null);
          if (revisionRef.current === snapshot.revision && selectedIdRef.current === selected?.id) {
            setBaseline(cloneBindings(snapshot.bindings));
            setPreviousRunBaseline(null);
            setUndoStack([]);
          }
          setRunMessage("Run already completed successfully. Canonical project records were not changed.");
          return;
        }

        if (["failed", "cancelled", "timed_out"].includes(created.runner_job.status)) {
          setPendingRun(null);
          setRunMessage(`Execution already finished with status ${created.runner_job.status}. Working edits were preserved.`);
          return;
        }

        setPendingRun(snapshot);
        setRunMessage(`Run is already ${created.runner_job.status}. Retry reconciliation uses the same request identity and will not redispatch execution.`);
        return;
      }

      const result = await runRunnerJob(created.runner_job.id);
      setRunResult(result);
      setPendingRun(null);
      if (result.runner_job.status === "succeeded") {
        if (revisionRef.current === snapshot.revision && selectedIdRef.current === selected?.id) {
          setBaseline(cloneBindings(snapshot.bindings));
          setPreviousRunBaseline(null);
          setUndoStack([]);
        }
        setRunMessage("Run completed. Canonical project records were not changed.");
      } else {
        setRunMessage("Execution finished without success. Working edits were preserved.");
      }
    } catch (error) {
      setPendingRun(snapshot);
      setRunMessage(`Run start/execution outcome is uncertain: ${error instanceof Error ? error.message : String(error)}. Retry uses the same request identity.`);
    } finally {
      setRunBusy(false);
    }
  };

  const startRun = () => {
    if (
      !workspaceId ||
      !selected ||
      runBusy ||
      previewPhase !== "ready" ||
      preview?.state !== "ready" ||
      !preview.normalized_input_set ||
      !runLabel.trim()
    ) return;
    const requestKey = newRequestKey();
    const snapshot: RunSnapshot = {
      requestKey,
      revision,
      bindings: cloneBindings(working),
      payload: {
        request_key: requestKey,
        model_version_id: selected.id,
        run_label: runLabel.trim(),
        input_set: preview.normalized_input_set
      }
    };
    setPendingRun(snapshot);
    void executeSnapshot(snapshot);
  };

  const retryRunStart = () => {
    if (!pendingRun || runBusy) return;
    void executeSnapshot(pendingRun);
  };

  return {
    workspaceId,
    implementations: eligible,
    selected,
    selectedId,
    setSelectedId,
    parameters,
    baseline,
    working,
    previousRunBaseline,
    previousRunLoadability,
    loadPreviousSuccessfulRun,
    preview,
    previewPhase,
    previewMessage,
    revision,
    dirtyNames,
    undoAvailable: undoStack.length > 0,
    runLabel,
    setRunLabel,
    runBusy,
    runMessage,
    runResult,
    pendingRun,
    applyWorkingAction,
    updateValue,
    selectParameter,
    undo,
    revertField,
    revertAll,
    startRun,
    retryRunStart,
    reload
  };
}

function firstBlockingName(controller: EngineeringPropertiesController): string | null {
  const variables = controller.selected?.input_contract?.variables ?? [];
  const localInvalid = variables.find((variable) => {
    const value = controller.working[variable.name]?.value.trim() ?? "";
    return Boolean(value) && !Number.isFinite(Number(value));
  });
  if (localInvalid) return localInvalid.name;
  const previewBlocker = controller.preview?.variables.find(
    (variable) => variable.binding_state === "invalid" || (variable.required && variable.binding_state === "missing")
  );
  return previewBlocker?.name ?? null;
}

function blockerCount(controller: EngineeringPropertiesController): number {
  const variables = controller.selected?.input_contract?.variables ?? [];
  const localInvalid = variables.filter((variable) => {
    const value = controller.working[variable.name]?.value.trim() ?? "";
    return Boolean(value) && !Number.isFinite(Number(value));
  }).length;
  if (localInvalid) return localInvalid;
  if (!controller.preview) return 0;
  return controller.preview.variables.filter(
    (variable) => variable.binding_state === "invalid" || (variable.required && variable.binding_state === "missing")
  ).length + controller.preview.errors.length;
}

export function EngineeringPropertiesPanel({
  controller,
  stageContext,
  navigate
}: {
  controller: EngineeringPropertiesController;
  stageContext?: ReactNode;
  navigate?: Navigate;
}) {
  const selected = controller.selected;
  const variables = selected?.input_contract?.variables ?? [];
  const blockers = blockerCount(controller);
  const groups = useMemo(() => {
    const grouped = new Map<string, ModelInputVariable[]>();
    variables.forEach((variable) => {
      const group = CATEGORY_LABELS[variable.category];
      grouped.set(group, [...(grouped.get(group) ?? []), variable]);
    });
    return [...grouped.entries()];
  }, [variables]);

  const goToFirstIssue = () => {
    const name = firstBlockingName(controller);
    if (!name) return;
    document.getElementById(`engineering-property-${name}`)?.focus();
  };

  const renderVariable = (variable: ModelInputVariable) => {
    const binding = controller.working[variable.name] ?? EMPTY_BINDING;
    const variablePreview = controller.preview?.variables.find((item) => item.name === variable.name);
    const linkedParameter = binding.parameterId ? controller.parameters.find((item) => item.id === binding.parameterId) : undefined;
    const compatible = controller.parameters.filter(
      (parameter) =>
        (isStudioKind(selected?.implementation_kind) || parameter.unit === variable.unit) &&
        parameter.value != null &&
        Number.isFinite(Number(parameter.value)) &&
        (parameter.status !== "superseded" || parameter.id === binding.parameterId)
    );
    const isDirty = controller.dirtyNames.has(variable.name);
    const isRequiredMissing = Boolean(variable.required && variablePreview?.binding_state === "missing");
    const isInvalid = variablePreview?.binding_state === "invalid" || (binding.value.trim() !== "" && !Number.isFinite(Number(binding.value)));
    const fromPreviousRun = Boolean(
      controller.previousRunBaseline &&
      !isDirty &&
      sameBinding(binding, controller.baseline[variable.name])
    );
    const source = binding.parameterId
      ? `Linked parameter · ${linkedParameter?.name ?? "linked source"}`
      : binding.value.trim() ? (fromPreviousRun ? "Previous successful run" : "Working override") : "Empty";
    return (
      <div className="scenario-variable" key={variable.name}>
        <div>
          <strong>{variable.label}{isDirty ? " · Modified" : ""}</strong>
          <span>{source}</span>
          <small>{variable.description}</small>
        </div>
        <label>
          Value [{variable.unit}]
          <NumericInput
            id={`engineering-property-${variable.name}`}
            value={binding.value}
            placeholder="Empty"
            aria-invalid={isInvalid || isRequiredMissing || undefined}
            onValueChange={(text) => controller.updateValue(variable, text)}
          />
        </label>
        <label>
          Source
          <select value={binding.parameterId} onChange={(event) => controller.selectParameter(variable, event.target.value)}>
            <option value="">Working override</option>
            {compatible.map((parameter) => (
              <option key={parameter.id} value={parameter.id}>
                {parameter.name}{parameter.status === "superseded" ? " (superseded)" : ""}: {parameter.value} {parameter.unit}
              </option>
            ))}
          </select>
        </label>
        <div className={`binding-state ${isInvalid ? "invalid" : isRequiredMissing ? "missing" : variablePreview?.binding_state ?? "missing"}`}>
          {isRequiredMissing ? <span>Required value is empty.</span> : null}
          {!variable.required && !binding.value.trim() ? <span>Optional · Empty</span> : null}
          {isInvalid && !variablePreview?.errors.length ? <span>Enter a finite number.</span> : null}
          {variablePreview?.errors.map((error) => <span key={error}>{error}</span>)}
        </div>
        {binding.parameterId ? (
          <details className="shell-properties__inspect">
            <summary>Inspect linked source</summary>
            <dl className="details">
              <div><dt>Parameter</dt><dd>{linkedParameter?.name ?? "Unavailable source record"}</dd></div>
              <div><dt>Status</dt><dd>{linkedParameter?.status ?? "unavailable"}</dd></div>
            </dl>
            {navigate ? (
              <button
                type="button"
                className="secondary-button"
                onClick={() => navigate(`/engineering-data?kind=parameter&id=${encodeURIComponent(binding.parameterId)}`)}
              >
                Open source
              </button>
            ) : null}
          </details>
        ) : null}
        {isDirty ? <button type="button" className="secondary-button" onClick={() => controller.revertField(variable.name)}>Revert field</button> : null}
      </div>
    );
  };

  if (!controller.workspaceId) {
    return <InlineNotice tone="neutral">No workspace is available for engineering Properties.</InlineNotice>;
  }

  const baselineLabel = controller.previousRunBaseline
    ? `${controller.previousRunBaseline.runLabel} · Previous successful run`
    : "current bindings";

  return (
    <div className="engineering-properties">
      {stageContext ? <section className="shell-properties__inspect" aria-label="Stage inspector">{stageContext}</section> : null}
      {controller.implementations.length === 0 ? (
        <InlineNotice tone="neutral">No eligible model input contract is registered in this workspace.</InlineNotice>
      ) : (
        <>
          <label className="scenario-model-select">
            Model contract
            <select value={controller.selectedId} onChange={(event) => controller.setSelectedId(event.target.value)}>
              {controller.implementations.map((item) => <option key={item.id} value={item.id}>{item.version_label}</option>)}
            </select>
          </label>

          <div className="dof-strip" aria-live="polite">
            {controller.previewPhase === "checking" ? <strong>Checking…</strong> : null}
            {controller.previewPhase === "ready" && controller.preview?.state === "ready" ? <strong>Ready</strong> : null}
            {blockers > 0 ? <strong>{blockers} blocker{blockers === 1 ? "" : "s"}</strong> : null}
            {controller.previewPhase === "unavailable" ? <strong>Preflight unavailable</strong> : null}
            {controller.dirtyNames.size > 0 ? <span>{controller.dirtyNames.size} unsaved change{controller.dirtyNames.size === 1 ? "" : "s"} · Baseline: {baselineLabel}</span> : <span>Baseline: {baselineLabel}</span>}
          </div>
          {controller.previewMessage ? <div className="error-banner">{controller.previewMessage}</div> : null}
          {blockers > 0 ? <button type="button" className="secondary-button" onClick={goToFirstIssue}>Go to first issue</button> : null}

          {groups.length > 0 ? (
            <div className="scenario-variable-list" aria-label="Model properties">
              {groups.map(([group, rows]) => (
                <section key={group} aria-labelledby={`engineering-properties-${group.replace(/\s+/g, "-").toLowerCase()}`}>
                  <h4 id={`engineering-properties-${group.replace(/\s+/g, "-").toLowerCase()}`}>{group}</h4>
                  {rows.map(renderVariable)}
                </section>
              ))}
            </div>
          ) : null}

          <div className="scenario-actions">
            <button type="button" className="secondary-button" disabled={!controller.undoAvailable} onClick={controller.undo}>Undo</button>
            <button type="button" className="secondary-button" disabled={controller.dirtyNames.size === 0} onClick={controller.revertAll}>Revert all</button>
            <label>
              Run label
              <input value={controller.runLabel} onChange={(event) => controller.setRunLabel(event.target.value)} />
            </label>
            <button
              type="button"
              disabled={controller.runBusy || controller.previewPhase !== "ready" || controller.preview?.state !== "ready" || !controller.runLabel.trim()}
              onClick={controller.startRun}
            >
              Run
            </button>
          </div>
          {controller.pendingRun && controller.runMessage ? <button type="button" className="secondary-button" disabled={controller.runBusy} onClick={controller.retryRunStart}>Retry same run request</button> : null}
          {controller.runMessage ? <p>{controller.runMessage}</p> : null}
          {controller.runResult ? <p>Execution status: <strong>{controller.runResult.runner_job.status}</strong></p> : null}
        </>
      )}

      {controller.previousRunBaseline ? (
        <details className="shell-properties__inspect">
          <summary>Previous run baseline</summary>
          <dl className="details">
            <div><dt>Run</dt><dd>{controller.previousRunBaseline.runLabel}</dd></div>
            <div><dt>Run id</dt><dd>{controller.previousRunBaseline.runId}</dd></div>
          </dl>
        </details>
      ) : null}
      {selected ? (
        <details className="shell-properties__inspect">
          <summary>Technical details</summary>
          <dl className="details">
            <div><dt>Model version</dt><dd>{selected.id}</dd></div>
            <div><dt>Contract digest</dt><dd>{selected.input_contract_sha256}</dd></div>
            <div><dt>Working revision</dt><dd>{controller.revision}</dd></div>
          </dl>
        </details>
      ) : null}
    </div>
  );
}
