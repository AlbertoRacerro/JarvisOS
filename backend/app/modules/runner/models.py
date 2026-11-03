from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RunStatus = Literal["draft", "queued", "running", "succeeded", "failed", "cancelled", "timed_out"]
BindingState = Literal["missing", "manual", "parameter", "invalid"]
PreviewState = Literal["incomplete", "ready", "invalid"]


class ModelImplementationCreate(BaseModel):
    model_spec_id: str = Field(min_length=1)
    version_label: str = "batch-growth-v0"
    implementation_kind: str = "batch_growth_v0"
    notes: str | None = None
    script_text: str | None = None
    input_contract: dict[str, Any] | None = None


class ModelImplementationRead(BaseModel):
    id: str
    workspace_id: str
    model_spec_id: str
    version_label: str
    implementation_artifact_id: str
    status: str
    implementation_kind: str
    script_sha256: str
    script_path: str
    created_at: str
    notes: str | None = None
    input_contract: dict[str, Any] | None = None
    input_contract_sha256: str | None = None


class BindingPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bindings: dict[str, Any] = Field(default_factory=dict)


class BindingVariablePreview(BaseModel):
    name: str
    label: str
    unit: str
    category: str
    description: str
    required: bool
    binding_state: BindingState
    value: float | None = None
    source_parameter_id: str | None = None
    errors: list[str] = Field(default_factory=list)


class BindingPreviewResponse(BaseModel):
    model_version_id: str
    contract_sha256: str
    evaluation_mode: Literal["forward"]
    structural_input_dof: int
    bound_input_dof: int
    unresolved_input_dof: int
    invalid_binding_count: int
    state: PreviewState
    variables: list[BindingVariablePreview]
    errors: list[str] = Field(default_factory=list)
    normalized_input_set: dict[str, dict[str, object]] | None = None


class RunnerJobCreate(BaseModel):
    model_version_id: str = Field(min_length=1)
    run_label: str | None = None
    input_set: dict[str, Any]
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    request_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class RunnerJobRead(BaseModel):
    id: str
    workspace_id: str
    simulation_run_id: str
    runner_type: str
    status: RunStatus
    script_path: str
    script_sha256: str
    command_metadata: dict[str, Any] | None = None
    environment_metadata: dict[str, Any] | None = None
    working_dir: str
    input_file: str | None = None
    output_dir: str
    timeout_seconds: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_output_json_bytes: int
    max_artifact_bytes: int
    created_at: str
    updated_at: str


class SimulationRunDetail(BaseModel):
    id: str
    workspace_id: str
    model_version_id: str | None = None
    run_label: str | None = None
    status: str
    input_payload: str | None = None
    parameter_payload: str | None = None
    output_payload: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str
    notes: str | None = None
    stale: bool = False
    superseded: bool = False


class RunnerJobCreateResponse(BaseModel):
    runner_job: RunnerJobRead
    simulation_run: SimulationRunDetail


class RunnerJobRunResponse(BaseModel):
    runner_job: RunnerJobRead
    simulation_run: SimulationRunDetail
    output: dict[str, Any] | None = None
    error: dict[str, str] | None = None


class RunLogRead(BaseModel):
    id: str
    workspace_id: str
    simulation_run_id: str
    stream: str
    content: str
    truncated: bool
    created_at: str


class RunArtifactRead(BaseModel):
    artifact_id: str
    workspace_id: str
    simulation_run_id: str
    role: str
    artifact_type: str
    filename: str
    relative_path: str | None = None
    stored_path: str | None = None
    size_bytes: int | None = None
    created_at: str
    source_ref: str | None = None
    source_module: str | None = None
    mime_type: str | None = None
    sha256: str | None = None
    status: str
    under_data_root: bool
