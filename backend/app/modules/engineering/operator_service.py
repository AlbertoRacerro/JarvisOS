"""Minimal HTTP projections over existing engineering owners."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.core.paths import build_paths
from app.modules.ai.context_builder import canonical_digest
from app.modules.bluecad.cfd_adapter import OpenFoamEvaluator
from app.modules.engineering.evaluator_contracts import (
    BackendKind,
    EngineeringEvaluator,
    EvaluatorAvailability,
    EvaluatorDescriptor,
)
from app.modules.engineering.evidence_contracts import FidelityTier
from app.modules.engineering.operator_models import CapabilityRead, EvaluatorRead
from app.modules.photobioreactor.pbr_evaluator import PbrDayNightEvaluator
from app.modules.process_stack.correlations import InternalConvectionEvaluator, PipePressureDropEvaluator
from app.modules.process_stack.dwsim import DwsimEvaluator
from app.modules.process_stack.properties import CoolPropPropertyEvaluator
from app.modules.workspaces.service import get_workspace

SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_STUDY_BUDGET = 16


def evaluator_registry() -> dict[str, EngineeringEvaluator]:
    factories: tuple[tuple[str, Callable[[], EngineeringEvaluator], BackendKind, str, FidelityTier], ...] = (
        ("fluids.pipe_pressure_drop", PipePressureDropEvaluator, "specialist", "fluids", "screening"),
        ("ht.internal_convection", InternalConvectionEvaluator, "specialist", "ht", "screening"),
        ("coolprop.pure_fluid_properties", CoolPropPropertyEvaluator, "property_package", "CoolProp", "screening"),
        ("dwsim.flowsheet", DwsimEvaluator, "dynamic_simulator", "DWSIM", "dynamic_detailed"),
        ("openfoam_channel_v1", OpenFoamEvaluator, "cfd_solver", "OpenFOAM", "field_resolved"),
        ("photobioreactor.day_night", PbrDayNightEvaluator, "dynamic_simulator", "scikit-sundae", "reduced_order"),
    )
    registry: dict[str, EngineeringEvaluator] = {}
    for evaluator_id, factory, kind, name, fidelity in factories:
        try:
            evaluator = factory()
            evaluator.descriptor()
        except Exception as exc:
            registry[evaluator_id] = UnavailableEvaluator(evaluator_id, kind, name, fidelity, type(exc).__name__)
        else:
            registry[evaluator_id] = evaluator
    return registry


class UnavailableEvaluator:
    def __init__(self, evaluator_id: str, kind: BackendKind, name: str, fidelity: FidelityTier, cause: str) -> None:
        self.evaluator_id = evaluator_id
        self.kind = kind
        self.name = name
        self.fidelity = fidelity
        self.cause = cause

    def descriptor(self) -> EvaluatorDescriptor:
        return EvaluatorDescriptor(evaluator_id=self.evaluator_id, backend_kind=self.kind, backend_name=self.name,
                                   backend_version="unavailable", fidelity=self.fidelity)

    def availability(self) -> EvaluatorAvailability:
        return EvaluatorAvailability(evaluator_id=self.evaluator_id, state="unknown",
                                     checked_at=datetime.now(UTC),
                                     reason_code=f"EVALUATOR_CONSTRUCTION_FAILED_{self.cause.upper()}")

    def evaluate(self, request: Any) -> Any:
        raise RuntimeError("evaluator is unavailable")


def evaluator_reads(registry: dict[str, EngineeringEvaluator]) -> list[EvaluatorRead]:
    rows = []
    for evaluator_id, evaluator in registry.items():
        descriptor = None
        availability = None
        reason = None
        try:
            descriptor = evaluator.descriptor()
            availability = evaluator.availability()
            state = availability.state
            reason = availability.reason_code
        except Exception as exc:
            state = "unknown"
            reason = f"STATUS_ERROR_{type(exc).__name__.upper()}"
        rows.append(EvaluatorRead(
            evaluator_id=evaluator_id,
            backend_kind=descriptor.backend_kind if descriptor else None,
            backend_name=descriptor.backend_name if descriptor else None,
            backend_version=(availability.backend_version if availability and availability.backend_version
                             else descriptor.backend_version if descriptor else None),
            fidelity=descriptor.fidelity if descriptor else None,
            state=state,
            reason_code=reason,
        ))
    return rows


def capability_reads(registry: dict[str, EngineeringEvaluator], app_state: Any) -> list[CapabilityRead]:
    evaluators = {row.evaluator_id: row for row in evaluator_reads(registry)}
    rows: list[CapabilityRead] = []
    hermes = app_state.hermes_supervisor.status() if getattr(app_state, "hermes_supervisor", None) else {}
    hermes_running = hermes.get("state") == "running" and hermes.get("worker_pid") is not None
    rows.append(CapabilityRead(capability_id="hermes", state="available" if hermes_running else "unavailable",
                               reason_code=None if hermes_running else "HERMES_WORKER_STOPPED"))
    try:
        from app.modules.local_ai.runtime.status import get_local_ai_runtime_status

        runtime = get_local_ai_runtime_status()
        configured = bool(runtime.get("configured_route_models"))
        available = bool(runtime.get("ollama_reachable")) and not runtime.get("missing_models")
        rows.append(CapabilityRead(
            capability_id="local_decision_routing",
            state="available" if available else "unavailable" if configured else "not_configured",
            reason_code=None if available else "LOCAL_MODEL_RUNTIME_UNAVAILABLE" if configured else "LOCAL_MODEL_ROUTES_NOT_CONFIGURED",
        ))
    except Exception:
        rows.append(CapabilityRead(capability_id="local_decision_routing", state="unavailable",
                                   reason_code="LOCAL_RUNTIME_STATUS_ERROR"))
    retrieval = _retrieval_status()
    rows.append(CapabilityRead(capability_id="retrieval", state=retrieval[0], reason_code=retrieval[1]))
    for capability_id, evaluator_id in (
        ("dwsim", "dwsim.flowsheet"), ("openfoam", "openfoam_channel_v1"),
        ("pbr", "photobioreactor.day_night"),
    ):
        result = evaluators.get(evaluator_id)
        rows.append(CapabilityRead(
            capability_id=capability_id,
            state="available" if result and result.state == "available" else "unavailable" if result else "not_configured",
            reason_code=None if result and result.state == "available" else result.reason_code if result else "EVALUATOR_NOT_REGISTERED",
        ))
    return rows


def _retrieval_status() -> tuple[Literal["available", "unavailable", "not_configured"], str | None]:
    path = build_paths().retrieval_index_file.resolve()
    if not path.is_file():
        return "not_configured", "RETRIEVAL_INDEX_NOT_BUILT"
    return "available", None


def workspace_dir(workspace_id: str) -> Path:
    if not SAFE_ID.fullmatch(workspace_id) or get_workspace(workspace_id) is None:
        raise LookupError("workspace_not_found")
    root = build_paths().workspaces_dir.resolve()
    target = (root / workspace_id / "engineering" / "studies").resolve()
    if not target.is_relative_to(root):
        raise ValueError("workspace_path_invalid")
    return target


def study_dir(workspace_id: str, study_id: str) -> Path:
    if not SAFE_ID.fullmatch(study_id) or study_id in {".", ".."}:
        raise ValueError("study_id_invalid")
    root = workspace_dir(workspace_id).resolve()
    target = (root / study_id).resolve()
    if not target.is_relative_to(root):
        raise ValueError("study_id_invalid")
    return target


def digest_file_name(digest: str) -> str:
    if not DIGEST.fullmatch(digest):
        raise ValueError("content_digest_invalid")
    return f"{digest.replace(':', '-')}.json"


def _data_path(path: Path) -> Path:
    root = build_paths().data_root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("engineering_record_path_outside_data_root")
    return resolved


class RecordConflictError(Exception):
    """An immutable record already exists with different content."""


def write_once(path: Path, model: Any) -> None:
    _data_path(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = _data_path(path)
    payload = json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    except FileExistsError:
        existing = json.loads(target.read_text(encoding="utf-8"))
        current = model.model_dump(mode="json")
        identity = current.get("content_digest") or current.get("envelope_digest")
        if identity and target.name == digest_file_name(identity) and existing.get(
            "content_digest", existing.get("envelope_digest")
        ) == identity:
            return
        if canonical_digest(existing) != canonical_digest(current):
            raise RecordConflictError(target.name) from None


def read_record(path: Path, model_type: Any) -> Any:
    target = _data_path(path)
    return model_type.model_validate_json(target.read_text(encoding="utf-8")) if target.is_file() else None


def record_paths(directory: Path, suffix: str) -> list[Path]:
    target = _data_path(directory)
    return sorted(target.glob(f"*{suffix}.json")) if target.is_dir() else []
