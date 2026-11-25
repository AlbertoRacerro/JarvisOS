"""Deterministic number provenance ledger and verification checker for Hermes turns.

Spec 363 (HERMES-NUMBER-PROVENANCE-1):
Per-turn numeric evidence collection from succeeded tool results and deterministic
answer checking against gathered evidence, reusing numeric parsing from literature_checks.
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.modules.memory.literature_checks import _NUMBER_TOKEN, _same_number, number_tokens

MAX_ENTRIES = 40
MAX_RAW_LEN = 48
MAX_SOURCE_REF_LEN = 120
MAX_TOOL_NAME_LEN = 80
MAX_ROUTE_HINT_LEN = 120
MAX_LEDGER_ITEMS = 1000
RELATIVE_ROUNDING_TOLERANCE = 0.01

_FORBIDDEN_PATH_OR_SECRET = re.compile(
    r"(?:/home/|/tmp/|/var/|/usr/|/etc/|[A-Za-z]:[/\\]|\b(?:secret|password|credential|token|api_key|private_key)\b)",
    re.IGNORECASE,
)
_SAFE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}\Z")
_METADATA_KEYS = frozenset({
    "id", "run_id", "source_id", "call_id", "tool_call_id", "doi", "url", "uri",
    "pub_no", "publication_number", "year", "created_at", "updated_at",
    "timestamp", "date", "title", "key", "source_ref", "route_hint",
})

_ISO_DATE_PATTERN = re.compile(
    r"\b\d{4}[-/]\d{2}(?:[-/]\d{2})?(?:[T\s]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)

_YEAR_CITATION_PATTERN = re.compile(
    r"(?:\((?:[A-Za-z\s.,]+)?(?:19|20)\d{2}\)|\b(?:in|year|since|dated|published)\s+(?:19|20)\d{2}\b)",
    re.IGNORECASE,
)

_HEADING_LINE_PATTERN = re.compile(r"^\s*#{1,6}\s+.*$", re.MULTILINE)
_LIST_INDEX_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)(?:[\.\)]\s+)", re.MULTILINE)


@dataclass(frozen=True)
class NumericEvidence:
    value: float
    source_ref: str
    tool_name: str
    tool_call_id: str
    route_hint: str | None = None


def normalize_tool_name(tool_name: str) -> str:
    """Normalize tool names by stripping MCP prefixes."""
    if tool_name.startswith("mcp__jarvis__"):
        return tool_name[len("mcp__jarvis__") :]
    return tool_name


def _is_safe_string(val: str) -> bool:
    """Check if string is safe to use in a source ref (no secrets, no paths)."""
    if not isinstance(val, str) or not _SAFE_REF.fullmatch(val):
        return False
    if _FORBIDDEN_PATH_OR_SECRET.search(val):
        return False
    return True


def derive_source_ref_and_route(
    tool_name: str,
    call_id: str,
    arguments: dict[str, Any] | None = None,
    item_context: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Derive safe source_ref and route_hint for a tool call or sub-item."""
    norm_name = normalize_tool_name(tool_name)
    call_id = call_id if _is_safe_string(call_id) else "unknown"
    args = arguments or {}
    ctx = item_context or {}

    # Bench tools -> /runs
    if norm_name in {"jarvis_bench_run", "jarvis_bench_read", "bench_run", "bench_read"}:
        run_id = ctx.get("run_id") or args.get("run_id") or args.get("run_id_a") or ctx.get("id")
        if isinstance(run_id, str) and _is_safe_string(run_id):
            return f"bench:{run_id}"[:MAX_SOURCE_REF_LEN], "/runs"
        return f"bench:{call_id}"[:MAX_SOURCE_REF_LEN], "/runs"

    # Literature / Paper tools -> /memory/literature
    if norm_name in {"jarvis_paper_search", "paper_search", "jarvis_paper_pdf_read", "paper_pdf_read"}:
        doi = ctx.get("doi") or args.get("doi")
        if isinstance(doi, str) and _is_safe_string(doi):
            return f"doi:{doi}"[:MAX_SOURCE_REF_LEN], "/memory/literature"
        source_id = ctx.get("source_id") or args.get("source_id") or ctx.get("id")
        if isinstance(source_id, str) and _is_safe_string(source_id):
            return f"literature:{source_id}"[:MAX_SOURCE_REF_LEN], "/memory/literature"
        return f"tool:{call_id}"[:MAX_SOURCE_REF_LEN], "/memory/literature"

    # Patent tools -> /memory/literature
    if norm_name in {"jarvis_patent_read", "patent_read"}:
        pub_no = (
            ctx.get("pub_no")
            or ctx.get("publication_number")
            or args.get("pub_no")
            or args.get("publication_number")
        )
        if isinstance(pub_no, str) and _is_safe_string(pub_no):
            return f"patent:{pub_no}"[:MAX_SOURCE_REF_LEN], "/memory/literature"
        return f"patent:{call_id}"[:MAX_SOURCE_REF_LEN], "/memory/literature"

    # Knowledge read tools
    if norm_name in {"jarvis_knowledge_read", "knowledge_read"}:
        view = args.get("view") or ctx.get("view")
        source_id = ctx.get("source_id") or args.get("source_id")
        if isinstance(source_id, str) and _is_safe_string(source_id):
            return f"literature:{source_id}"[:MAX_SOURCE_REF_LEN], "/memory/literature"
        if view in {"literature_sources", "literature_data"}:
            return f"literature:{call_id}"[:MAX_SOURCE_REF_LEN], "/memory/literature"
        # other knowledge views -> /memory/search
        safe_view = view if isinstance(view, str) and _is_safe_string(view) else call_id
        return f"knowledge:{safe_view}"[:MAX_SOURCE_REF_LEN], "/memory/search"

    # Turn plan
    if norm_name in {"jarvis_turn_plan", "turn_plan"}:
        sref = ctx.get("source_ref")
        if isinstance(sref, str) and _is_safe_string(sref):
            route = (
                "/runs"
                if sref.startswith("bench:")
                else "/memory/literature"
                if sref.startswith(("doi:", "literature:", "patent:"))
                else None
            )
            return sref[:MAX_SOURCE_REF_LEN], route
        return f"tool:{call_id}"[:MAX_SOURCE_REF_LEN], None

    # Context preview / retrieval query -> /memory/search
    if norm_name in {
        "jarvis_context_preview",
        "context_preview",
        "jarvis_retrieval_query",
        "retrieval_query",
    }:
        return f"tool:{call_id}"[:MAX_SOURCE_REF_LEN], "/memory/search"

    # Default fallback
    return f"tool:{call_id}"[:MAX_SOURCE_REF_LEN], None


def extract_tool_numeric_evidence(
    *,
    tool_name: str,
    call_id: str,
    arguments: dict[str, Any] | None = None,
    result: Any = None,
) -> list[NumericEvidence]:
    """Extract bounded numeric tokens from a succeeded tool result into evidence entries."""
    if result is None:
        return []

    norm_name = normalize_tool_name(tool_name)
    admitted = {
        "jarvis_python",
        "jarvis_bench_run",
        "bench_run",
        "jarvis_bench_read",
        "bench_read",
        "jarvis_knowledge_read",
        "knowledge_read",
        "jarvis_paper_search",
        "paper_search",
        "jarvis_paper_pdf_read",
        "paper_pdf_read",
        "jarvis_patent_read",
        "patent_read",
        "jarvis_browser_page_read",
        "browser_page_read",
        "jarvis_context_preview",
        "context_preview",
        "jarvis_turn_plan",
        "turn_plan",
    }
    if norm_name not in admitted:
        return []

    evidence: list[NumericEvidence] = []
    default_ref, default_route = derive_source_ref_and_route(norm_name, call_id, arguments)

    # 1. Special handling for turn plan findings
    if norm_name in {"jarvis_turn_plan", "turn_plan"} and isinstance(result, dict):
        findings = result.get("findings")
        if isinstance(findings, list):
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                f_sref, f_route = derive_source_ref_and_route(norm_name, call_id, arguments, finding)
                val = finding.get("value")
                if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
                    evidence.append(
                        NumericEvidence(
                            value=float(val),
                            source_ref=f_sref,
                            tool_name=norm_name[:MAX_TOOL_NAME_LEN],
                            tool_call_id=call_id,
                            route_hint=f_route,
                        )
                    )
                elif isinstance(val, str):
                    for tok in number_tokens(val):
                        evidence.append(
                            NumericEvidence(
                                value=tok,
                                source_ref=f_sref,
                                tool_name=norm_name[:MAX_TOOL_NAME_LEN],
                                tool_call_id=call_id,
                                route_hint=f_route,
                            )
                        )

    # 2. General recursive walker for other structures
    def walk(obj: Any, current_ref: str, current_route: str | None, depth: int = 0) -> None:
        if len(evidence) >= MAX_LEDGER_ITEMS or depth > 8:
            return

        if isinstance(obj, dict):
            # Check for item-specific identifier
            item_ref, item_route = derive_source_ref_and_route(norm_name, call_id, arguments, obj)
            effective_ref = item_ref if item_ref != f"tool:{call_id}" else current_ref
            effective_route = item_route or current_route

            for k, v in obj.items():
                if len(evidence) >= MAX_LEDGER_ITEMS:
                    break
                k_lower = str(k).lower()
                # Skip secret or forbidden keys
                if k_lower in _METADATA_KEYS or any(
                    bad in k_lower for bad in ("password", "secret", "token", "credential", "private_key", "path")
                ):
                    continue
                walk(v, effective_ref, effective_route, depth + 1)

        elif isinstance(obj, (list, tuple)):
            for item in obj:
                if len(evidence) >= MAX_LEDGER_ITEMS:
                    break
                walk(item, current_ref, current_route, depth + 1)

        elif isinstance(obj, (int, float)) and not isinstance(obj, bool) and math.isfinite(obj):
            evidence.append(
                NumericEvidence(
                    value=float(obj),
                    source_ref=current_ref[:MAX_SOURCE_REF_LEN],
                    tool_name=norm_name[:MAX_TOOL_NAME_LEN],
                    tool_call_id=call_id,
                    route_hint=current_route[:MAX_ROUTE_HINT_LEN] if current_route else None,
                )
            )

        elif isinstance(obj, str):
            # Source passages can be long; refs are checked separately. Avoid
            # filesystem paths and secrets, while bounding the text scanned.
            if _FORBIDDEN_PATH_OR_SECRET.search(obj):
                return
            for tok in number_tokens(obj[:100_000]):
                if len(evidence) >= MAX_LEDGER_ITEMS:
                    break
                evidence.append(
                    NumericEvidence(
                        value=tok,
                        source_ref=current_ref[:MAX_SOURCE_REF_LEN],
                        tool_name=norm_name[:MAX_TOOL_NAME_LEN],
                        tool_call_id=call_id,
                        route_hint=current_route[:MAX_ROUTE_HINT_LEN] if current_route else None,
                    )
                )

    walk(result, default_ref, default_route)
    return evidence[:MAX_LEDGER_ITEMS]


def extract_assistant_numbers(text: str) -> list[tuple[str, float]]:
    """Extract numeric values from assistant prose, skipping trivial structural tokens."""
    if not text:
        return []

    # Identify spans to skip:
    skip_spans: list[tuple[int, int]] = []

    # 1. Markdown headings
    for match in _HEADING_LINE_PATTERN.finditer(text):
        skip_spans.append((match.start(), match.end()))

    # 2. List indices (e.g. "1. ", "2) ", "1.1 ")
    for match in _LIST_INDEX_PATTERN.finditer(text):
        # Only the numeric index part should be skipped as a structural token
        skip_spans.append((match.start(1), match.end(1)))

    # 3. ISO dates/timestamps
    for match in _ISO_DATE_PATTERN.finditer(text):
        skip_spans.append((match.start(), match.end()))

    # 4. Citations and standalone years in date context
    for match in _YEAR_CITATION_PATTERN.finditer(text):
        skip_spans.append((match.start(), match.end()))

    def is_skipped(start: int, end: int) -> bool:
        for s_start, s_end in skip_spans:
            if not (end <= s_start or start >= s_end):
                return True
        return False

    candidates: list[tuple[str, float]] = []
    for match in _NUMBER_TOKEN.finditer(text):
        start, end = match.start(), match.end()
        if is_skipped(start, end):
            continue
        raw = match.group(0)
        parsed = number_tokens(raw)
        if parsed:
            candidates.append((raw, parsed[0]))

    return candidates


def match_number(
    raw: str,
    value: float,
    evidence_ledger: Sequence[NumericEvidence],
) -> tuple[Literal["verified", "unverified"], NumericEvidence | None]:
    """Match an extracted number against the turn's numeric evidence ledger.

    Prefer exact matches using `_same_number`. When not exact, allow bounded
    rounding tolerance up to 1% relative when the evidence value rounds to the
    displayed precision.
    """
    # 1. Exact match pass
    for ev in evidence_ledger:
        if not math.isfinite(ev.value):
            continue
        if _same_number(value, ev.value):
            return "verified", ev

    # 2. Bounded rounding tolerance up to 1% relative
    best_ev: NumericEvidence | None = None
    best_rel_err: float = float("inf")

    cleaned = raw.replace(",", ".")
    if "." in cleaned and "e" not in cleaned.lower() and "×" not in cleaned:
        decimals = len(cleaned.split(".", 1)[1])
    else:
        decimals = 0

    for ev in evidence_ledger:
        if not math.isfinite(ev.value):
            continue
        rounded_ev = round(ev.value, decimals)
        if _same_number(rounded_ev, value):
            if ev.value == 0:
                rel_err = abs(value - ev.value)
            else:
                rel_err = abs(value - ev.value) / abs(ev.value)
            if rel_err <= RELATIVE_ROUNDING_TOLERANCE + 1e-9:
                if rel_err < best_rel_err:
                    best_rel_err = rel_err
                    best_ev = ev

    if best_ev is not None:
        return "verified", best_ev

    return "unverified", None


def compute_number_provenance(
    assistant_text: str | None,
    evidence_ledger: Sequence[NumericEvidence],
    *,
    interaction_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    """Deterministic answer checker: produce the bounded hermes.number_provenance payload."""
    if not assistant_text:
        return {
            "interaction_id": interaction_id,
            "workspace_id": workspace_id,
            "verified_count": 0,
            "unverified_count": 0,
            "entries": [],
        }

    raw_candidates = extract_assistant_numbers(assistant_text)
    entries: list[dict[str, Any]] = []

    for raw, value in raw_candidates:
        status, ev = match_number(raw, value, evidence_ledger)
        entry: dict[str, Any] = {
            "raw": raw[:MAX_RAW_LEN],
            "value": value,
            "status": status,
        }
        if status == "verified" and ev is not None:
            if ev.source_ref:
                entry["source_ref"] = ev.source_ref[:MAX_SOURCE_REF_LEN]
            if ev.tool_name:
                entry["tool_name"] = ev.tool_name[:MAX_TOOL_NAME_LEN]
            if ev.route_hint:
                entry["route_hint"] = ev.route_hint[:MAX_ROUTE_HINT_LEN]

        entries.append(entry)
        if len(entries) >= MAX_ENTRIES:
            break

    verified_count = sum(1 for e in entries if e["status"] == "verified")
    unverified_count = sum(1 for e in entries if e["status"] == "unverified")

    return {
        "interaction_id": interaction_id,
        "workspace_id": workspace_id,
        "verified_count": verified_count,
        "unverified_count": unverified_count,
        "entries": entries,
    }
