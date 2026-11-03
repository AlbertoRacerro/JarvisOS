from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from math import isfinite
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.modules.process_kernel.errors import ProcessKernelError
from app.modules.process_kernel.units import normalize_magnitude, parse_unit_token, unit_registry
from app.modules.runner.models import (
    BindingPreviewResponse,
    BindingVariablePreview,
)
from app.modules.runner.safety import RunnerSafetyError, canonical_json

_VARIABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SEMANTIC_KEY = _VARIABLE_NAME
_CATEGORIES = Literal["design", "operating", "property", "model_parameter", "equipment"]


class NumericDomain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    max: float | None = None
    exclusive_min: float | None = None
    exclusive_max: float | None = None

    @field_validator("min", "max", "exclusive_min", "exclusive_max", mode="before")
    @classmethod
    def finite_number_or_none(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("domain bounds must be numeric")
        if not isfinite(float(value)):
            raise ValueError("domain bounds must be finite")
        return float(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> NumericDomain:
        if self.min is not None and self.exclusive_min is not None:
            raise ValueError("lower bound cannot be both inclusive and exclusive")
        if self.max is not None and self.exclusive_max is not None:
            raise ValueError("upper bound cannot be both inclusive and exclusive")
        lower = self.exclusive_min if self.exclusive_min is not None else self.min
        upper = self.exclusive_max if self.exclusive_max is not None else self.max
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError("lower bound must be less than upper bound")
        return self


class InputVariable(BaseModel):
    """Schema-v1 input variable. Its fields and canonical bytes remain unchanged."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    unit: str = Field(min_length=1, max_length=32)
    required: bool
    category: _CATEGORIES
    description: str = Field(min_length=1, max_length=500)
    domain: NumericDomain | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _VARIABLE_NAME.fullmatch(value):
            raise ValueError("variable name must be a stable identifier")
        return value

    @field_validator("label", "unit", "description")
    @classmethod
    def no_edge_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("text fields cannot have leading or trailing whitespace")
        return value


class InputVariableV2(InputVariable):
    model_config = ConfigDict(extra="forbid")

    physical_dimension: str = Field(min_length=1, max_length=64)
    semantic_basis: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("physical_dimension", "semantic_basis")
    @classmethod
    def v2_no_edge_whitespace(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("text fields cannot have leading or trailing whitespace")
        return value


class SemanticContextV3(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applicable_part_kinds: list[str] = Field(min_length=1, max_length=16)
    model_family_key: str = Field(min_length=1, max_length=64)
    model_family_label: str = Field(min_length=1, max_length=120)
    model_option_label: str = Field(min_length=1, max_length=120)

    @field_validator("applicable_part_kinds")
    @classmethod
    def valid_part_kinds(cls, values: list[str]) -> list[str]:
        _require_unique_semantic_keys(values, field_name="applicable_part_kinds")
        return values

    @field_validator("model_family_key")
    @classmethod
    def valid_family_key(cls, value: str) -> str:
        if not _SEMANTIC_KEY.fullmatch(value):
            raise ValueError("model_family_key must be a stable identifier")
        return value

    @field_validator("model_family_label", "model_option_label")
    @classmethod
    def valid_semantic_label(cls, value: str) -> str:
        return _validate_semantic_label(value, field_name="semantic label")


class InputVariableV3(InputVariableV2):
    model_config = ConfigDict(extra="forbid")

    property_group: str = Field(min_length=1, max_length=120)
    applicable_part_kinds: list[str] = Field(max_length=16)

    @field_validator("property_group")
    @classmethod
    def valid_property_group(cls, value: str) -> str:
        return _validate_semantic_label(value, field_name="property_group")

    @field_validator("applicable_part_kinds")
    @classmethod
    def valid_part_kinds(cls, values: list[str]) -> list[str]:
        _require_unique_semantic_keys(values, field_name="applicable_part_kinds")
        return values


class ModelInputContract(BaseModel):
    """Schema-v1 contract retained as the public legacy model."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    evaluation_mode: Literal["forward"]
    variables: list[InputVariable] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_names(self) -> ModelInputContract:
        _require_unique_variable_names(self.variables)
        return self


class ModelInputContractV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    evaluation_mode: Literal["forward"]
    variables: list[InputVariableV2] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_names(self) -> ModelInputContractV2:
        _require_unique_variable_names(self.variables)
        return self


class ModelInputContractV3(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3]
    evaluation_mode: Literal["forward"]
    semantic_context: SemanticContextV3
    variables: list[InputVariableV3] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_semantics(self) -> ModelInputContractV3:
        _require_unique_variable_names(self.variables)
        implementation_kinds = set(self.semantic_context.applicable_part_kinds)
        for variable in self.variables:
            if not set(variable.applicable_part_kinds).issubset(implementation_kinds):
                raise ValueError("variable applicable_part_kinds must be implementation-applicable")
        return self


StoredInputContract: TypeAlias = ModelInputContract | ModelInputContractV2 | ModelInputContractV3
ParameterLoader = Callable[[str], dict[str, object] | None]


def canonicalize_input_contract(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    contract = _validate_contract(payload)
    normalized = contract.model_dump(mode="json", exclude_none=True)
    encoded = canonical_json(normalized)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return encoded, digest, normalized


def parse_stored_input_contract(
    payload: str | None,
    expected_sha256: str | None,
) -> tuple[StoredInputContract, str]:
    if not payload or not expected_sha256:
        raise RunnerSafetyError(
            "runner_input_contract_missing",
            "Model implementation has no input contract.",
        )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != expected_sha256:
        raise RunnerSafetyError(
            "runner_input_contract_hash_mismatch",
            "Model input contract digest does not match stored payload.",
        )
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError("contract must be an object")
        contract = _validate_contract(raw)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise RunnerSafetyError(
            "runner_input_contract_invalid",
            "Stored model input contract is invalid.",
        ) from exc
    if canonical_json(contract.model_dump(mode="json", exclude_none=True)) != payload:
        raise RunnerSafetyError(
            "runner_input_contract_not_canonical",
            "Stored model input contract is not canonical.",
        )
    return contract, digest


def build_binding_preview(
    *,
    model_version_id: str,
    contract_payload: str | None,
    contract_sha256: str | None,
    bindings: dict[str, Any],
    load_parameter: ParameterLoader,
    studio_pint: bool = False,
) -> BindingPreviewResponse:
    contract, digest = parse_stored_input_contract(contract_payload, contract_sha256)
    contract_names = {variable.name for variable in contract.variables}
    global_errors = [f"binding_unknown_variable:{name}" for name in sorted(set(bindings) - contract_names)]
    invalid_count = len(global_errors)
    bound_required = 0
    normalized: dict[str, dict[str, object]] = {}
    variable_results: list[BindingVariablePreview] = []

    for variable in contract.variables:
        item = bindings.get(variable.name)
        if item is None:
            variable_results.append(
                BindingVariablePreview(
                    name=variable.name,
                    label=variable.label,
                    unit=variable.unit,
                    category=variable.category,
                    description=variable.description,
                    required=variable.required,
                    binding_state="missing",
                )
            )
            continue

        source_parameter_updated_at: str | None = None
        if isinstance(contract, (ModelInputContractV2, ModelInputContractV3)):
            value, source_parameter_id, source_parameter_updated_at, errors = _validate_v2_binding(
                variable,
                item,
                load_parameter=load_parameter,
                studio_pint=studio_pint,
            )
        else:
            value, source_parameter_id, errors = _validate_v1_binding(
                variable,
                item,
                load_parameter=load_parameter,
            )

        if errors:
            invalid_count += 1
            state = "invalid"
        else:
            state = "parameter" if source_parameter_id else "manual"
            normalized_item: dict[str, object] = {
                "value": value,
                "unit": variable.unit,
            }
            if source_parameter_id:
                normalized_item["source_parameter_id"] = source_parameter_id
                if source_parameter_updated_at is not None:
                    normalized_item["source_parameter_updated_at"] = source_parameter_updated_at
            normalized[variable.name] = normalized_item
            if variable.required:
                bound_required += 1

        variable_results.append(
            BindingVariablePreview(
                name=variable.name,
                label=variable.label,
                unit=variable.unit,
                category=variable.category,
                description=variable.description,
                required=variable.required,
                binding_state=state,
                value=value,
                source_parameter_id=source_parameter_id,
                errors=errors,
            )
        )

    structural = sum(1 for variable in contract.variables if variable.required)
    unresolved = structural - bound_required
    if invalid_count:
        preview_state = "invalid"
    elif unresolved:
        preview_state = "incomplete"
    else:
        preview_state = "ready"

    return BindingPreviewResponse(
        model_version_id=model_version_id,
        contract_sha256=digest,
        evaluation_mode="forward",
        structural_input_dof=structural,
        bound_input_dof=bound_required,
        unresolved_input_dof=unresolved,
        invalid_binding_count=invalid_count,
        state=preview_state,
        variables=variable_results,
        errors=global_errors,
        normalized_input_set=normalized if preview_state == "ready" else None,
    )


def normalize_input_set_v2(
    contract: ModelInputContractV2 | ModelInputContractV3,
    input_set: dict[str, Any],
    *,
    load_parameter: ParameterLoader | None = None,
    studio_pint: bool = False,
) -> dict[str, dict[str, object]]:
    if set(input_set) != {variable.name for variable in contract.variables}:
        raise RunnerSafetyError("runner_input_invalid", "Input set does not match the schema-v2/v3 contract.")
    normalized: dict[str, dict[str, object]] = {}
    for variable in contract.variables:
        value, source_parameter_id, source_parameter_updated_at, errors = _validate_v2_binding(
            variable,
            input_set[variable.name],
            load_parameter=load_parameter,
            studio_pint=studio_pint,
        )
        if errors or value is None:
            raise RunnerSafetyError(
                "runner_input_invalid",
                f"Invalid schema-v2/v3 input: {variable.name}.",
            )
        normalized_item: dict[str, object] = {"value": value, "unit": variable.unit}
        if source_parameter_id:
            normalized_item["source_parameter_id"] = source_parameter_id
            if source_parameter_updated_at is not None:
                normalized_item["source_parameter_updated_at"] = source_parameter_updated_at
        normalized[variable.name] = normalized_item
    return normalized


def _validate_contract(payload: dict[str, Any]) -> StoredInputContract:
    try:
        schema_version = payload.get("schema_version")
        if schema_version == 1:
            return ModelInputContract.model_validate(payload)
        if schema_version == 2:
            return ModelInputContractV2.model_validate(payload)
        if schema_version == 3:
            return ModelInputContractV3.model_validate(payload)
    except ValidationError as exc:
        raise RunnerSafetyError(
            "runner_input_contract_invalid",
            "Model input contract is invalid.",
        ) from exc
    raise RunnerSafetyError(
        "runner_input_contract_invalid",
        "Model input contract schema version is unsupported.",
    )


def _require_unique_variable_names(
    variables: list[InputVariable] | list[InputVariableV2] | list[InputVariableV3],
) -> None:
    names = [variable.name for variable in variables]
    if len(names) != len(set(names)):
        raise ValueError("variable names must be unique")


def _require_unique_semantic_keys(values: list[str], *, field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} values must be unique")
    if any(not _SEMANTIC_KEY.fullmatch(value) for value in values):
        raise ValueError(f"{field_name} values must be stable identifiers")


def _validate_semantic_label(value: str, *, field_name: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field_name} must already be NFC-normalized")
    if value != value.strip():
        raise ValueError(f"{field_name} cannot have leading or trailing whitespace")
    if any(character in "\r\n\t" for character in value):
        raise ValueError(f"{field_name} must be single-line")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise ValueError(f"{field_name} cannot contain control or format characters")
    return value


def _validate_v1_binding(
    variable: InputVariable,
    item: object,
    *,
    load_parameter: ParameterLoader,
) -> tuple[float | None, str | None, list[str]]:
    errors: list[str] = []
    value: float | None = None
    source_parameter_id: str | None = None
    if not isinstance(item, dict):
        errors.append("binding_object_invalid")
    else:
        allowed = {"value", "unit", "source_parameter_id"}
        if set(item) - allowed or "value" not in item or "unit" not in item:
            errors.append("binding_object_invalid")
        raw_value = item.get("value")
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            errors.append("binding_value_invalid")
        elif not isfinite(float(raw_value)):
            errors.append("binding_value_invalid")
        else:
            value = float(raw_value)
        if item.get("unit") != variable.unit:
            errors.append("binding_unit_invalid")
        raw_source = item.get("source_parameter_id")
        if raw_source is not None:
            if not isinstance(raw_source, str) or not raw_source.strip():
                errors.append("binding_parameter_reference_invalid")
            else:
                source_parameter_id = raw_source

    if value is not None and not errors:
        errors.extend(_domain_errors(variable.domain, value))

    if source_parameter_id is not None and not errors:
        parameter = load_parameter(source_parameter_id)
        if parameter is None:
            errors.append("binding_parameter_not_found")
        else:
            parameter_value = _finite_parameter_value(parameter.get("value"))
            if parameter_value is None:
                errors.append("binding_parameter_value_invalid")
            if parameter.get("unit") != variable.unit:
                errors.append("binding_parameter_unit_mismatch")
            if parameter_value is not None and value is not None and parameter_value != value:
                errors.append("binding_parameter_value_mismatch")
    return value, source_parameter_id, errors


def _validate_v2_binding(
    variable: InputVariableV2,
    item: object,
    *,
    load_parameter: ParameterLoader | None,
    studio_pint: bool = False,
) -> tuple[float | None, str | None, str | None, list[str]]:
    errors: list[str] = []
    value: float | None = None
    source_parameter_id: str | None = None
    source_parameter_updated_at: str | None = None
    if not isinstance(item, dict):
        return None, None, None, ["binding_object_invalid"]
    allowed = {"value", "unit", "source_parameter_id", "source_parameter_updated_at"}
    if set(item) - allowed or "value" not in item or "unit" not in item:
        return None, None, None, ["binding_object_invalid"]
    raw_value = item.get("value")
    source_unit = item.get("unit")
    if (
        isinstance(raw_value, bool)
        or not isinstance(raw_value, int | float)
        or not isinstance(source_unit, str)
    ):
        errors.append("binding_value_invalid")
    else:
        try:
            normalizer = normalize_studio_magnitude if studio_pint else normalize_magnitude
            value = normalizer(
                float(raw_value),
                source_unit=source_unit,
                target_unit=variable.unit,
                physical_dimension=variable.physical_dimension,
                semantic_basis=variable.semantic_basis,
            )
        except ProcessKernelError:
            errors.append("binding_unit_invalid")
    if value is not None and not errors:
        errors.extend(_domain_errors(variable.domain, value))

    raw_source = item.get("source_parameter_id")
    if raw_source is not None:
        if not isinstance(raw_source, str) or not raw_source.strip():
            errors.append("binding_parameter_reference_invalid")
        else:
            source_parameter_id = raw_source

    supplied_updated_at = item.get("source_parameter_updated_at")
    if supplied_updated_at is not None and (
        not isinstance(supplied_updated_at, str) or not supplied_updated_at.strip()
    ):
        errors.append("binding_parameter_revision_invalid")
    elif supplied_updated_at is not None and source_parameter_id is None:
        errors.append("binding_parameter_revision_without_reference")

    if source_parameter_id is not None and not errors:
        if load_parameter is None:
            return value, source_parameter_id, None, errors
        parameter = load_parameter(source_parameter_id)
        if parameter is None:
            errors.append("binding_parameter_not_found")
        else:
            parameter_value = _finite_parameter_value(parameter.get("value"))
            parameter_unit = parameter.get("unit")
            parameter_updated_at = parameter.get("updated_at")
            if parameter_value is None or not isinstance(parameter_unit, str):
                errors.append("binding_parameter_value_invalid")
            elif not isinstance(parameter_updated_at, str) or not parameter_updated_at.strip():
                errors.append("binding_parameter_revision_invalid")
            else:
                try:
                    normalizer = normalize_studio_magnitude if studio_pint else normalize_magnitude
                    normalized_parameter_value = normalizer(
                        parameter_value,
                        source_unit=parameter_unit,
                        target_unit=variable.unit,
                        physical_dimension=variable.physical_dimension,
                        semantic_basis=variable.semantic_basis,
                    )
                except ProcessKernelError:
                    errors.append("binding_parameter_unit_mismatch")
                else:
                    if value is not None and normalized_parameter_value != value:
                        errors.append("binding_parameter_value_mismatch")
                    if supplied_updated_at is not None and supplied_updated_at != parameter_updated_at:
                        errors.append("binding_parameter_revision_mismatch")
                    if not errors:
                        source_parameter_updated_at = parameter_updated_at
    return value, source_parameter_id, source_parameter_updated_at, errors


def normalize_studio_magnitude(
    value: float,
    *,
    source_unit: str,
    target_unit: str,
    physical_dimension: str,
    semantic_basis: str | None,
) -> float:
    if not physical_dimension.startswith("["):
        return normalize_magnitude(
            value, source_unit=source_unit, target_unit=target_unit,
            physical_dimension=physical_dimension, semantic_basis=semantic_basis,
        )
    if semantic_basis is not None or not isfinite(value):
        raise ProcessKernelError("quantity_dimension_mismatch", "Invalid Studio quantity dimension.")
    try:
        registry = unit_registry()
        source = registry.Unit(parse_unit_token(source_unit))
        target = registry.Unit(parse_unit_token(target_unit))
        if str(source.dimensionality) != physical_dimension or str(target.dimensionality) != physical_dimension:
            raise ProcessKernelError("quantity_dimension_mismatch", "Quantity unit does not match the contract.")
        converted = float(registry.Quantity(value, source).to(target).magnitude)
    except ProcessKernelError:
        raise
    except Exception as exc:
        raise ProcessKernelError("quantity_unit_invalid", "Studio unit conversion failed.") from exc
    if not isfinite(converted):
        raise ProcessKernelError("quantity_value_invalid", "Converted quantity must be finite.")
    return converted


def _finite_parameter_value(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _domain_errors(domain: NumericDomain | None, value: float) -> list[str]:
    if domain is None:
        return []
    if domain.min is not None and value < domain.min:
        return ["binding_domain_violation"]
    if domain.max is not None and value > domain.max:
        return ["binding_domain_violation"]
    if domain.exclusive_min is not None and value <= domain.exclusive_min:
        return ["binding_domain_violation"]
    if domain.exclusive_max is not None and value >= domain.exclusive_max:
        return ["binding_domain_violation"]
    return []
