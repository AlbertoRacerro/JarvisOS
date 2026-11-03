"""Deterministic, operator-configured candidate selection for one cloud step."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.provider_registry import ProviderRegistry, load_default_provider_registry, resolve_model_pricing


@dataclass(frozen=True)
class CloudCandidate:
    binding: ProviderBinding
    quality_tier: int
    qualification: str
    qualification_evidence_ref: str
    projected_cost_usd: Decimal
    pricing_version: str
    pricing_reviewed_on: date
    pricing_source_url: str


@dataclass(frozen=True)
class CloudCatalog:
    max_request_usd: Decimal
    max_thread_usd: Decimal
    max_output_tokens: int
    eur_usd_rate: Decimal
    fx_date: str
    fx_source: str
    task_floors: dict[str, int]
    candidates: tuple[tuple[str, int, str, str | None, date, str], ...]


def load_catalog(path: Path | None = None) -> CloudCatalog:
    target = path or Path(__file__).resolve().parents[4] / "configs" / "ai_cloud_escalation.json"
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {
        "version", "max_request_usd", "max_thread_usd", "max_output_tokens", "eur_usd_rate",
        "fx_date", "fx_source", "task_floors", "candidates"
    } or raw["version"] != 1:
        raise ValueError("invalid cloud escalation catalog")
    request_cap = _positive_decimal(raw["max_request_usd"])
    thread_cap = _positive_decimal(raw["max_thread_usd"])
    output_cap = raw["max_output_tokens"]
    if not isinstance(output_cap, int) or isinstance(output_cap, bool) or not 1 <= output_cap <= 4096:
        raise ValueError("invalid cloud output ceiling")
    floors = raw["task_floors"]
    if not isinstance(floors, dict) or not floors or any(
        not isinstance(key, str) or not key or not isinstance(value, int)
        or isinstance(value, bool) or value < 1 for key, value in floors.items()
    ):
        raise ValueError("invalid cloud task floors")
    listed = raw["candidates"]
    if not isinstance(listed, list) or not listed:
        raise ValueError("cloud catalog needs candidates")
    candidates: list[tuple[str, int, str, str | None, date, str]] = []
    for item in listed:
        if not isinstance(item, dict) or set(item) != {
            "route_class", "quality_tier", "qualification", "qualification_evidence_ref",
            "pricing_reviewed_on", "pricing_source_url"
        }:
            raise ValueError("invalid cloud candidate")
        route, tier, qualification = item["route_class"], item["quality_tier"], item["qualification"]
        if not isinstance(route, str) or not route.startswith("external:") or not isinstance(tier, int) \
                or isinstance(tier, bool) or tier < 1 or not isinstance(qualification, str) or not qualification.strip():
            raise ValueError("invalid cloud candidate")
        try:
            reviewed = date.fromisoformat(item["pricing_reviewed_on"])
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid cloud price review date") from exc
        source_url = item["pricing_source_url"]
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            raise ValueError("invalid cloud price source")
        evidence_ref = item["qualification_evidence_ref"]
        if evidence_ref is not None and (not isinstance(evidence_ref, str) or not evidence_ref.strip()):
            raise ValueError("invalid cloud qualification evidence")
        candidates.append((route, tier, qualification, evidence_ref, reviewed, source_url))
    if len({route for route, _, _, _, _, _ in candidates}) != len(candidates):
        raise ValueError("duplicate cloud candidate")
    fx_rate = _positive_decimal(raw["eur_usd_rate"])
    fx_date, fx_source = raw["fx_date"], raw["fx_source"]
    if not isinstance(fx_date, str) or not isinstance(fx_source, str) or not fx_source.startswith("https://"):
        raise ValueError("invalid cloud FX source")
    return CloudCatalog(request_cap, thread_cap, output_cap, fx_rate, fx_date, fx_source, floors, tuple(candidates))


def select_candidate(
    catalog: CloudCatalog, *, task_family: str, derivative_content: str,
    registry: ProviderRegistry | None = None,
) -> CloudCandidate:
    registry = registry or load_default_provider_registry()
    floor = catalog.task_floors.get(task_family)
    if floor is None:
        raise ValueError("unqualified task family")
    eligible: list[CloudCandidate] = []
    for route, tier, qualification, evidence_ref, reviewed, source_url in catalog.candidates:
        today = datetime.now(UTC).date()
        if reviewed > today or today - reviewed > timedelta(days=30):
            continue
        binding = registry.bindings.get(route)
        if binding is None or binding.execution_class != "external_provider" or not binding.requires_network:
            continue
        if tier < floor or not qualification.strip() or evidence_ref is None:
            continue
        pricing = resolve_model_pricing(registry, binding.provider_id, binding.model_id)
        if pricing.currency != "USD":
            continue
        if binding.context_window_tokens is None or len(derivative_content.encode("utf-8")) \
                + catalog.max_output_tokens > binding.context_window_tokens:
            continue
        # The 059b packet uses UTF-8 byte count as its conservative input-token ceiling.
        input_ceiling = len(derivative_content.encode("utf-8")) + 1024
        cost = (
            Decimal(input_ceiling) * Decimal(str(pricing.input_usd_per_1m_tokens))
            + Decimal(catalog.max_output_tokens) * Decimal(str(pricing.output_usd_per_1m_tokens))
        ) / Decimal(1_000_000)
        if cost <= catalog.max_request_usd:
            eligible.append(CloudCandidate(binding, tier, qualification, evidence_ref, cost, pricing.pricing_version, reviewed, source_url))
    if not eligible:
        raise ValueError("no eligible model within the task and request budget")
    return min(eligible, key=lambda item: (item.projected_cost_usd, item.binding.provider_id, item.binding.model_id))


def _positive_decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError("invalid cloud budget") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("invalid cloud budget")
    return parsed
