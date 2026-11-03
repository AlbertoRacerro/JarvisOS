import { API_BASE_URL } from "./client";
import type { CapabilityRead, EvaluatorRead } from "./generated/engineering";

export type Quantity = { value: number; unit: string };
export type NamedQuantity = { name: string; value: Quantity };
export type StudyRef = {
  authority_owner: string;
  object_type: string;
  object_id: string;
  workspace_id: string;
  revision: string;
};
export type StudyDefinition = {
  study_ref: StudyRef;
  evaluator_id: string;
  subject_ref: StudyRef;
  method: "grid" | "latin_hypercube" | "monte_carlo" | "single_objective_opt";
  variables: Array<{ name: string; domain: { variable: string; lower: Quantity; upper: Quantity }; step?: Quantity }>;
  fixed_inputs: NamedQuantity[];
  objectives: Array<{ output: string; sense: "minimize" | "maximize" }>;
  constraints: Array<{ output: string; operator: "le" | "ge" | "eq"; bound: Quantity }>;
  seed: number;
  budget: number;
  sample_count: number;
  deadline_per_point_s: number;
};
export type StudyRun = {
  study_ref: StudyRef;
  definition_digest: string;
  content_digest: string;
  evaluator_id: string;
  status: string;
  availability_state: string;
  availability_reason: string | null;
  failure: unknown;
  points: Array<{
    index: number;
    inputs: NamedQuantity[];
    request_ref: unknown;
    status: string;
    evaluation: {
      outputs: NamedQuantity[];
      validity?: unknown;
      evidence_refs?: unknown[];
      qualification_status?: unknown;
    } | null;
    failure: unknown;
    feasible: boolean;
    pareto_optimal: boolean;
  }>;
  feasible_count: number;
  failed_count: number;
  infeasible_count: number;
  best_point_index: number | null;
  pareto_point_indices: number[];
  output_summaries: unknown[];
  qualification_status: unknown;
  fidelity: string | null;
};
export type Envelope = {
  envelope_ref: unknown;
  study_ref: StudyRef;
  definition_digest: string;
  content_digest: string;
  selected_point_index: number;
  evaluation_ref: unknown;
  validity_ref: unknown;
  quantities: Array<[string, Quantity]>;
  bounds: unknown[];
  qualification_status: unknown;
  provenance_refs: unknown[];
  envelope_digest: string;
};
export type EscalationRun = {
  study_ref: StudyRef;
  base_run_digest: string;
  content_digest: string;
  points: Array<{
    study_point_index: number;
    original_request_ref: unknown;
    reason: string;
    evaluator_id: string | null;
    requested_fidelity: string | null;
    status: string;
    status_reason: string | null;
    evaluation: unknown;
    discrepancies: unknown[];
  }>;
};

export class EngineeringRequestError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    const code =
      detail && typeof detail === "object" && "code" in detail ? String((detail as { code: unknown }).code) : null;
    super(
      code
        ? `Engineering request failed (${status}): ${code} · ${JSON.stringify(detail)}`
        : `Engineering request failed (${status}): ${JSON.stringify(detail)}`,
    );
  }
}
async function request<T>(workspaceId: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(
    `${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/engineering${path}`,
    init,
  );
  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail ?? null;
    } catch {
      /* response may not be JSON */
    }
    throw new EngineeringRequestError(response.status, detail);
  }
  return response.json() as Promise<T>;
}
const post = (workspaceId: string, path: string, payload?: unknown) =>
  request<unknown>(workspaceId, path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload ?? {}),
  });
const validArray = <T>(value: unknown, label: string): T[] => {
  if (!Array.isArray(value)) throw new Error(`Stale engineering response: ${label} must be a list`);
  return value as T[];
};
const validEvaluators = (value: unknown): EvaluatorRead[] =>
  validArray<unknown>(value, "evaluators").map((item) => {
    if (!item || typeof item !== "object") throw new Error("Stale engineering response: evaluator record is malformed");
    const row = item as Partial<EvaluatorRead>;
    if (typeof row.evaluator_id !== "string" || typeof row.state !== "string")
      throw new Error("Stale engineering response: evaluator identity or state is missing");
    return row as EvaluatorRead;
  });
const validCapabilities = (value: unknown): CapabilityRead[] =>
  validArray<unknown>(value, "capabilities").map((item) => {
    if (!item || typeof item !== "object")
      throw new Error("Stale engineering response: capability record is malformed");
    const row = item as Partial<CapabilityRead>;
    if (typeof row.capability_id !== "string" || typeof row.state !== "string")
      throw new Error("Stale engineering response: capability identity or state is missing");
    return row as CapabilityRead;
  });
const validQuantity = (value: unknown): value is Quantity =>
  Boolean(
    value &&
      typeof value === "object" &&
      typeof (value as Quantity).value === "number" &&
      typeof (value as Quantity).unit === "string",
  );
const validNamedQuantities = (value: unknown): value is NamedQuantity[] =>
  Array.isArray(value) &&
  value.every((item) =>
    Boolean(
      item &&
        typeof item === "object" &&
        typeof (item as NamedQuantity).name === "string" &&
        validQuantity((item as NamedQuantity).value),
    ),
  );
const validRun = (value: unknown): StudyRun => {
  if (!value || typeof value !== "object") throw new Error("Stale engineering response: run is missing");
  const run = value as Partial<StudyRun>;
  if (
    typeof run.content_digest !== "string" ||
    typeof run.definition_digest !== "string" ||
    !Array.isArray(run.points) ||
    typeof run.qualification_status !== "string" ||
    typeof run.status !== "string" ||
    typeof run.availability_state !== "string" ||
    ![run.feasible_count, run.failed_count, run.infeasible_count].every((item) => typeof item === "number") ||
    !Array.isArray(run.pareto_point_indices) ||
    !Array.isArray(run.output_summaries) ||
    (run.best_point_index !== null && typeof run.best_point_index !== "number")
  ) {
    throw new Error("Stale engineering response: required run evidence is missing");
  }
  for (const point of run.points) {
    if (
      !point ||
      typeof point !== "object" ||
      typeof point.index !== "number" ||
      typeof point.status !== "string" ||
      typeof point.feasible !== "boolean" ||
      typeof point.pareto_optimal !== "boolean" ||
      !validNamedQuantities(point.inputs)
    ) {
      throw new Error("Stale engineering response: a study point is malformed");
    }
    if (
      point.evaluation !== null &&
      (!point.evaluation || typeof point.evaluation !== "object" || !validNamedQuantities(point.evaluation.outputs))
    ) {
      throw new Error("Stale engineering response: point evaluation is malformed");
    }
  }
  return run as StudyRun;
};
const validStudyIds = (value: unknown) =>
  validArray<unknown>(value, "studies").map((item) => {
    if (typeof item !== "string") throw new Error("Stale engineering response: study id is malformed");
    return item;
  });
const validEnvelope = (value: unknown): Envelope => {
  if (!value || typeof value !== "object") throw new Error("Stale engineering response: envelope is malformed");
  const envelope = value as Partial<Envelope>;
  if (
    typeof envelope.envelope_digest !== "string" ||
    typeof envelope.content_digest !== "string" ||
    typeof envelope.selected_point_index !== "number" ||
    !Array.isArray(envelope.quantities) ||
    typeof envelope.qualification_status !== "string"
  )
    throw new Error("Stale engineering response: envelope evidence is incomplete");
  return envelope as Envelope;
};
const validEscalation = (value: unknown): EscalationRun => {
  if (!value || typeof value !== "object") throw new Error("Stale engineering response: escalation is malformed");
  const escalation = value as Partial<EscalationRun>;
  if (
    typeof escalation.content_digest !== "string" ||
    typeof escalation.base_run_digest !== "string" ||
    !Array.isArray(escalation.points)
  )
    throw new Error("Stale engineering response: escalation evidence is incomplete");
  for (const point of escalation.points)
    if (!point || typeof point.status !== "string" || typeof point.study_point_index !== "number")
      throw new Error("Stale engineering response: escalation point is malformed");
  return escalation as EscalationRun;
};
export const listEvaluators = async (workspaceId: string) => validEvaluators(await request(workspaceId, "/evaluators"));
export const listCapabilities = async (workspaceId: string) =>
  validCapabilities(await request(workspaceId, "/capabilities"));
export const listStudies = async (workspaceId: string) => validStudyIds(await request(workspaceId, "/studies"));
export const createStudy = async (workspaceId: string, definition: StudyDefinition) =>
  validRun(await post(workspaceId, "/studies", definition));
export const listStudyRuns = async (workspaceId: string, studyId: string) =>
  validArray<unknown>(await request(workspaceId, `/studies/${encodeURIComponent(studyId)}/runs`), "runs").map(validRun);
export const getStudyRun = async (workspaceId: string, studyId: string, digest: string) =>
  validRun(await request(workspaceId, `/studies/${encodeURIComponent(studyId)}/runs/${encodeURIComponent(digest)}`));
export const createEnvelope = async (workspaceId: string, studyId: string, digest: string, pointIndex: number) =>
  validEnvelope(
    await post(
      workspaceId,
      `/studies/${encodeURIComponent(studyId)}/runs/${encodeURIComponent(digest)}/envelope?point_index=${pointIndex}`,
    ),
  );
export const getEnvelope = async (workspaceId: string, studyId: string, digest: string, envelopeDigest: string) =>
  validEnvelope(
    await request(
      workspaceId,
      `/studies/${encodeURIComponent(studyId)}/runs/${encodeURIComponent(digest)}/envelopes/${encodeURIComponent(envelopeDigest)}`,
    ),
  );
export const listEscalations = async (workspaceId: string, studyId: string, digest: string) =>
  validArray<unknown>(
    await request(
      workspaceId,
      `/studies/${encodeURIComponent(studyId)}/runs/${encodeURIComponent(digest)}/escalations`,
    ),
    "escalations",
  ).map(validEscalation);
export const createEscalation = async (
  workspaceId: string,
  studyId: string,
  digest: string,
  evaluatorIds: string[],
  pointIndex: number,
) =>
  validEscalation(
    await post(workspaceId, `/studies/${encodeURIComponent(studyId)}/runs/${encodeURIComponent(digest)}/escalations`, {
      evaluator_ids: evaluatorIds,
      policy: { operator_requested_points: [pointIndex] },
    }),
  );
