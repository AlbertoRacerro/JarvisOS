from pydantic import BaseModel, Field


class ModelDossierVersionIdentity(BaseModel):
    model_spec_id: str
    model_version_id: str
    version_label: str | None = None
    implementation_kind: str | None = None
    status: str | None = None
    created_at: str | None = None
    input_contract_digest: str | None = None


class ModelDossierIndexItem(BaseModel):
    model_spec_id: str
    title: str
    engineering_question: str
    scope: str | None = None
    versions: list[ModelDossierVersionIdentity] = Field(default_factory=list)


class ModelDossierRunSummary(BaseModel):
    run_id: str
    run_label: str | None = None
    status: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    project_knowledge_revision_id: str | None = None


class ModelDossierArtifactRef(BaseModel):
    artifact_id: str
    run_id: str
    role: str | None = None
    digest: str | None = None
    source_ref: str | None = None
    availability: str = "available"


class ModelDossierEvidenceRef(BaseModel):
    evidence_id: str
    kind: str | None = None
    freshness: str | None = None
    source_ref: str | None = None
    availability: str = "available"


class ModelDossierDetail(BaseModel):
    identity: ModelDossierVersionIdentity
    title: str
    engineering_question: str
    scope: str | None = None
    maturity_status: str | None = None
    assumptions_summary: str | None = None
    inputs_summary: str | None = None
    outputs_summary: str | None = None
    runs: list[ModelDossierRunSummary] = Field(default_factory=list)
    artifacts: list[ModelDossierArtifactRef] = Field(default_factory=list)
    evidence: list[ModelDossierEvidenceRef] = Field(default_factory=list)
