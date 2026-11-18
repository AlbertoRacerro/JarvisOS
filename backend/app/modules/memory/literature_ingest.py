"""Deterministic checks for values a local model proposes from one stored PDF (spec 297).

A proposal is evidence about the source text only. A value passes only when its quote is on the cited page, the
number and unit are re-derived from that page text, and the quote matches one narrow label-value-unit shape with the
conditions its label requires. Everything else is flagged with a reason; nothing here grants calculation use.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.database import open_sqlite_connection
from app.modules.ai.execution import run_ai_task
from app.modules.events.service import utc_now
from app.modules.memory.literature_checks import _NUMBER_TOKEN, number_tokens
from app.modules.memory.pdf_text import PdfTextError, cached_pdf_pages
from app.modules.process_kernel.errors import ProcessKernelError
from app.modules.process_kernel.units import parse_unit_token, unit_registry

EXTRACTOR_ID = "literature_ingest"
EXTRACTOR_VERSION = "1"

IngestionStatus = Literal["source_checked", "flagged"]


@dataclass(frozen=True)
class QuantityLabel:
    name: str
    aliases: tuple[str, ...]
    reference_units: tuple[str, ...]
    requires_conditions: bool


@dataclass(frozen=True)
class ConditionLabel:
    name: str
    aliases: tuple[str, ...]
    reference_unit: str


# Generic physical quantities only. Project, organism or apparatus terms never enter the core table.
QUANTITY_LABELS: tuple[QuantityLabel, ...] = (
    QuantityLabel("density", ("density",), ("kg/m^3",), True),
    QuantityLabel("dynamic viscosity", ("dynamic viscosity", "viscosity"), ("Pa*s",), True),
    QuantityLabel("solubility", ("solubility",), ("g/L", "mol/L"), True),
    QuantityLabel("diffusivity", ("diffusivity", "diffusion coefficient"), ("m^2/s",), True),
    QuantityLabel("surface tension", ("surface tension",), ("N/m",), True),
    QuantityLabel("thermal conductivity", ("thermal conductivity",), ("W/(m*K)",), True),
    QuantityLabel("specific heat capacity", ("specific heat capacity", "specific heat"), ("J/(kg*K)",), True),
    QuantityLabel("specific growth rate", ("specific growth rate", "growth rate"), ("1/h",), True),
    QuantityLabel("molar mass", ("molar mass", "molecular weight"), ("g/mol",), False),
    QuantityLabel("diameter", ("inner diameter", "outer diameter", "diameter"), ("m",), False),
    QuantityLabel("length", ("length",), ("m",), False),
)

CONDITION_LABELS: tuple[ConditionLabel, ...] = (
    ConditionLabel("temperature", ("temperature", "T"), "K"),
    ConditionLabel("pressure", ("pressure", "p", "P"), "Pa"),
    ConditionLabel("photon flux density", ("photon flux density", "light intensity", "irradiance"), "umol/m^2/s"),
)

_SEPARATOR = r"(?:is|=|:|of|was|were)"
_QUALIFIER = r"of\s+(?P<qualifier>[A-Za-z][\w₀-₉+\-]*)"
_UNIT = r"(?P<unit>[^\s,;()]+(?:\s(?:s|h|K))?)"


class ProposedCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    value_text: str = Field(min_length=1, max_length=40)
    unit: str = Field(min_length=1, max_length=40)


class Proposal(BaseModel):
    """What the model proposes; never stored as a value without the checks below."""

    model_config = ConfigDict(extra="forbid")
    quantity: str = Field(min_length=1, max_length=120)
    value_text: str = Field(min_length=1, max_length=40)
    unit: str = Field(min_length=1, max_length=40)
    conditions: list[ProposedCondition] = Field(default_factory=list, max_length=4)
    page: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=600)


@dataclass
class CheckOutcome:
    check: str
    passed: bool | None  # None: not applicable
    reason: str | None = None


@dataclass
class ValidatedValue:
    status: IngestionStatus
    quantity: str
    value_number: float | None
    unit: str
    conditions: list[dict[str, object]]
    page: int
    quote: str
    checks: list[CheckOutcome] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [item.reason for item in self.checks if item.passed is False and item.reason]


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _dimensionality(unit: str) -> object:
    registry = unit_registry()
    return registry.get_dimensionality(parse_unit_token(unit))


def _same_dimension(unit: str, reference: str) -> bool:
    try:
        return _dimensionality(unit) == _dimensionality(reference)
    except ProcessKernelError:
        return False


def _label_for(quantity: str) -> QuantityLabel | None:
    wanted = _normalise(quantity).lower()
    for label in QUANTITY_LABELS:
        for alias in label.aliases:
            if wanted == alias or wanted.startswith(f"{alias} of "):
                return label
    return None


def _condition_label(name: str) -> ConditionLabel | None:
    wanted = _normalise(name)
    for label in CONDITION_LABELS:
        if wanted in label.aliases or wanted.lower() in label.aliases:
            return label
    return None


def _number(text: str) -> float | None:
    found = number_tokens(text)
    return found[0] if len(found) == 1 else None


_RANGE = re.compile(r"\d\s*(?:[-–—]|to)\s*[−\-+]?\d|\d\s*/\s*\d|±|\+/-")
_NEGATION = re.compile(r"\b(?:not|no|never|neither|nor)\b", re.IGNORECASE)
_CONDITION_AT = re.compile(r"\s*(?:,\s*|\band\s+)?(?:at|under|@)\s+(?P<num>\S+)\s*" + _UNIT)
_CONDITION_PAREN = re.compile(r"\s*\(\s*(?P<name>[A-Za-z][\w ]{0,40}?)\s*=\s*(?P<num>\S+)\s*" + _UNIT + r"\s*\)")


def _shape(quote: str, label: QuantityLabel) -> tuple[re.Match[str] | None, str | None]:
    """Find the single label-value-unit shape in the quote; return a reason code when there is none."""
    aliases = "|".join(re.escape(alias) for alias in sorted(label.aliases, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<![\w])(?P<label>{aliases})(?:\s+{_QUALIFIER})?\s*(?:{_SEPARATOR})?\s*"
        rf"(?P<num>[^\s]+)\s*{_UNIT}",
        re.IGNORECASE,
    )
    matches = [match for match in pattern.finditer(quote) if _NUMBER_TOKEN.fullmatch(match["num"])]
    if not matches:
        return None, "label_value_unit_shape_missing"
    if len(matches) > 1:
        return None, "multiple_numbers_after_separator"
    return matches[0], None


def _conditions_after(quote: str, start: int) -> tuple[list[tuple[str, float, str]], int]:
    found: list[tuple[str, float, str]] = []
    position = start
    while True:
        rest = quote[position:]
        match = _CONDITION_AT.match(rest)
        name: str | None = None
        if match is None:
            match = _CONDITION_PAREN.match(rest)
            if match is None:
                break
            label = _condition_label(match["name"])
            name = label.name if label else None
        number = _number(match["num"]) if _NUMBER_TOKEN.fullmatch(match["num"]) else None
        if number is None:
            break
        unit = match["unit"].rstrip(".")
        if name is None:
            name = next((item.name for item in CONDITION_LABELS if _same_dimension(unit, item.reference_unit)), None)
        if name is None:
            break
        found.append((name, number, unit))
        position += match.end()
    return found, position


def validate_proposal(proposal: Proposal, pages: list[str]) -> ValidatedValue:
    """Run the source, unit and meaning checks on one proposal against the cited page text."""
    checks: list[CheckOutcome] = []
    page_text = _normalise(pages[proposal.page - 1]) if proposal.page <= len(pages) else ""
    quote = _normalise(proposal.quote)
    label = _label_for(proposal.quantity)
    result = ValidatedValue(status="flagged", quantity=_normalise(proposal.quantity), value_number=None,
                            unit=proposal.unit.strip(), conditions=[], page=proposal.page, quote=quote, checks=checks)

    if not page_text or quote not in page_text:
        checks.append(CheckOutcome("source_occurrence", False, "quote_not_on_page"))
        return result
    proposed_number = _number(proposal.value_text)
    if proposed_number is None or proposed_number not in number_tokens(quote):
        checks.append(CheckOutcome("source_occurrence", False, "number_not_in_quote"))
        return result
    checks.append(CheckOutcome("source_occurrence", True))

    if label is None:
        checks.append(CheckOutcome("meaning_and_conditions", False, "label_not_in_table"))
        return result
    try:
        parse_unit_token(proposal.unit.strip())
    except ProcessKernelError:
        checks.append(CheckOutcome("unit_dimension", False, "unit_unknown"))
        return result
    if not any(_same_dimension(proposal.unit.strip(), reference) for reference in label.reference_units):
        checks.append(CheckOutcome("unit_dimension", False, "unit_dimension_mismatch"))
        return result
    checks.append(CheckOutcome("unit_dimension", True))

    reason = _meaning_reason(proposal, quote, label, result)
    checks.append(CheckOutcome("meaning_and_conditions", reason is None, reason))
    if reason is None:
        result.status = "source_checked"
    return result


def _meaning_reason(proposal: Proposal, quote: str, label: QuantityLabel, result: ValidatedValue) -> str | None:
    if _NEGATION.search(quote):
        return "negation"
    if _RANGE.search(quote):
        return "range_or_ratio"
    match, reason = _shape(quote, label)
    if match is None:
        if reason == "label_value_unit_shape_missing" and len(number_tokens(quote)) >= 3:
            return "table_meaning_unverified"
        return reason
    number = _number(match["num"])
    unit = match["unit"].rstrip(".")
    if number is None or number != _number(proposal.value_text):
        return "number_differs_from_page"
    if unit != proposal.unit.strip():
        return "unit_differs_from_page"
    conditions, end = _conditions_after(quote, match.end())
    for proposed in proposal.conditions:
        condition_label = _condition_label(proposed.name)
        proposed_number = _number(proposed.value_text)
        if condition_label is None or not any(
            name == condition_label.name and value == proposed_number and unit == proposed.unit.strip()
            for name, value, unit in conditions
        ):
            return "condition_not_in_source"
    # Numbers outside the matched value and condition clauses (unit exponents are inside them).
    outside = number_tokens(f"{quote[:match.start()]} {quote[end:]}")
    if len(outside) >= 2:
        return "table_meaning_unverified"
    if outside:
        return "extra_numbers_in_quote"
    if label.requires_conditions and not conditions:
        return "conditions_missing"
    qualifier = match["qualifier"]
    result.quantity = f"{label.name} of {qualifier}" if qualifier else label.name
    result.value_number = number
    result.unit = unit
    result.conditions = [{"name": name, "value": value, "unit": unit} for name, value, unit in sorted(conditions)]
    return None


# --- Batch ingestion (one stored PDF -> proposals -> checks -> entries) ----------------------------------------------

MAX_INGEST_PAGES = 40
MAX_MODEL_CALLS = 12
MAX_PROPOSALS = 200
WINDOW_CHARS = 12_000
INGEST_ROUTE_CLASS = "local:general"
_ADAPTERS: dict | None = None  # tests inject a scripted local adapter; production uses the default adapters
INTERRUPTED_AFTER_SECONDS = 1800  # a running batch with no heartbeat for this long was cut off (restart, crash)


def is_interrupted(batch) -> bool:
    """A 'running' batch whose last heartbeat is older than the limit; read and retried as failed/interrupted."""
    if batch["state"] != "running":
        return False
    age = datetime.now(UTC) - datetime.fromisoformat(str(batch["updated_at"]))
    return age.total_seconds() > INTERRUPTED_AFTER_SECONDS

_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"values": {"type": "array", "items": Proposal.model_json_schema()}},
    "required": ["values"],
}


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: list[Proposal] = Field(max_length=MAX_PROPOSALS)


def _windows(pages: list[str]) -> list[list[int]]:
    windows: list[list[int]] = []
    current: list[int] = []
    size = 0
    for number, text in enumerate(pages, start=1):
        if current and size + len(text) > WINDOW_CHARS:
            windows.append(current)
            current, size = [], 0
        current.append(number)
        size += len(text)
    if current:
        windows.append(current)
    return windows


def _prompt(pages: list[str], window: list[int]) -> str:
    body = "\n".join(f"=== page {number} ===\n{pages[number - 1][:WINDOW_CHARS]}" for number in window)
    return (
        "List the numeric physical values stated in these pages of a published paper. Reply with one JSON object "
        '{"values": [...]} and nothing else. Each value has quantity (as the paper words it), value_text (the number '
        "exactly as written), unit (as written), conditions (list of {name, value_text, unit}), page (the page number "
        "shown) and quote (the verbatim sentence or table row containing it). Copy text exactly; do not compute, "
        "convert or infer values.\n" + body
    )


def _parse_reply(text: str) -> list[Proposal]:
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    return _Reply.model_validate(json.loads(body)).values


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_conditions(conditions: list[dict[str, object]]) -> str:
    return json.dumps(conditions, sort_keys=True, separators=(",", ":"))


def _range_and_dimension(connection, workspace_id: str, value: ValidatedValue) -> list[CheckOutcome]:
    """Workspace dimension agreement and the sourced plausible range from accepted parameters of the same name."""
    rows = connection.execute(
        """
        SELECT unit, value_min, value_max FROM parameters
        WHERE workspace_id = ? AND lower(name) = lower(?) AND status = 'accepted' AND lifecycle_state = 'active'
        """,
        (workspace_id, value.quantity),
    ).fetchall()
    registry = unit_registry()
    range_check = CheckOutcome("plausible_range", None, None)
    for row in rows:
        unit = str(row["unit"] or "")
        if not _same_dimension(value.unit, unit):
            return [CheckOutcome("unit_dimension", False, "workspace_unit_dimension_mismatch")]
        if row["value_min"] is None and row["value_max"] is None or value.value_number is None:
            continue
        converted = registry.Quantity(value.value_number, parse_unit_token(value.unit)).to(parse_unit_token(unit))
        number = float(converted.magnitude)
        outside = (row["value_min"] is not None and number < float(row["value_min"])) or (
            row["value_max"] is not None and number > float(row["value_max"]))
        range_check = CheckOutcome("plausible_range", not outside, "outside_plausible_range" if outside else None)
        if outside:
            break
    return [range_check]


def _duplicate_or_contradiction(connection, workspace_id: str, value: ValidatedValue) -> tuple[CheckOutcome, str | None]:
    rows = connection.execute(
        """
        SELECT e.id, e.value_number, e.unit FROM literature_entries e
        JOIN literature_sources s ON s.id = e.source_id AND s.workspace_id = e.workspace_id
        JOIN artifacts a ON a.id = s.artifact_id AND a.workspace_id = s.workspace_id
        WHERE e.workspace_id = ? AND e.quantity = ? AND e.conditions_json = ? AND e.ingestion_status = 'source_checked'
          AND e.source_sha256 = a.sha256
        ORDER BY e.created_at, e.id
        """,
        (workspace_id, value.quantity, _canonical_conditions(value.conditions)),
    ).fetchall()
    for row in rows:
        if row["unit"] == value.unit and row["value_number"] == value.value_number:
            return CheckOutcome("duplicate_contradiction", True, None), str(row["id"])
    if any(row["unit"] == value.unit for row in rows):
        return CheckOutcome("duplicate_contradiction", False, "contradiction"), None
    return CheckOutcome("duplicate_contradiction", True, None), None


def _batch_row(connection, source_id: str, sha256: str):
    return connection.execute(
        "SELECT * FROM literature_ingestion_batches WHERE source_id = ? AND source_sha256 = ? AND extractor_version = ?",
        (source_id, sha256, EXTRACTOR_VERSION),
    ).fetchone()


def _finish(connection, batch_id: str, state: str, reason: str | None = None, **counts: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in counts)
    connection.execute(
        f"UPDATE literature_ingestion_batches SET state = ?, reason = ?, updated_at = ?"
        f"{', ' + assignments if assignments else ''} WHERE id = ?",
        (state, reason, utc_now(), *counts.values(), batch_id),
    )
    connection.commit()


def ingest_source(workspace_id: str, source_id: str, *, retry: bool = False) -> str | None:
    """Ingest one source's stored PDF once per (source, bytes, extractor version); return the batch id."""
    from app.modules.memory.literature_service import (
        _artifact_row_or_none,
        _backing_is_linked,
        _content_state,
        _require_workspace,
        _source_row,
    )

    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        source = _source_row(connection, workspace_id, source_id)
        if source["artifact_id"] is None:
            return None
        artifact = _artifact_row_or_none(connection, workspace_id, str(source["artifact_id"]))
        if artifact is None:
            return None
        availability, path = _content_state(artifact)
        if _backing_is_linked(artifact):
            availability, path = "unsafe", None
        sha256 = _file_sha256(path) if path is not None else str(artifact["sha256"] or "")
        existing = _batch_row(connection, source_id, sha256)
        now = utc_now()
        if existing is None:
            batch_id = f"lib_{uuid4().hex}"
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO literature_ingestion_batches
                    (id, workspace_id, source_id, source_sha256, extractor_version, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (batch_id, workspace_id, source_id, sha256, EXTRACTOR_VERSION, now, now),
            )
            if inserted.rowcount != 1:  # a concurrent create/attach trigger won the same (source, bytes, version)
                connection.commit()
                return str(_batch_row(connection, source_id, sha256)["id"])
        else:
            batch_id = str(existing["id"])
            if not retry:
                return batch_id
            cutoff = (datetime.now(UTC) - timedelta(seconds=INTERRUPTED_AFTER_SECONDS)).isoformat()
            claimed = connection.execute(
                """
                UPDATE literature_ingestion_batches SET state = 'running', reason = NULL, updated_at = ?
                WHERE id = ? AND (state IN ('model_unavailable', 'failed') OR (state = 'running' AND updated_at < ?))
                """,
                (now, batch_id, cutoff),
            )
            if claimed.rowcount != 1:  # not retryable, or another retry already claimed it
                connection.commit()
                return batch_id
        connection.commit()
        try:
            _run_batch(connection, workspace_id, source_id, batch_id, artifact, availability, path, sha256)
        except Exception:  # noqa: BLE001 - never leave a batch 'running' with no worker behind it.
            connection.rollback()
            _finish(connection, batch_id, "failed", "internal_error")
        return batch_id


def _run_batch(connection, workspace_id: str, source_id: str, batch_id: str, artifact, availability: str,
               path: Path | None, sha256: str) -> None:
    if str(artifact["mime_type"] or "") != "application/pdf":
        _finish(connection, batch_id, "unavailable", "not_pdf")
        return
    if availability != "available" or path is None:
        _finish(connection, batch_id, "unavailable", f"backing_{availability if availability != 'available' else 'unsafe'}")
        return
    try:
        pages = cached_pdf_pages(path)
    except PdfTextError:
        _finish(connection, batch_id, "unavailable", "unreadable_pdf")
        return
    if not pages:
        _finish(connection, batch_id, "unavailable", "no_text_layer")
        return
    truncated = len(pages) > MAX_INGEST_PAGES
    windows = _windows(pages[:MAX_INGEST_PAGES])
    if len(windows) > MAX_MODEL_CALLS:
        windows, truncated = windows[:MAX_MODEL_CALLS], True

    proposals: list[tuple[Proposal, str]] = []
    job_ids: list[str] = []
    for window in windows:
        try:
            outcome = run_ai_task(user_prompt=_prompt(pages, window), task_kind="general",
                                  route_class=INGEST_ROUTE_CLASS, max_output_tokens=4096,
                                  structured_output_schema=_RESPONSE_SCHEMA, workspace_id=workspace_id,
                                  adapters=_ADAPTERS)
        except Exception:  # noqa: BLE001 - any AI-route failure means no local model answered.
            _finish(connection, batch_id, "model_unavailable", "ai_route_failed",
                    ai_job_ids_json=json.dumps(job_ids))
            return
        job_ids.append(outcome.ledger_id)
        connection.execute("UPDATE literature_ingestion_batches SET updated_at = ? WHERE id = ?", (utc_now(), batch_id))
        connection.commit()
        if outcome.status != "success" or outcome.response is None or not outcome.response.text:
            reason = outcome.decision.blocked_reason or outcome.error_type or outcome.status
            _finish(connection, batch_id, "model_unavailable", str(reason)[:200],
                    ai_job_ids_json=json.dumps(job_ids))
            return
        try:
            found = _parse_reply(outcome.response.text)
        except (ValueError, ValidationError):
            _finish(connection, batch_id, "failed", "malformed_model_output", ai_job_ids_json=json.dumps(job_ids))
            return
        proposals.extend((item, outcome.ledger_id) for item in found)
    if len(proposals) > MAX_PROPOSALS:
        proposals, truncated = proposals[:MAX_PROPOSALS], True

    checked = flagged = 0
    duplicates: list[str] = []
    for proposal, job_id in proposals:
        value = validate_proposal(proposal, pages)
        if value.status == "source_checked":
            value.checks.extend(_range_and_dimension(connection, workspace_id, value))
            duplicate_check, duplicate_of = _duplicate_or_contradiction(connection, workspace_id, value)
            if duplicate_of is not None:
                duplicates.append(duplicate_of)
                continue
            value.checks.append(duplicate_check)
            if value.reasons:
                value.status = "flagged"
        checked += value.status == "source_checked"
        flagged += value.status == "flagged"
        connection.execute(
            """
            INSERT INTO literature_entries
                (id, workspace_id, source_id, entry_kind, value_text, value_number, unit, status, locator_kind,
                 locator_start, locator_end, context_text, quantity, conditions_json, ingestion_status,
                 ingestion_reasons_json, batch_id, source_sha256, extractor_id, extractor_version, ai_job_id,
                 created_at, updated_at)
            VALUES (?, ?, ?, 'datum', ?, ?, ?, 'raw', 'page', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"lit_entry_{uuid4().hex}", workspace_id, source_id, proposal.value_text.strip(),
             value.value_number, value.unit, value.page, value.page, value.quote, value.quantity,
             _canonical_conditions(value.conditions), value.status,
             json.dumps([{"check": item.check, "passed": item.passed, "reason": item.reason}
                         for item in value.checks]),
             batch_id, sha256, EXTRACTOR_ID, EXTRACTOR_VERSION, job_id, utc_now(), utc_now()),
        )
    _finish(connection, batch_id, "done", "truncated" if truncated else None,
            proposals_count=len(proposals), source_checked_count=checked, flagged_count=flagged,
            duplicate_entry_ids_json=json.dumps(sorted(set(duplicates))), truncated=int(truncated),
            notification_state="pending" if flagged else "none", ai_job_ids_json=json.dumps(job_ids))
    return


def acknowledge_batch(workspace_id: str, source_id: str, batch_id: str) -> None:
    with open_sqlite_connection() as connection:
        updated = connection.execute(
            """
            UPDATE literature_ingestion_batches SET notification_state = 'read', updated_at = ?
            WHERE id = ? AND source_id = ? AND workspace_id = ? AND notification_state = 'pending'
            """,
            (utc_now(), batch_id, source_id, workspace_id),
        )
        connection.commit()
        if updated.rowcount != 1:
            exists = connection.execute(
                "SELECT 1 FROM literature_ingestion_batches WHERE id = ? AND source_id = ? AND workspace_id = ?",
                (batch_id, source_id, workspace_id),
            ).fetchone()
            if exists is None:
                raise LookupError(batch_id)


def ingestion_eligibility(row, current_sha256: str | None, verdict: str | None) -> tuple[bool, bool, list[str]]:
    """The single read-time rule: (eligible_for_later_admission, stale_source, reasons). Grants no calculation use."""
    if row["ingestion_status"] is None:
        return False, False, ["not_ingested"]
    stale = current_sha256 is None or row["source_sha256"] != current_sha256
    reasons: list[str] = []
    if row["ingestion_status"] != "source_checked":
        reasons.append("flagged")
    if stale:
        reasons.append("stale_source")
    if verdict not in {"usable", "usable_with_warnings"}:
        reasons.append("datum_check_not_usable" if verdict == "not_usable" else "datum_check_unchecked")
    return not reasons, stale, reasons


def flagged_entry_labels(connection, workspace_id: str) -> list[str]:
    """Readable labels of the workspace's ingested entries that still need review (flagged).

    Ingestion fields stay owned by the memory module; other modules ask through this read.
    """
    rows = connection.execute(
        "SELECT id, statement, value_text, quantity FROM literature_entries "
        "WHERE workspace_id = ? AND ingestion_status = 'flagged' ORDER BY id",
        (workspace_id,),
    ).fetchall()
    return [row["statement"] or row["value_text"] or row["quantity"] or row["id"] for row in rows]
