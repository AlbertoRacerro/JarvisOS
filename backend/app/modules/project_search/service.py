from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from app.core.search_text import match_rank
from app.modules.memory.literature_search import search_literature_sources
from app.modules.modeling.model_dossier_search import search_model_dossier_index
from app.modules.modeling.project_search_owner import search_context_records_literal
from app.modules.project_search.models import ProjectSearchKind, ProjectSearchResponse, ProjectSearchResult

PROJECT_SEARCH_KINDS: tuple[ProjectSearchKind, ...] = (
    "requirement",
    "parameter",
    "assumption",
    "decision",
    "model",
    "literature_source",
    "literature_entry",
)
_KIND_ORDER = {kind: index for index, kind in enumerate(PROJECT_SEARCH_KINDS)}
_TIER_ORDER = {"exact": 0, "prefix": 1, "contains": 2, "normalized": 3, "tokens": 4}
_MAX_OWNER_MATCHES = 101
_MODELING_KINDS = ("requirement", "parameter", "assumption", "decision")
_MODELING_STATUSES = {
    "requirement": ["draft", "active"],
    "parameter": ["candidate", "literature", "measured", "validated", "accepted"],
    "assumption": ["proposed", "accepted", "rejected", "superseded"],
    # Decision.status is owner-defined/free-form. None means all owner-visible statuses.
    "decision": None,
}
_MAX_SUMMARY_CHARS = 12_000


def _nonempty(values: Iterable[str | None]) -> list[str]:
    return [value for value in values if isinstance(value, str) and value.strip()]


def _match(query: str, fields: dict[str, str | None]) -> tuple[str, list[str]] | None:
    ranks = {name: match_rank(query, raw) for name, raw in fields.items()}
    best = min(ranks.values(), default=99)
    if best == 99:
        return None
    return list(_TIER_ORDER)[best], [name for name, rank in ranks.items() if rank == best]


def _summary(*values: str | None) -> str | None:
    parts = _nonempty(values)
    summary = " · ".join(parts[:3]) if parts else None
    return summary[:_MAX_SUMMARY_CHARS] if summary is not None else None


def _modeling_results(workspace_id: str, query: str, kinds: list[str]) -> list[ProjectSearchResult]:
    if not kinds:
        return []
    selected = search_context_records_literal(
        workspace_id,
        kinds=kinds,
        statuses_by_kind={kind: _MODELING_STATUSES[kind] for kind in kinds},
        query=query,
        max_matches_per_kind=_MAX_OWNER_MATCHES,
    )
    results: list[ProjectSearchResult] = []
    for kind in kinds:
        for record in selected[kind]:
            if kind == "decision" and record.basis_lifecycle_state != "active":
                continue
            if kind == "requirement":
                fields = {
                    "statement": record.statement,
                    "rationale": record.rationale,
                    "notes": record.notes,
                }
                title = record.statement
                summary = _summary(record.rationale, record.notes)
                status = record.status
                source_refs: list[str] = []
            elif kind == "parameter":
                fields = {"name": record.name, "symbol": record.symbol, "notes": record.notes}
                title = record.name
                value = f"{record.value} {record.unit}" if record.value is not None else None
                summary = _summary(value, record.notes)
                status = f"{record.lifecycle_state} · {record.value_status}"
                source_refs = _nonempty([record.source_ref])
            elif kind == "assumption":
                fields = {"statement": record.statement, "notes": record.notes}
                title = record.statement
                summary = _summary(record.scope, record.notes)
                status = record.status
                source_refs = _nonempty([record.source_ref])
            else:
                fields = {
                    "title": record.title,
                    "decision_text": record.decision_text,
                    "rationale": record.rationale,
                    "notes": record.notes,
                }
                title = record.title
                summary = _summary(record.decision_text, record.rationale, record.notes)
                status = record.status
                source_refs = []
            fields["id"] = record.id
            matched = _match(query, fields)
            if matched is None:
                continue
            tier, match_fields = matched
            stable_ref = f"{kind}:{record.id}"
            results.append(
                ProjectSearchResult(
                    kind=cast(ProjectSearchKind, kind),
                    owner="modeling",
                    stable_ref=stable_ref,
                    workspace_id=workspace_id,
                    title=title[:8_000],
                    summary=summary,
                    lifecycle_or_status=status[:256] if status is not None else None,
                    version_or_revision=None,
                    provenance_refs=[],
                    source_refs=source_refs,
                    route="/memory/project-basis",
                    route_params={"recordKind": kind, "recordId": record.id},
                    match_fields=match_fields,
                    match_tier=cast(Any, tier),
                )
            )
    return results


def _model_results(workspace_id: str, query: str) -> list[ProjectSearchResult]:
    results: list[ProjectSearchResult] = []
    for model in search_model_dossier_index(workspace_id, query):
        if not model.versions:
            matched = _match(
                query,
                {
                    "title": model.title,
                    "engineering_question": model.engineering_question,
                    "scope": model.scope,
                },
            )
            if matched is not None:
                tier, match_fields = matched
                results.append(
                    ProjectSearchResult(
                        kind="model",
                        owner="model-dossier",
                        stable_ref=f"model_spec:{model.model_spec_id}",
                        workspace_id=workspace_id,
                        title=model.title[:8_000],
                        summary=_summary(model.engineering_question, model.scope),
                        lifecycle_or_status=None,
                        version_or_revision=None,
                        provenance_refs=[],
                        source_refs=[],
                        route="/memory/models",
                        route_params={"modelSpecId": model.model_spec_id},
                        match_fields=match_fields,
                        match_tier=cast(Any, tier),
                    )
                )
            continue
        for version in model.versions:
            fields = {
                "title": model.title,
                "engineering_question": model.engineering_question,
                "scope": model.scope,
                "version_label": version.version_label,
                "implementation_kind": version.implementation_kind,
            }
            matched = _match(query, fields)
            if matched is None:
                continue
            tier, match_fields = matched
            results.append(
                ProjectSearchResult(
                    kind="model",
                    owner="model-dossier",
                    stable_ref=f"model_version:{version.model_version_id}",
                    workspace_id=workspace_id,
                    title=model.title[:8_000],
                    summary=_summary(model.engineering_question, model.scope),
                    lifecycle_or_status=version.status[:256] if version.status is not None else None,
                    version_or_revision=version.version_label[:500] if version.version_label is not None else None,
                    provenance_refs=[],
                    source_refs=[],
                    route="/memory/models",
                    route_params={
                        "modelSpecId": version.model_spec_id,
                        "modelVersionId": version.model_version_id,
                    },
                    match_fields=match_fields,
                    match_tier=cast(Any, tier),
                )
            )
    return results


def _literature_results(
    workspace_id: str, query: str, kinds: set[ProjectSearchKind]
) -> list[ProjectSearchResult]:
    results: list[ProjectSearchResult] = []
    for source in search_literature_sources(workspace_id, query):
        if "literature_source" in kinds:
            matched = _match(
                query,
                {
                    "title": source.title,
                    "citation": source.citation,
                    "publisher": source.publisher,
                },
            )
            if matched is not None:
                tier, match_fields = matched
                results.append(
                    ProjectSearchResult(
                        kind="literature_source",
                        owner="literature",
                        stable_ref=source.source_ref,
                        workspace_id=workspace_id,
                        title=source.title[:8_000],
                        summary=_summary(source.citation, source.publisher),
                        lifecycle_or_status=source.state[:256] if source.state is not None else None,
                        version_or_revision=None,
                        provenance_refs=[],
                        source_refs=[source.source_ref],
                        route="/memory/literature",
                        route_params={"sourceId": source.id},
                        match_fields=match_fields,
                        match_tier=cast(Any, tier),
                    )
                )
        if "literature_entry" not in kinds:
            continue
        for entry in source.entries:
            value_number = None if entry.value_number is None else str(entry.value_number)
            matched = _match(
                query,
                {
                    "statement": entry.statement,
                    "value_text": entry.value_text,
                    "value_number": value_number,
                    "unit": entry.unit,
                    "context_text": entry.context_text,
                },
            )
            if matched is None:
                continue
            tier, match_fields = matched
            title = entry.statement or entry.value_text or (
                f"{entry.value_number} {entry.unit or ''}".strip() if entry.value_number is not None else "Literature datum"
            )
            route_params = {"sourceId": source.id, "entryId": entry.id}
            if entry.locator_kind is not None and entry.locator_start is not None:
                route_params["locatorKind"] = entry.locator_kind
                route_params["locatorStart"] = str(entry.locator_start)
                if entry.locator_end is not None:
                    route_params["locatorEnd"] = str(entry.locator_end)
            results.append(
                ProjectSearchResult(
                    kind="literature_entry",
                    owner="literature",
                    stable_ref=entry.provenance_ref,
                    workspace_id=workspace_id,
                    title=title[:8_000],
                    summary=_summary(entry.context_text, source.title),
                    lifecycle_or_status=entry.status[:256] if entry.status is not None else None,
                    version_or_revision=None,
                    provenance_refs=[entry.provenance_ref],
                    source_refs=[source.source_ref],
                    route="/memory/literature",
                    route_params=route_params,
                    match_fields=match_fields,
                    match_tier=cast(Any, tier),
                )
            )
    return results


def search_project(
    workspace_id: str,
    *,
    query: str,
    kinds: list[ProjectSearchKind] | None,
    limit: int,
) -> ProjectSearchResponse:
    requested = list(kinds or PROJECT_SEARCH_KINDS)
    requested_set = set(requested)
    candidates: list[ProjectSearchResult] = []
    modeling_kinds: list[str] = [kind for kind in _MODELING_KINDS if kind in requested_set]
    candidates.extend(_modeling_results(workspace_id, query, modeling_kinds))
    if "model" in requested_set:
        candidates.extend(_model_results(workspace_id, query))
    literature_kinds = requested_set & {"literature_source", "literature_entry"}
    if literature_kinds:
        candidates.extend(_literature_results(workspace_id, query, literature_kinds))

    deduplicated: dict[str, ProjectSearchResult] = {}
    for item in candidates:
        previous = deduplicated.get(item.stable_ref)
        if previous is None or _TIER_ORDER[item.match_tier] < _TIER_ORDER[previous.match_tier]:
            deduplicated[item.stable_ref] = item
    ordered = sorted(
        deduplicated.values(),
        key=lambda item: (
            _TIER_ORDER[item.match_tier],
            _KIND_ORDER[item.kind],
            item.title.casefold(),
            item.stable_ref,
        ),
    )
    truncated = len(ordered) > limit
    items = ordered[:limit]
    return ProjectSearchResponse(
        query=query,
        items=items,
        total_returned=len(items),
        truncated=truncated,
    )
