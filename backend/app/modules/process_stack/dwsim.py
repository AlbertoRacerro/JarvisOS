"""Thin, hash-pinned DWSIM evaluator; DWSIM owns the flowsheet and its solver."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree

from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.engineering.evaluator_contracts import (
    AvailabilityState,
    EvaluationFailure,
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    EvaluatorAvailability,
    EvaluatorDescriptor,
    FailureCategory,
    NamedQuantity,
    NumericalDiagnostics,
)
from app.modules.engineering.evidence_contracts import validity_content_digest
from app.modules.engineering.refs import EvaluationResultRef, Quantity, ValidityEnvelopeRef
from app.modules.process_stack._common import EvaluationRefusal, magnitude
from app.modules.process_stack.dwsim_mcp import (
    DwsimExecutableMismatch,
    DwsimMcpClient,
    DwsimMcpError,
)

EVALUATOR_ID = "dwsim.flowsheet"
_MASS_RESIDUAL_TOLERANCE_KG_S = 1e-6
_READBACK_REL_TOLERANCE = 1e-8
_READBACK_ABS_TOLERANCE = 1e-9
_STREAM_INPUTS = {
    "mass_flow": ("mass_flow_kg_s", "kg/s"),
    "temperature": ("temperature_K", "K"),
    "pressure": ("pressure_Pa", "Pa"),
}


class DwsimEvaluationError(RuntimeError):
    """An evaluation failure with a stable shared category and DWSIM-native code."""

    def __init__(self, code: str, message: str, category: FailureCategory = "did_not_converge") -> None:
        super().__init__(message)
        self.code = code
        self.category = category


def _configured_runtime() -> tuple[Path | None, str, str | None]:
    executable = os.environ.get("JARVISOS_DWSIM_MCP_PATH")
    expected = os.environ.get("JARVISOS_DWSIM_MCP_SHA256", "").lower()
    if not executable:
        return None, expected, None
    path = Path(executable).expanduser()
    if not path.is_file():
        return path, expected, "DWSIM_MCP_EXECUTABLE_UNAVAILABLE"
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return path, expected, "DWSIM_MCP_DIGEST_UNCONFIGURED"
    digest = _sha256(path)
    if digest != expected:
        return path, expected, "DWSIM_MCP_DIGEST_MISMATCH"
    return path, expected, None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _version(path: Path) -> str:
    deps = path.with_name(f"{path.name}.deps.json")
    try:
        data = json.loads(deps.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    libraries = data.get("libraries", {})
    versions = {
        match.group(1)
        for name in libraries
        if name.startswith("DWSIM.") and (match := re.search(r"/(\d+\.\d+\.\d+)$", name))
    }
    return next(iter(versions)) if len(versions) == 1 else "unknown"


def _client() -> tuple[DwsimMcpClient, Path, str]:
    path, expected, reason = _configured_runtime()
    if reason or path is None:
        raise DwsimEvaluationError(reason or "DWSIM_MCP_UNAVAILABLE", "DWSIM runtime is unavailable", "not_available")
    try:
        client = DwsimMcpClient(path, expected)
    except ValueError as exc:
        raise DwsimEvaluationError("DWSIM_MCP_CONFIG_INVALID", "DWSIM runtime configuration is invalid", "not_available") from exc
    return client, path, _version(path)


def _connector_records(case_path: Path) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, dict[str, list[dict[str, Any]]]], bool]:
    """Read native connector attachments, keyed by native object ID and stream tag."""
    try:
        with zipfile.ZipFile(case_path) as archive:
            xml_name = next(name for name in archive.namelist() if name.lower().endswith(".xml"))
            root = ElementTree.fromstring(archive.read(xml_name))
    except (OSError, zipfile.BadZipFile, StopIteration, ElementTree.ParseError):
        try:
            root = ElementTree.parse(case_path).getroot()
        except (OSError, ElementTree.ParseError):
            return {}, {}, False
    by_id: dict[str, dict[str, list[dict[str, Any]]]] = {}
    by_tag: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for node in root.findall(".//GraphicObject"):
        native_id = node.findtext("Name")
        tag = node.findtext("Tag")
        if not native_id:
            continue
        connectors: dict[str, list[dict[str, Any]]] = {}
        for direction in ("Input", "Output"):
            records = []
            for index, connector in enumerate(node.findall(f"./{direction}Connectors/Connector")):
                attached = connector.get("IsAttached", "false").lower() == "true"
                records.append({
                    "port_index": index, "attached": attached,
                    "native_object_id": connector.get("AttachedToObjID") if attached else None,
                    "native_connector_index": int(connector.get("AttachedToConnIndex", "0")) if attached else None,
                    "connection_type": connector.get("ConnType"),
                    "energy": connector.get("AttachedToEnergyConn", "False").lower() == "true",
                })
            connectors[direction.lower()] = records
        energy_records = []
        for index, connector in enumerate(node.findall("./EnergyConnector/Connector")):
            attached = connector.get("IsAttached", "false").lower() == "true"
            energy_records.append({
                "port_index": index, "attached": attached,
                "native_object_id": connector.get("AttachedToObjID") if attached else None,
                "native_connector_index": int(connector.get("AttachedToConnIndex", "0")) if attached else None,
                "connection_type": connector.get("ConnType"), "energy": True,
            })
        connectors["energy"] = energy_records
        by_id[native_id] = connectors
        if tag:
            by_tag[tag] = connectors
    return by_id, by_tag, bool(by_id)


def project_flowsheet(client: DwsimMcpClient, flowsheet_id: str, case_path: Path) -> dict[str, Any]:
    """Read-only 149 projection: native identity/geometry/status and saved connector graph."""
    result = client.call("dwsim_flowsheet_list_objects", {"flowsheet_id": flowsheet_id}, 30)
    objects = result.get("objects", [])
    by_id, _, graph_available = _connector_records(case_path)
    projection = []
    for item in objects:
        projection.append({
            "native_id": item.get("id"),
            "tag": item.get("name"),
            "type": item.get("type"),
            "position": {"x": item.get("x"), "y": item.get("y")},
            "calculated": item.get("calculated"),
            "errors": item.get("error", ""),
            "connectors": by_id.get(item.get("id")),
        })
    return {
        "objects": projection,
        "connector_graph_available": graph_available,
        "unavailable": [] if graph_available else ["saved XML connector records unavailable"],
        "native_ids_are_case_local": True,
    }


def _apply_writes(client: DwsimMcpClient, flowsheet_id: str, request: EvaluationRequest) -> list[dict[str, Any]]:
    writes = []
    for item in request.inputs:
        match = re.fullmatch(r"stream\.(.+)\.(mass_flow|temperature|pressure)", item.name)
        if not match:
            raise EvaluationRefusal("unsupported_request", "stream_input_unsupported", f"unsupported DWSIM input {item.name}")
        tag, variable = match.groups()
        property_name, target_unit = _STREAM_INPUTS[variable]
        target = magnitude(item.value, target_unit)
        client.call("dwsim_stream_set_conditions", {
            "flowsheet_id": flowsheet_id, "name": tag, property_name: target,
        }, 30)
        readback = client.call("dwsim_stream_get_results", {"flowsheet_id": flowsheet_id, "name": tag}, 30)
        actual = readback.get(property_name)
        if not isinstance(actual, (int, float)) or not math.isclose(
            float(actual), target, rel_tol=_READBACK_REL_TOLERANCE, abs_tol=_READBACK_ABS_TOLERANCE
        ):
            raise DwsimEvaluationError("DWSIM_READBACK_MISMATCH", f"DWSIM read-back mismatch for {item.name}", "invalid_input")
        writes.append({"stream": tag, "property": property_name, "requested": target, "readback": float(actual), "unit": target_unit})
    return writes


def _findings(response: dict[str, Any]) -> list[Any]:
    findings = response.get("findings", [])
    blockers = response.get("blockers", [])
    errors = response.get("errors", [])
    return [*findings, *blockers, *errors] if isinstance(findings, list) and isinstance(blockers, list) and isinstance(errors, list) else ["malformed solver diagnostics"]


def _check_objects(response: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    objects = response.get("objects")
    if not isinstance(objects, list) or not objects:
        return False, [{"name": "<object results unavailable>", "calculated": False, "error": "missing object results"}]
    bad = [obj for obj in objects if isinstance(obj, dict) and (obj.get("calculated") is not True or obj.get("error"))]
    return not bad, bad


def _mass_balance(client: DwsimMcpClient, flowsheet_id: str, case_path: Path, objects: list[dict[str, Any]]) -> tuple[float, dict[str, float]]:
    _, tags, available = _connector_records(case_path)
    if not available:
        raise DwsimEvaluationError("DWSIM_CONNECTOR_GRAPH_UNAVAILABLE", "saved case connector graph is unavailable", "result_parse_error")
    object_tags = sorted(
        item["tag"] for item in objects
        if item.get("type") == "MaterialStream" and isinstance(item.get("tag"), str)
    )
    incoming = outgoing = 0.0
    boundary: dict[str, float] = {}
    for tag in object_tags:
        if tag not in tags:
            raise DwsimEvaluationError("DWSIM_STREAM_TOPOLOGY_MISSING", "a material stream has no saved connector record", "result_parse_error")
        connectors = tags[tag]
        has_upstream = any(connector["attached"] for connector in connectors["input"])
        has_downstream = any(connector["attached"] for connector in connectors["output"])
        if has_upstream and has_downstream:
            continue
        result = client.call("dwsim_stream_get_results", {"flowsheet_id": flowsheet_id, "name": tag}, 30)
        flow = result.get("mass_flow_kg_s")
        if not isinstance(flow, (int, float)) or not math.isfinite(float(flow)):
            raise DwsimEvaluationError("DWSIM_STREAM_FLOW_UNAVAILABLE", "boundary stream mass flow is unavailable", "result_parse_error")
        if not has_upstream and has_downstream:
            incoming += float(flow)
            boundary[tag] = float(flow)
        elif has_upstream and not has_downstream:
            outgoing += float(flow)
            boundary[tag] = -float(flow)
    if not boundary:
        raise DwsimEvaluationError("DWSIM_BOUNDARY_STREAMS_MISSING", "flowsheet has no identifiable boundary streams", "result_parse_error")
    return incoming - outgoing, boundary


def _dynamic(client: DwsimMcpClient, flowsheet_id: str, request: EvaluationRequest) -> tuple[dict[str, Any], list[str]]:
    options = request.backend_options
    duration = options.get("duration_s")
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise EvaluationRefusal("invalid_input", "dynamic_duration_invalid", "dynamic_duration_s must be positive")
    started = client.call("dwsim_dynamics_run", {
        "flowsheet_id": flowsheet_id, "duration_s": float(duration), "wait": True,
        "max_wall_time_s": int(options.get("max_wall_time_s", 120)),
    }, 180)
    run_id = started.get("run_id")
    if not isinstance(run_id, str):
        raise DwsimEvaluationError("DWSIM_DYNAMIC_RUN_INVALID", "DWSIM did not return a dynamic run id", "result_parse_error")
    status = client.call("dwsim_dynamics_status", {"run_id": run_id, "include_summary": True}, 30)
    series = client.call("dwsim_dynamics_series", {"run_id": run_id, "max_points": 400}, 30)
    points = series.get("points", series.get("series"))
    times: list[float] = []
    raw_times = series.get("t_s")
    if isinstance(raw_times, list):
        for value in raw_times:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                times.append(number)
    elif isinstance(points, list):
        for point in points:
            value = point.get("time_s", point.get("time", point.get("t"))) if isinstance(point, dict) else None
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                times.append(float(value))
    elif isinstance(series.get("csv"), str):
        for line in series["csv"].splitlines()[1:]:
            try:
                times.append(float(line.split(",", 1)[0]))
            except ValueError:
                continue
    if not times:
        raise DwsimEvaluationError("DWSIM_DYNAMIC_SERIES_MISSING", "dynamic time-series has no readable sample times", "result_parse_error")
    nested = status.get("summary")
    envelope_value = status.get("simulated_s", started.get("simulated_s", status.get("duration_s")))
    nested_value = nested.get("simulated_s", nested.get("duration_s")) if isinstance(nested, dict) else None
    try:
        envelope_time = float(envelope_value) if envelope_value is not None else None
        nested_time = float(nested_value) if nested_value is not None else None
    except (TypeError, ValueError):
        raise DwsimEvaluationError("DWSIM_DYNAMIC_TIMING_INVALID", "DWSIM returned non-numeric timing metadata", "result_parse_error") from None
    discrepancies = []
    series_end = times[-1]
    if isinstance(envelope_time, (int, float)) and not math.isclose(float(envelope_time), series_end, abs_tol=1e-9):
        discrepancies.append(f"envelope duration {envelope_time} s differs from time-series end {series_end} s")
    if isinstance(nested_time, (int, float)) and not math.isclose(float(nested_time), series_end, abs_tol=1e-9):
        discrepancies.append(f"nested summary duration {nested_time} s differs from time-series end {series_end} s")
    summary_errors = nested.get("errors", []) if isinstance(nested, dict) else []
    object_result = client.call("dwsim_flowsheet_list_objects", {"flowsheet_id": flowsheet_id}, 30)
    normalized_status = {
        "ok": status.get("state") == "completed" and isinstance(nested, dict) and nested.get("completed") is True,
        "findings": [], "blockers": [],
        "errors": summary_errors if isinstance(summary_errors, list) else ["malformed dynamic errors"],
        "objects": object_result.get("objects", []),
    }
    return {"run_id": run_id, "status": normalized_status, "series": series, "sample_times_s": times,
            "series_end_s": series_end, "envelope_duration_s": envelope_time,
            "nested_summary_duration_s": nested_time}, discrepancies


class DwsimEvaluator:
    """106 evaluator for exact saved DWSIM cases, always scientifically unqualified."""

    def descriptor(self) -> EvaluatorDescriptor:
        path, _, _ = _configured_runtime()
        version = _version(path) if path else "unavailable"
        return EvaluatorDescriptor(
            evaluator_id=EVALUATOR_ID, backend_kind="dynamic_simulator", backend_name="DWSIM",
            backend_version=version, fidelity="dynamic_detailed",
            capabilities=("steady_state", "dynamic", "stream_condition_writes", "readback", "mass_balance"),
        )

    def availability(self) -> EvaluatorAvailability:
        path, _, reason = _configured_runtime()
        now = datetime.now(UTC)
        if reason or path is None:
            state: AvailabilityState = "not_installed" if path is None or reason == "DWSIM_MCP_EXECUTABLE_UNAVAILABLE" else "unhealthy"
            return EvaluatorAvailability(evaluator_id=EVALUATOR_ID, state=state, checked_at=now,
                                         reason_code=(reason or "DWSIM_MCP_PATH_UNSET").lower())
        version = _version(path)
        if version == "unknown":
            return EvaluatorAvailability(evaluator_id=EVALUATOR_ID, state="unknown", checked_at=now,
                                         reason_code="dwsim_version_unknown")
        return EvaluatorAvailability(evaluator_id=EVALUATOR_ID, state="available", checked_at=now,
                                     backend_version=version)

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        started_at = datetime.now(UTC)
        result_id = f"dwsim-result-{uuid4().hex}"
        result_ref = EvaluationResultRef(authority_owner="dwsim", object_id=result_id,
                                         workspace_id=request.request_ref.workspace_id, revision="1")
        path_value = request.backend_options.get("case_path")
        case_path = Path(path_value).expanduser() if isinstance(path_value, str) else None
        case_hash = _sha256(case_path) if case_path is not None and case_path.is_file() else None
        version = "unknown"
        executable_digest: str | None = None
        status: EvaluationStatus = "succeeded"
        failure = None
        numerical = NumericalDiagnostics()
        outputs: tuple[NamedQuantity, ...] = ()
        try:
            if request.evaluator_id != EVALUATOR_ID:
                raise EvaluationRefusal("unsupported_request", "evaluator_id_mismatch", "request targets another evaluator")
            if case_path is None or not case_path.is_file():
                raise EvaluationRefusal("invalid_input", "case_path_invalid", "backend_options.case_path must name an existing DWSIM case")
            if request.backend_options.keys() - {"case_path", "dynamic", "duration_s", "max_wall_time_s"}:
                raise EvaluationRefusal("unsupported_request", "option_unsupported", "unsupported DWSIM backend option")
            client, executable, version = _client()
            executable_digest = _sha256(executable)
            with client:
                loaded = client.call("dwsim_flowsheet_load", {"filepath": str(case_path.resolve())}, 60)
                flowsheet_id = loaded.get("flowsheet_id")
                if not isinstance(flowsheet_id, str):
                    raise DwsimEvaluationError("DWSIM_LOAD_INVALID", "DWSIM did not return a flowsheet id", "result_parse_error")
                _apply_writes(client, flowsheet_id, request)
                projection = project_flowsheet(client, flowsheet_id, case_path)
                dynamic = None
                timing_diagnostics: list[str] = []
                if request.backend_options.get("dynamic") is True:
                    dynamic, timing_diagnostics = _dynamic(client, flowsheet_id, request)
                    solve = dynamic["status"]
                else:
                    solve = client.call("dwsim_solve_run", {"flowsheet_id": flowsheet_id, "timeout_s": 120}, 150)
                findings = _findings(solve)
                objects_ok, bad_objects = _check_objects(solve)
                if solve.get("ok") is not True or findings:
                    codes = [str(item.get("code", item)) if isinstance(item, dict) else str(item) for item in findings]
                    raise DwsimEvaluationError(codes[0] if codes else "DWSIM_NOT_CONVERGED",
                                               "DWSIM solve reported findings, blockers, or errors")
                if not objects_ok:
                    uncalculated = any(obj.get("calculated") is not True for obj in bad_objects)
                    object_names = ", ".join(str(obj.get("name", "unknown")) for obj in bad_objects)
                    code = "DWSIM_OBJECT_UNCALCULATED" if uncalculated else "DWSIM_OBJECT_ERROR"
                    details = "; ".join(timing_diagnostics)
                    suffix = f"; {details}" if details else ""
                    raise DwsimEvaluationError(code, f"DWSIM has uncalculated/error objects: {object_names}{suffix}")
                mass_residual, boundary = _mass_balance(client, flowsheet_id, case_path, projection["objects"])
                # Dynamic holdup legitimately accumulates mass; only steady state must close.
                if dynamic is None and abs(mass_residual) > _MASS_RESIDUAL_TOLERANCE_KG_S:
                    raise DwsimEvaluationError("DWSIM_MASS_RESIDUAL", "flowsheet boundary mass residual exceeds tolerance")
                numerical = NumericalDiagnostics(converged=True, final_residual=abs(mass_residual) if dynamic is None else None)
                output_values = [NamedQuantity(name="mass_residual", value=Quantity(value=mass_residual, unit="kg/s"))]
                if dynamic is not None:
                    output_values.append(NamedQuantity(name="dynamic_end_time", value=Quantity(value=dynamic["series_end_s"], unit="s")))
                    envelope_time = dynamic.get("envelope_duration_s")
                    nested_time = dynamic.get("nested_summary_duration_s")
                    if isinstance(envelope_time, (int, float)):
                        output_values.append(NamedQuantity(name="envelope_time_discrepancy", value=Quantity(value=float(envelope_time) - dynamic["series_end_s"], unit="s")))
                    if isinstance(nested_time, (int, float)):
                        output_values.append(NamedQuantity(name="nested_summary_time_discrepancy", value=Quantity(value=float(nested_time) - dynamic["series_end_s"], unit="s")))
                outputs = tuple(output_values)
        except EvaluationRefusal as exc:
            status = "refused"
            failure = EvaluationFailure(category=exc.category, backend_code=exc.backend_code, message=str(exc))
        except DwsimExecutableMismatch as exc:
            status = "refused"
            failure = EvaluationFailure(category="not_available", backend_code="DWSIM_MCP_DIGEST_MISMATCH", message=str(exc))
        except DwsimEvaluationError as exc:
            status = "failed" if exc.category not in {"not_available", "invalid_input", "unsupported_request"} else "refused"
            failure = EvaluationFailure(category=exc.category, backend_code=exc.code, message=str(exc))
        except DwsimMcpError as exc:
            status = "failed"
            failure = EvaluationFailure(category="solver_crash", backend_code="DWSIM_MCP_FAILURE", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - unexpected adapter errors become typed failures
            status = "failed"
            failure = EvaluationFailure(category="internal_error", backend_code="DWSIM_ADAPTER_ERROR", message=type(exc).__name__)
        completed_at = datetime.now(UTC)
        validity = ValidityEnvelopeRef(authority_owner="dwsim", object_id=f"{result_id}-validity",
                                       workspace_id=request.request_ref.workspace_id,
                                       qualification_status="unqualified", content_digest="sha256:" + "0" * 64)
        validity = validity.model_copy(update={"content_digest": validity_content_digest(validity)})
        evidence_refs = []
        workspace_id = request.request_ref.workspace_id
        if case_hash is not None and case_path is not None:
            evidence_refs.append(SourceRef(
                authority_owner="dwsim_runtime", object_type="flowsheet_case_file", object_id=case_path.name,
                workspace_id=workspace_id, revision=case_hash, content_digest=f"sha256:{case_hash}",
            ))
        if executable_digest is not None:
            evidence_refs.append(SourceRef(
                authority_owner="dwsim_runtime", object_type="mcp_executable", object_id="dwsim-mcp",
                workspace_id=workspace_id, revision=version, content_digest=f"sha256:{executable_digest}",
            ))
        return EvaluationResult(
            result_ref=result_ref, request_ref=request.request_ref, evaluator_id=EVALUATOR_ID,
            backend_version=version, status=status, failure=failure, fidelity="dynamic_detailed",
            outputs=outputs if status == "succeeded" else (), numerical=numerical,
            validity=validity, evidence_refs=tuple(evidence_refs), completed_at=completed_at, started_at=started_at,
        )
