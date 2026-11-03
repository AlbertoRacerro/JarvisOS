import { API_BASE_URL } from "./client";

export type ModelDossierVersionIdentity = {
  model_spec_id: string;
  model_version_id: string;
  version_label?: string | null;
  implementation_kind?: string | null;
  status?: string | null;
  created_at?: string | null;
  input_contract_digest?: string | null;
};

export type ModelDossierIndexItem = {
  model_spec_id: string;
  title: string;
  engineering_question: string;
  scope?: string | null;
  versions: ModelDossierVersionIdentity[];
};

export type ModelDossierRunSummary = {
  run_id: string;
  run_label?: string | null;
  status: string;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  project_knowledge_revision_id?: string | null;
};

export type ModelDossierArtifactRef = {
  artifact_id: string;
  run_id: string;
  role?: string | null;
  digest?: string | null;
  source_ref?: string | null;
  availability: string;
};

export type ModelDossierEvidenceRef = {
  evidence_id: string;
  kind?: string | null;
  freshness?: string | null;
  source_ref?: string | null;
  availability: string;
};

export type ModelDossierDetail = {
  identity: ModelDossierVersionIdentity;
  title: string;
  engineering_question: string;
  scope?: string | null;
  maturity_status?: string | null;
  assumptions_summary?: string | null;
  inputs_summary?: string | null;
  outputs_summary?: string | null;
  runs: ModelDossierRunSummary[];
  artifacts: ModelDossierArtifactRef[];
  evidence: ModelDossierEvidenceRef[];
};

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) throw new Error(`Request failed with ${response.status}`);
  return response.json() as Promise<T>;
}

export function listModelDossiers(workspaceId: string): Promise<ModelDossierIndexItem[]> {
  return getJson<ModelDossierIndexItem[]>(`/workspaces/${encodeURIComponent(workspaceId)}/model-dossiers`);
}

export function getModelDossier(workspaceId: string, modelVersionId: string): Promise<ModelDossierDetail> {
  return getJson<ModelDossierDetail>(`/workspaces/${encodeURIComponent(workspaceId)}/model-dossiers/${encodeURIComponent(modelVersionId)}`);
}
