"""Shared request/result mechanics for the 104 upstream evaluators."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from app.modules.engineering.evaluator_contracts import (
    EngineeringEvaluator,
    EvaluationFailure,
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    EvaluatorAvailability,
    FailureCategory,
    NamedQuantity,
    NumericalDiagnostics,
    OptionValue,
)
from app.modules.engineering.refs import EvaluationResultRef, Quantity
from app.modules.process_kernel.errors import ProcessKernelError
from app.modules.process_kernel.units import parse_unit_token, unit_registry


class EvaluationRefusal(Exception):
    """Raised inside an evaluator body; becomes a typed non-success result."""

    def __init__(self, category: FailureCategory, backend_code: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.backend_code = backend_code
        self.message = message


def magnitude(quantity: Quantity, unit: str) -> float:
    """Convert through the single Pint owner; offset temperatures are handled by Pint."""
    registry = unit_registry()
    try:
        converted = registry.Quantity(quantity.value, parse_unit_token(quantity.unit)).to(parse_unit_token(unit))
    except ProcessKernelError as exc:
        raise EvaluationRefusal("invalid_input", "unit_unknown", str(exc)) from exc
    except Exception as exc:  # Pint raises several dimensionality/offset error types.
        raise EvaluationRefusal(
            "invalid_input", "unit_dimension_mismatch", f"{quantity.unit} cannot be converted to {unit}"
        ) from exc
    return float(converted.magnitude)


def required_inputs(request: EvaluationRequest, units: Mapping[str, str]) -> dict[str, float]:
    """Exactly the named inputs, in evaluator units; unknown names are refused, not ignored."""
    given = {item.name: item.value for item in request.inputs}
    unknown = sorted(set(given) - set(units))
    missing = sorted(set(units) - set(given))
    if unknown or missing:
        raise EvaluationRefusal(
            "invalid_input", "input_names_invalid", f"missing={missing} unknown={unknown}"
        )
    return {name: magnitude(given[name], unit) for name, unit in units.items()}


def option(request: EvaluationRequest, name: str, allowed: tuple[str, ...], default: str) -> str:
    unknown = sorted(set(request.backend_options) - {name})
    if unknown:
        raise EvaluationRefusal("unsupported_request", "option_unsupported", f"unsupported options {unknown}")
    value: OptionValue = request.backend_options.get(name, default)
    if not isinstance(value, str) or value not in allowed:
        raise EvaluationRefusal("unsupported_request", "option_unsupported", f"{name} must be one of {allowed}")
    return value


def quantities(values: Mapping[str, tuple[float, str]]) -> tuple[NamedQuantity, ...]:
    return tuple(
        NamedQuantity(name=name, value=Quantity(value=value, unit=unit)) for name, (value, unit) in values.items()
    )


def evaluate_with(
    evaluator: EngineeringEvaluator,
    request: EvaluationRequest,
    body: Callable[[], tuple[tuple[NamedQuantity, ...], NumericalDiagnostics]],
    *,
    cancelled: Callable[[str], bool] | None = None,
) -> EvaluationResult:
    """Run ``body`` and map every outcome, including refusal and deadline, to a typed result."""
    descriptor = evaluator.descriptor()
    started = datetime.now(UTC)
    outputs: tuple[NamedQuantity, ...] = ()
    numerical = NumericalDiagnostics()
    failure: EvaluationFailure | None = None
    status: EvaluationStatus = "succeeded"
    if request.evaluator_id != descriptor.evaluator_id:
        status, failure = "refused", EvaluationFailure(
            category="unsupported_request", backend_code="evaluator_mismatch",
            message=f"request targets {request.evaluator_id}",
        )
    elif request.is_expired(started):
        status, failure = "deadline_exceeded", EvaluationFailure(category="timeout", backend_code="deadline_passed")
    elif cancelled is not None and request.cancellation_id is not None and cancelled(request.cancellation_id):
        status, failure = "cancelled", EvaluationFailure(category="cancelled", backend_code="cancelled")
    else:
        availability = evaluator.availability()
        if availability.state != "available":
            status, failure = "refused", EvaluationFailure(
                category="not_available", backend_code=availability.reason_code or "not_available",
            )
        else:
            clock = time.perf_counter()
            try:
                outputs, numerical = body()
            except EvaluationRefusal as exc:
                failure = EvaluationFailure(category=exc.category, backend_code=exc.backend_code, message=exc.message[:1024])
                status = "refused" if exc.category in {
                    "not_available", "invalid_input", "unsupported_request", "outside_validity_domain",
                    "resource_unavailable",
                } else "failed"
            except Exception as exc:  # noqa: BLE001 - upstream native failures become typed results
                failure = EvaluationFailure(
                    category="solver_crash", backend_code=f"{descriptor.backend_name}:{type(exc).__name__}"[:96],
                    message=str(exc)[:1024],
                )
                status = "failed"
            elapsed = time.perf_counter() - clock
            numerical = numerical.model_copy(update={"wall_time_s": elapsed})
    completed = datetime.now(UTC)
    if status == "succeeded" and request.is_expired(completed):
        outputs, status = (), "deadline_exceeded"
        failure = EvaluationFailure(category="timeout", backend_code="deadline_passed")
    if status != "succeeded":
        outputs = ()
        numerical = NumericalDiagnostics(wall_time_s=numerical.wall_time_s)
    return EvaluationResult(
        result_ref=EvaluationResultRef(
            authority_owner="process_stack",
            object_id=f"{request.request_ref.object_id}/{descriptor.evaluator_id}",
            workspace_id=request.request_ref.workspace_id,
            revision=request.request_ref.revision or "1",
        ),
        request_ref=request.request_ref,
        evaluator_id=descriptor.evaluator_id,
        backend_version=descriptor.backend_version,
        status=status,
        failure=failure,
        fidelity=descriptor.fidelity,
        outputs=outputs,
        numerical=numerical,
        qualification_record_ref=descriptor.qualification_record_ref,
        started_at=started,
        completed_at=completed,
    )


def import_availability(evaluator_id: str, module: str, version: Callable[[], str]) -> EvaluatorAvailability:
    """Truthful 'can it run here': the upstream package must import and report a version."""
    now = datetime.now(UTC)
    try:
        __import__(module)
        found = version()
    except ImportError:
        return EvaluatorAvailability(evaluator_id=evaluator_id, state="not_installed", checked_at=now,
                                     reason_code=f"{module}_not_installed")
    except Exception:  # noqa: BLE001 - a broken install is unhealthy, not absent
        return EvaluatorAvailability(evaluator_id=evaluator_id, state="unhealthy", checked_at=now,
                                     reason_code=f"{module}_import_failed")
    return EvaluatorAvailability(evaluator_id=evaluator_id, state="available", checked_at=now, backend_version=found)


def descriptor_version(module: str) -> str:
    try:
        return str(__import__(module).__version__)
    except Exception:  # noqa: BLE001 - descriptor must exist even when the backend is absent
        return "not_installed"
