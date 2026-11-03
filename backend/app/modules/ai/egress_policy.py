from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

EXTERNAL_PROVIDER_OPERATION = "external_provider_call"
CONFIRMABLE_TRIGGERS = frozenset({"t1", "t2", "t5"})
_REQUIRED_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "policy_version",
        "trigger_version",
        "max_prompt_chars",
        "max_context_blocks",
        "max_context_chars",
        "sample_rate_bps",
        "confirmation_ticket_ttl_seconds",
        "daily_soft_spend_usd",
        "reservation_ttl_seconds",
        "confirmable_triggers",
        "supported_operations",
    }
)


@dataclass(frozen=True)
class EgressPolicyConfig:
    schema_version: int
    policy_version: str
    trigger_version: str
    max_prompt_chars: int
    max_context_blocks: int
    max_context_chars: int
    sample_rate_bps: int
    confirmation_ticket_ttl_seconds: int
    daily_soft_spend_usd: float
    reservation_ttl_seconds: int
    confirmable_triggers: tuple[str, ...]
    supported_operations: tuple[str, ...]
    config_digest: str


def default_egress_policy_path() -> Path:
    return Path(__file__).resolve().parents[4] / "configs" / "ai_egress_policy.json"


def load_egress_policy(path: str | Path | None = None) -> EgressPolicyConfig:
    canonical_path = default_egress_policy_path().resolve()
    requested_path = canonical_path if path is None else Path(path).resolve()
    if requested_path != canonical_path:
        raise ValueError("egress policy must load from configs/ai_egress_policy.json")
    with requested_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return parse_egress_policy(raw)


@lru_cache(maxsize=1)
def load_default_egress_policy() -> EgressPolicyConfig:
    return load_egress_policy()


def parse_egress_policy(raw: Any) -> EgressPolicyConfig:
    if not isinstance(raw, dict):
        raise ValueError("egress policy must be a JSON object")
    keys = frozenset(raw)
    missing = _REQUIRED_POLICY_KEYS - keys
    extra = keys - _REQUIRED_POLICY_KEYS
    if missing:
        raise ValueError(f"egress policy missing keys: {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"egress policy has unsupported keys: {', '.join(sorted(extra))}")

    schema_version = _positive_int(raw["schema_version"], "schema_version")
    if schema_version != 1:
        raise ValueError("egress policy schema_version must be 1")

    sample_rate_bps = _bounded_int(raw["sample_rate_bps"], "sample_rate_bps", 500, 10_000)
    confirmable_triggers = _unique_string_tuple(raw["confirmable_triggers"], "confirmable_triggers")
    if not confirmable_triggers:
        raise ValueError("confirmable_triggers must not be empty")
    if not set(confirmable_triggers).issubset(CONFIRMABLE_TRIGGERS):
        raise ValueError("confirmable_triggers contains a non-confirmable trigger")

    supported_operations = _unique_string_tuple(raw["supported_operations"], "supported_operations")
    if supported_operations != (EXTERNAL_PROVIDER_OPERATION,):
        raise ValueError("supported_operations must contain only external_provider_call")

    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return EgressPolicyConfig(
        schema_version=schema_version,
        policy_version=_required_string(raw["policy_version"], "policy_version"),
        trigger_version=_required_string(raw["trigger_version"], "trigger_version"),
        max_prompt_chars=_positive_int(raw["max_prompt_chars"], "max_prompt_chars"),
        max_context_blocks=_positive_int(raw["max_context_blocks"], "max_context_blocks"),
        max_context_chars=_positive_int(raw["max_context_chars"], "max_context_chars"),
        sample_rate_bps=sample_rate_bps,
        confirmation_ticket_ttl_seconds=_positive_int(
            raw["confirmation_ticket_ttl_seconds"], "confirmation_ticket_ttl_seconds"
        ),
        daily_soft_spend_usd=_nonnegative_float(raw["daily_soft_spend_usd"], "daily_soft_spend_usd"),
        reservation_ttl_seconds=_positive_int(raw["reservation_ttl_seconds"], "reservation_ttl_seconds"),
        confirmable_triggers=confirmable_triggers,
        supported_operations=supported_operations,
        config_digest=digest,
    )


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    parsed = _positive_int(value, field)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def _nonnegative_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be numeric")
    parsed = float(value)
    if parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    return parsed


def _unique_string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    parsed = tuple(_required_string(item, field) for item in value)
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{field} contains duplicates")
    return parsed
