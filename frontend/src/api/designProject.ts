import { request, StudioApiError } from "./studio";

export type DesignVersion = {
  id: string; project_id: string; data_workspace_id: string;
  parent_version_id: string | null; label: string; active: boolean;
  starts_empty: boolean; source_mode: "fork" | "empty" | null; created_at: string;
};
export type DesignVersionSet = { project_id: string; active_version_id: string; versions: DesignVersion[] };
export type DesignComponent = {
  id: string; project_id: string; version_id: string; parent_id: string | null;
  name: string; revision: number; created_at: string; updated_at: string;
  source_draft_id: string | null; source_unit_id: string | null;
  geometry_candidate_id: string | null; geometry_source_draft_revision: string | null;
  /** Workspace owning the attached candidate; differs from the version's data workspace after a fork. */
  geometry_workspace_id: string | null;
  models: DesignComponentModel[];
};
export type DesignComponentModel = {
  model_version_id: string; node: string; category: string; source_sha256: string; contract_sha256: string; attached_at: string;
};
/** Studio categories that attach to a component node; process units and procedures attach to Plant. */
export const COMPONENT_NODE_OF_CATEGORY: Record<string, string> = { calculation: "Physics", geometry: "Geometry" };

const base = (projectId: string) => `/workspaces/${encodeURIComponent(projectId)}/design-versions`;
const components = (projectId: string, versionId: string) => `${base(projectId)}/${encodeURIComponent(versionId)}/components`;

export const listDesignVersions = (projectId: string) => request<DesignVersionSet>(base(projectId));
export const createDesignVersion = (projectId: string, label: string, expected: string, mode: "fork" | "empty" = "fork") =>
  request<DesignVersionSet>(base(projectId), "POST", { label, expected_active_version_id: expected, mode });
export const activateDesignVersion = (projectId: string, versionId: string, expected: string) =>
  request<DesignVersionSet>(`${base(projectId)}/activate`, "POST", { version_id: versionId, expected_active_version_id: expected });
export const listDesignComponents = (projectId: string, versionId: string) =>
  request<DesignComponent[]>(components(projectId, versionId));
export const createDesignComponent = (projectId: string, versionId: string, name: string, parentId: string | null) =>
  request<DesignComponent>(components(projectId, versionId), "POST", {
    name, parent_id: parentId, expected_active_version_id: versionId
  });
export const openDesignUnit = (projectId: string, versionId: string, draftId: string, revision: string, unitId: string) =>
  request<DesignComponent>(`${components(projectId, versionId)}/from-unit`, "POST", {
    draft_id: draftId, unit_id: unitId, expected_draft_revision: revision,
    expected_active_version_id: versionId
  });
export const editDesignComponent = (projectId: string, versionId: string, component: DesignComponent,
  name: string, parentId: string | null) =>
  request<DesignComponent>(`${components(projectId, versionId)}/${encodeURIComponent(component.id)}`, "PUT", {
    name, parent_id: parentId, expected_revision: component.revision, expected_active_version_id: versionId
  });
export const attachDesignGeometry = (projectId: string, versionId: string, component: DesignComponent,
  candidateId: string, draftRevision: string | null) =>
  request<DesignComponent>(`${components(projectId, versionId)}/${encodeURIComponent(component.id)}/geometry`, "PUT", {
    candidate_id: candidateId, expected_revision: component.revision, expected_active_version_id: versionId,
    expected_draft_revision: draftRevision
  });
export type ProcessGeometryMapping = {
  supported: boolean; unit_type: string; template: string | null; inner_d_mm: number | null; length_mm: number | null;
  segment_count: number | null; segment_length_mm: number | null; bend_radius_mm: number | null;
  basis: string; draft_revision: string;
};
export const getProcessGeometryMapping = (projectId: string, versionId: string, componentId: string) =>
  request<ProcessGeometryMapping>(`${components(projectId, versionId)}/${encodeURIComponent(componentId)}/geometry/process-mapping`);
export const deepenDesignGeometry = (projectId: string, versionId: string, component: DesignComponent,
  wallMm: number, draftRevision: string) =>
  request<DesignComponent>(`${components(projectId, versionId)}/${encodeURIComponent(component.id)}/geometry/from-process`, "POST", {
    wall_t_mm: wallMm, expected_revision: component.revision, expected_active_version_id: versionId,
    expected_draft_revision: draftRevision
  });
export const attachDesignModel = (projectId: string, versionId: string, component: DesignComponent, modelVersionId: string) =>
  request<DesignComponent>(`${components(projectId, versionId)}/${encodeURIComponent(component.id)}/models`, "PUT", {
    model_version_id: modelVersionId, expected_revision: component.revision, expected_active_version_id: versionId
  });

export type ReducedSegmentStatus = {
  regime: string; reynolds_min: number; reynolds_max: number; diameter_m: number; density_kg_m3: number;
  dynamic_viscosity_pa_s: number; turbulence_model: string | null; wall_treatment: string;
  combined_uncertainty: number; reference: string;
};
export type ReducedModelStatusValue = "current" | "obsolete" | "unknown" | "invalid";
export type ReducedModelStatus = {
  model_version_id: string; reduced_model_id: string | null; reduced_model_version_id: string | null;
  node: string; attached_at: string; fidelity: string; status: ReducedModelStatusValue | string;
  segments: ReducedSegmentStatus[]; geometry_digest: string | null; current_geometry_digest: string | null;
  obsolete_reasons: string[]; process_unit_pinned: boolean;
};
export type ReducedModelOutOfRange = "refuse" | "warn";
export const getComponentReducedModels = (projectId: string, versionId: string, componentId: string) =>
  request<ReducedModelStatus[]>(`${components(projectId, versionId)}/${encodeURIComponent(componentId)}/reduced-models`);
export const pinReducedModelToProcess = (projectId: string, versionId: string, component: DesignComponent,
  modelVersionId: string, draftRevision: string, outOfRange: ReducedModelOutOfRange) =>
  request<ReducedModelStatus>(`${components(projectId, versionId)}/${encodeURIComponent(component.id)}/reduced-models/${encodeURIComponent(modelVersionId)}/process-pin`, "POST", {
    expected_active_version_id: versionId, expected_revision: component.revision,
    expected_draft_revision: draftRevision, out_of_range: outOfRange
  });

export type FluidDomain = {
  domain_version: "cfd_pipe_fluid_domain_v1"; geometry_digest: string; source_part_id: string;
  inner_diameter_m: number; length_m: number; patches: string[];
};
export type PipeStudyContract = {
  contract_version: "cfd_pipe_study_v1"; domain: FluidDomain;
  fluid: { name: string; density_kg_m3: number; dynamic_viscosity_pa_s: number };
  inlet: { mean_velocity_m_s: number };
  outlet_pressure_pa: number; regime: "laminar" | "turbulent_rans";
  turbulence_model: "kOmegaSST" | "kEpsilon" | null;
  wall_treatment: "resolved" | "low_re_resolved" | "wall_function"; mesh_level: number;
};
export type StudyTemplate = { regime: string; template_digest: string; updated_at: string; contract: Record<string, unknown> };
export type CfdToolchainStatus = {
  binary_verified: boolean;
  tools: { tool_id: string; binary_verified: boolean; version_pin: string | null; reason: string | null }[];
};
const componentPath = (projectId: string, versionId: string, componentId: string) =>
  `${components(projectId, versionId)}/${encodeURIComponent(componentId)}`;
export const getComponentCfdToolchain = (projectId: string, versionId: string, componentId: string) =>
  request<CfdToolchainStatus>(`${componentPath(projectId, versionId, componentId)}/cfd-toolchain`);
export const getComponentFluidDomain = (projectId: string, versionId: string, componentId: string) =>
  request<FluidDomain>(`${componentPath(projectId, versionId, componentId)}/fluid-domain`);
export const listComponentStudyTemplates = (projectId: string, versionId: string, componentId: string) =>
  request<StudyTemplate[]>(`${componentPath(projectId, versionId, componentId)}/cfd-study-templates`);
export const setComponentStudyTemplate = (projectId: string, versionId: string, component: DesignComponent, contract: PipeStudyContract) =>
  request<StudyTemplate>(`${componentPath(projectId, versionId, component.id)}/cfd-study-templates/${encodeURIComponent(contract.regime)}`, "PUT", {
    contract, expected_revision: component.revision, expected_active_version_id: versionId
  });

export type CfdRunState = "queued" | "running" | "stopping" | "stopped" | "finished" | "failed" | "interrupted" | string;
export type CfdRun = {
  run_id: string; component_id: string; regime: string; state: CfdRunState; live: boolean; retry_of: string | null; processors?: number;
  queued_at: string; started_at?: string; finished_at?: string; message?: string; qualifies_publication: boolean; authority: string;
  case_status?: string; mesh_status?: string; mesh_reason?: string | null; solver_status?: string | null;
  solver_reason?: string | null; residual_converged?: boolean; iterations?: number; events_truncated?: boolean;
  /** Server-measured result of a finished run; absent on runs that predate measurement. */
  measurement?: CfdRunMeasurement;
};
export type CfdRunMeasurement =
  | { status: "measured"; reason?: null; regime?: string; pressure_gradient_pa_m: number | null; wall_shear_pa?: number | null;
    wall_shear_method?: string; reynolds?: number; mesh_level?: number;
    cells?: number | null; case_manifest_digest?: string | null; tool_versions?: Record<string, string> }
  | { status: "unmeasured"; reason: string | null };
export type CfdRunEvent =
  | { seq: number; kind: "log"; emitted_at: string; stream: string; line: string }
  | { seq: number; kind: "residual"; emitted_at: string; iteration: number; initial: Record<string, number | null>;
    final: Record<string, number | null>; max_normalized_residual: number | null };
export type CfdRunEvents = { run_id: string; state: CfdRunState; live: boolean; events: CfdRunEvent[]; cursor: number };
export type CfdFieldSlice =
  | { status: "available"; run_id?: string; time: string; plane: { patch?: string; normal?: number[]; axis?: string; description?: string };
    association: "point" | "cell"; points: number[][]; polygons: number[][]; point_count: number; cell_count: number; source_files: string[];
    fields: { p: { values: number[]; min: number; max: number; units: string };
      U: { values: number[][]; magnitude_min: number; magnitude_max: number; units: string } } }
  | { status: "unavailable"; run_id?: string; reason: string }
  | { status: "too_large"; run_id?: string; reason: string; time?: string; cells?: number; points?: number };
export const getComponentCfdRunFields = (projectId: string, versionId: string, componentId: string, runId: string) =>
  request<CfdFieldSlice>(`${componentPath(projectId, versionId, componentId)}/cfd-runs/${encodeURIComponent(runId)}/fields`);
export type CfdField3dOperator = { position: [number, number, number]; vector_field: [number, number, number]; scalar: "unchanged" };
export type CfdField3dSurface = {
  name: string; kind: "wall" | "symmetry_plane" | "section"; provenance: "computed";
  points: [number, number, number][]; polygons: number[][];
  fields: { p: { values: number[]; min: number; max: number; units: string };
    U: { values: [number, number, number][]; magnitude_min: number; magnitude_max: number; units: string } };
};
export type CfdField3d =
  | { status: "available"; run_id: string; time: string; surfaces: CfdField3dSurface[];
    symmetry: { domain: "quarter"; axis: "x"; mirror_planes: string[]; basis: string };
    reflection_operators: Record<string, CfdField3dOperator> }
  | { status: "unavailable" | "too_large"; run_id?: string; reason: string };
export const getComponentCfdRunFields3d = (projectId: string, versionId: string, componentId: string, runId: string) =>
  request<CfdField3d>(`${componentPath(projectId, versionId, componentId)}/cfd-runs/${encodeURIComponent(runId)}/fields-3d`);
export const startComponentCfdRun = (projectId: string, versionId: string, componentId: string, regime: string, retryOf?: string, processors = 1) =>
  request<CfdRun>(`${componentPath(projectId, versionId, componentId)}/cfd-runs`, "POST",
    { regime, ...(retryOf ? { retry_of: retryOf } : {}), ...(processors > 1 ? { processors } : {}) });
export const listComponentCfdRuns = (projectId: string, versionId: string, componentId: string) =>
  request<CfdRun[]>(`${componentPath(projectId, versionId, componentId)}/cfd-runs`);
export const getComponentCfdRun = (projectId: string, versionId: string, componentId: string, runId: string) =>
  request<CfdRun>(`${componentPath(projectId, versionId, componentId)}/cfd-runs/${encodeURIComponent(runId)}`);
export const getComponentCfdRunEvents = (projectId: string, versionId: string, componentId: string, runId: string, cursor: number) =>
  request<CfdRunEvents>(`${componentPath(projectId, versionId, componentId)}/cfd-runs/${encodeURIComponent(runId)}/events?cursor=${cursor}`);
export const stopComponentCfdRun = (projectId: string, versionId: string, componentId: string, runId: string) =>
  request<CfdRun>(`${componentPath(projectId, versionId, componentId)}/cfd-runs/${encodeURIComponent(runId)}/stop`, "POST");

export type ReducedModelRegime = "laminar" | "turbulent_rans";
/** Exactly the backend ReducedModelPublication body: run ids only, never measured values (spec 216 contract 7). */
export type ReducedModelPublication = {
  model_id: string; run_ids: string[]; mesh_study_run_ids: Partial<Record<ReducedModelRegime, string[]>>;
  tolerances: { laminar: number; turbulent_rans: number }; expected_revision: number; expected_active_version_id: string;
};
/** Server refusals carry a message plus reasons; both are joined verbatim into the error message. */
export const publishComponentReducedModel = async (projectId: string, versionId: string, componentId: string, body: ReducedModelPublication) => {
  try { return await request<ReducedModelStatus>(`${componentPath(projectId, versionId, componentId)}/reduced-models`, "POST", body); }
  catch (cause) {
    if (cause instanceof StudioApiError && cause.reasons.length) throw new StudioApiError(`${cause.message} ${cause.reasons.join("; ")}`, cause.code, cause.reasons);
    throw cause;
  }
};
