"""Consult attempts: argument checks, loop guard, rewrite cap, mode, ledger and approval (spec 380, part a).

Code never writes consult text. It assembles the approved brief, the fixed template for ``kind`` and Inkling's
question and context, runs the deterministic gate and records every attempt in ``consult_attempts``. Sending goes
through a :class:`ConsultTransport`; until the Relay consult session exists (part b) there is none, and any mode other
than ``off`` refuses with ``transport_unavailable``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.core import packs, vault
from app.core.paths import build_paths
from app.modules.consult.brief import Brief, BriefUnavailable, approved_brief
from app.modules.consult.config import ConsultConfig, TrustedAccount, load_consult_config
from app.modules.consult.gate import GateSources, run_gate, source_numbers
from app.modules.consult.templates import TEMPLATES
from app.modules.events.service import utc_now

MAX_QUESTION = 1500
MAX_CONTEXT = 12000
MAX_MODULES = 2
MAX_CONSULTS_PER_TURN = 3
MAX_REWRITES = 2
READ_CHUNK_CHARS = 6000
AUDIT_ONE_IN = 5

# Stored project numbers (spec 380 code facts). Identifier, digest and timestamp columns are skipped; everything else
# is scanned, so a new numeric column is compared by default.
STORED_NUMBER_TABLES = ("parameters", "simulation_result_values", "simulation_run_scalar_results",
                        "design_component_geometry", "design_component_units", "studio_procedure_runs",
                        "bluecad_candidates")
_SKIP_SUFFIXES = ("_id", "_at", "sha256", "digest", "_hash")


class ConsultTransport(Protocol):
    def send(self, packet: str, account: TrustedAccount, *, consult_id: str | None = None) -> str: ...


@dataclass(frozen=True)
class ConsultOutcome:
    status: str  # refused | rewrite_required | sage_unavailable | pending | answered
    code: str | None = None
    consult_id: str | None = None
    reason: str | None = None
    offending: tuple[str, ...] = ()
    answer: str | None = None
    answer_id: str | None = None


@dataclass
class ConsultRequest:
    kind: str
    question: str
    modules: list[str]
    context: str = ""
    local_only: bool = False
    turn_id: str | None = None
    thread_id: str | None = None
    workspace_id: str | None = None
    tool_outputs: tuple[str, ...] = field(default_factory=tuple)


def stored_project_numbers(connection: sqlite3.Connection) -> list[float]:
    numbers: list[float] = []
    for table in STORED_NUMBER_TABLES:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        wanted = [c for c in columns if c != "id" and not c.endswith(_SKIP_SUFFIXES)]
        if not wanted:
            continue
        select = ", ".join(f'"{c}"' for c in wanted)
        for row in connection.execute(f'SELECT {select} FROM "{table}"'):
            numbers.extend(source_numbers(str(value) for value in row if value is not None))
    return numbers


def protected_values() -> list[float]:
    values = [d.value for pack in packs.load_packs() for d in pack.search_guard.protected_dimensions]
    values.extend(d.value for d in vault.vault_protected_dimensions())
    return values


def build_packet(brief: Brief, kind: str, modules: list[str], question: str, context: str) -> str:
    parts = [brief.packet_part(modules), TEMPLATES[kind], "Question:\n" + question]
    if context.strip():
        parts.append("Context:\n" + context)
    return "\n\n".join(parts)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _steps_json_entries(gate_steps: tuple[Any, ...]) -> list[dict[str, Any]]:
    steps = []
    for s in gate_steps:
        entry: dict[str, Any] = {"step": s.step, "result": s.result, "reason": s.reason}
        if getattr(s, "hits", ()):
            entry["hits"] = list(s.hits)
        if getattr(s, "ai_job_id", None):
            entry["ai_job_id"] = s.ai_job_id
        steps.append(entry)
    return steps


def _record(connection: sqlite3.Connection, request: ConsultRequest, *, state: str, code: str | None, mode: str,
            packet: str | None, packet_sha: str, brief: Brief | None, steps: list[dict], rewrite_count: int,
            account: TrustedAccount | None, approval: str | None = None, audit: bool = False,
            ai_job_id: str | None = None) -> str:
    consult_id = f"consult-{uuid.uuid4().hex[:16]}"
    now = utc_now()
    connection.execute(
        """
        INSERT INTO consult_attempts (id, workspace_id, thread_id, turn_id, kind, local_only, packet_text,
            packet_sha256, brief_sha256, modules_json, gate_steps_json, result_code, rewrite_count, mode, account_id,
            model_id, approval, audit, state, ai_job_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (consult_id, request.workspace_id, request.thread_id, request.turn_id, request.kind, int(request.local_only),
         packet, packet_sha, brief.sha256 if brief else None, json.dumps(list(request.modules)), json.dumps(steps),
         code, rewrite_count, mode, account.id if account else None, account.model if account else None, approval,
         int(audit), state, ai_job_id, now, now),
    )
    return consult_id


def _turn_counts(connection: sqlite3.Connection, turn_id: str | None) -> tuple[int, int]:
    """(consults ended this turn, rewrites in the current streak) for the loop guard and the rewrite cap."""
    if turn_id is None:
        return 0, 0
    states = [row[0] for row in connection.execute(
        "SELECT state FROM consult_attempts WHERE turn_id = ? ORDER BY created_at, rowid", (turn_id,))]
    ended = sum(1 for state in states if state != "rewrite_required")
    streak = 0
    for state in reversed(states):
        if state != "rewrite_required":
            break
        streak += 1
    return ended, streak


def _argument_error(request: ConsultRequest) -> str | None:
    if request.kind not in TEMPLATES:
        return "kind_unknown"
    if not isinstance(request.question, str) or not request.question.strip() or len(request.question) > MAX_QUESTION:
        return "question_invalid"
    if not isinstance(request.context, str) or len(request.context) > MAX_CONTEXT:
        return "context_too_long"
    if not isinstance(request.modules, list) or len(request.modules) > MAX_MODULES or \
            len(set(request.modules)) != len(request.modules):
        return "modules_invalid"
    return None


def request_consult(connection: sqlite3.Connection, request: ConsultRequest, *,
                    transport: ConsultTransport | None = None,
                    config_loader: Callable[[], ConsultConfig] = load_consult_config) -> ConsultOutcome:
    ended, streak = _turn_counts(connection, request.turn_id)
    config = config_loader()
    mode = config.mode

    def refuse(code: str, *, steps: list[dict] | None = None, packet: str | None = None, brief: Brief | None = None,
               account: TrustedAccount | None = None, status: str = "refused", ai_job_id: str | None = None) -> ConsultOutcome:
        raw = packet if packet is not None else f"{request.question}\n\n{request.context}"
        consult_id = _record(connection, request, state="refused", code=code, mode=mode, packet=packet,
                             packet_sha=_sha(raw), brief=brief, steps=steps or [], rewrite_count=streak,
                             account=account, ai_job_id=ai_job_id)
        return ConsultOutcome(status=status, code=code, consult_id=consult_id)

    if ended >= MAX_CONSULTS_PER_TURN:
        return refuse("loop_guard")
    error = _argument_error(request)
    if error:
        return refuse(error)
    if request.local_only:
        return refuse("sage_unavailable", status="sage_unavailable")
    if mode == "off":
        return refuse(config.reason or "consult_off")
    account = next(iter(config.trusted_accounts), None)
    if account is None:
        return refuse("no_trusted_account")
    try:
        brief = approved_brief(config.brief_sha256)
    except BriefUnavailable as exc:
        return refuse(exc.code)
    if any(module not in brief.modules for module in request.modules):
        return refuse("module_unknown", brief=brief, account=account)
    if transport is None:
        return refuse("transport_unavailable", brief=brief, account=account)

    if _vault_dir() is None and config_loader is load_consult_config:
        return refuse("vault_absent", brief=brief, account=account)
    try:
        sources = _gate_sources(connection, request.tool_outputs)
    except Exception:  # noqa: BLE001 - an unreadable source (vault, pack, table) refuses, never passes
        return refuse("gate_error", brief=brief, account=account)
    gate = run_gate(request.question, request.context, sources)
    steps = _steps_json_entries(gate.steps)
    if gate.literature_numbers or gate.constant_numbers:
        steps.append({"step": "logged_matches", "result": "pass",
                      "reason": json.dumps({"literature": gate.literature_numbers, "constants": gate.constant_numbers})})
    judge_job_id = next((s.get("ai_job_id") for s in steps if s.get("ai_job_id")), None)
    if gate.status == "refused":
        # An S4 or identity hit stores the hash and the reason only, never the text.
        return refuse(gate.code or "refused", steps=steps, brief=brief, account=account, ai_job_id=judge_job_id)
    packet = build_packet(brief, request.kind, request.modules, request.question, request.context)
    if gate.status == "rewrite_required":
        if streak >= MAX_REWRITES:
            return refuse("sage_unavailable", steps=steps, packet=packet, brief=brief, account=account,
                          status="sage_unavailable", ai_job_id=judge_job_id)
        consult_id = _record(connection, request, state="rewrite_required", code=gate.code, mode=mode, packet=packet,
                             packet_sha=_sha(packet), brief=brief, steps=steps, rewrite_count=streak, account=account,
                             ai_job_id=judge_job_id)
        return ConsultOutcome(status="rewrite_required", code=gate.code, consult_id=consult_id,
                              reason=gate.steps[-1].reason, offending=gate.offending)
    packet_sha = _sha(packet)
    if mode == "ask":
        consult_id = _record(connection, request, state="awaiting_approval", code=None, mode=mode, packet=packet,
                             packet_sha=packet_sha, brief=brief, steps=steps, rewrite_count=streak, account=account,
                             ai_job_id=judge_job_id)
        return ConsultOutcome(status="pending", consult_id=consult_id,
                              reason=f"End your answer with {{{{consult:{consult_id}}}}}; the maintainer approves it.")
    audit = int(packet_sha[:8], 16) % AUDIT_ONE_IN == 0
    consult_id = _record(connection, request, state="approved", code=None, mode=mode, packet=packet,
                         packet_sha=packet_sha, brief=brief, steps=steps, rewrite_count=streak, account=account,
                         approval="policy_auto", audit=audit, ai_job_id=judge_job_id)
    connection.commit()
    return _send(connection, consult_id, packet, account, transport)


def _gate_sources(connection: sqlite3.Connection, tool_outputs: tuple[str, ...]) -> GateSources:
    return GateSources(tool_outputs=tool_outputs, stored_numbers=tuple(stored_project_numbers(connection)),
                       protected_values=tuple(protected_values()), vault_dir=_vault_dir(),
                       data_root=_data_root(), pack_roots=tuple(pack.root for pack in packs.load_packs()))


def _data_root() -> Path:
    return build_paths().data_root


def _vault_dir() -> Path | None:
    value = os.environ.get(vault.VAULT_PATH_ENV, "").strip()
    return Path(value) if value else None


def _send(connection: sqlite3.Connection, consult_id: str, packet: str, account: TrustedAccount,
          transport: ConsultTransport) -> ConsultOutcome:
    try:
        try:
            answer = transport.send(packet, account, consult_id=consult_id)
        except TypeError:
            answer = transport.send(packet, account)
    except Exception as exc:  # noqa: BLE001 - a transport failure is recorded, never retried silently
        _set_state(connection, consult_id, "failed")
        return ConsultOutcome(status="refused", code=f"transport_failed:{type(exc).__name__}", consult_id=consult_id)
    answer_id = f"answer-{uuid.uuid4().hex[:16]}"
    connection.execute(
        "INSERT INTO consult_answers (id, attempt_id, answer_text, answer_sha256, model_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (answer_id, consult_id, answer, _sha(answer), account.model, utc_now()),
    )
    _set_state(connection, consult_id, "sent")
    return ConsultOutcome(status="answered", consult_id=consult_id, answer=answer, answer_id=answer_id)


def _set_state(connection: sqlite3.Connection, consult_id: str, state: str, approval: str | None = None) -> None:
    connection.execute(
        "UPDATE consult_attempts SET state = ?, approval = COALESCE(?, approval), updated_at = ? WHERE id = ?",
        (state, approval, utc_now(), consult_id),
    )


def approve_consult(connection: sqlite3.Connection, consult_id: str, packet_sha256: str, *,
                    transport: ConsultTransport | None,
                    config_loader: Callable[[], ConsultConfig] = load_consult_config) -> ConsultOutcome:
    """The maintainer approves the exact packet he saw; a digest mismatch or a replay refuses."""
    row = connection.execute(
        "SELECT state, packet_text, packet_sha256, account_id, brief_sha256 FROM consult_attempts WHERE id = ?",
        (consult_id,),
    ).fetchone()
    if row is None:
        return ConsultOutcome(status="refused", code="consult_unknown")
    state, packet, stored_sha, account_id, stored_brief_sha = row[0], row[1], row[2], row[3], row[4]
    if state != "awaiting_approval":
        return ConsultOutcome(status="refused", code="not_awaiting_approval", consult_id=consult_id)
    if packet is None or packet_sha256 != stored_sha or _sha(packet) != stored_sha:
        return ConsultOutcome(status="refused", code="digest_mismatch", consult_id=consult_id)
    config = config_loader()
    account = next((a for a in config.trusted_accounts if a.id == account_id), None)
    if config.mode not in ("ask", "auto") or account is None:
        return ConsultOutcome(status="refused", code=config.reason or "consult_off", consult_id=consult_id)
    try:
        brief = approved_brief(config.brief_sha256)
    except BriefUnavailable as exc:
        return ConsultOutcome(status="refused", code=exc.code, consult_id=consult_id)
    if stored_brief_sha is not None and brief.sha256 != stored_brief_sha:
        return ConsultOutcome(status="refused", code="brief_not_approved", consult_id=consult_id)
    if transport is None:
        return ConsultOutcome(status="refused", code="transport_unavailable", consult_id=consult_id)
    _set_state(connection, consult_id, "approved", approval="operator")
    connection.commit()
    return _send(connection, consult_id, packet, account, transport)


def edit_consult(connection: sqlite3.Connection, consult_id: str, question: str, context: str = "", *,
                 config_loader: Callable[[], ConsultConfig] = load_consult_config) -> ConsultOutcome:
    """The maintainer edits a pending packet: the original is rejected and the edited text is a new attempt.

    The edit re-runs gate steps 1-3 (stored and protected numbers; the turn's tool outputs are gone by then). Ledger
    rows stay append-only: the edited packet never overwrites the one Inkling wrote.
    """
    row = connection.execute(
        "SELECT state, kind, modules_json, workspace_id, thread_id, turn_id FROM consult_attempts WHERE id = ?",
        (consult_id,),
    ).fetchone()
    if row is None:
        return ConsultOutcome(status="refused", code="consult_unknown")
    if row[0] != "awaiting_approval":
        return ConsultOutcome(status="refused", code="not_awaiting_approval", consult_id=consult_id)
    request = ConsultRequest(kind=row[1], question=question, modules=list(json.loads(row[2])), context=context,
                             workspace_id=row[3], thread_id=row[4], turn_id=row[5])
    config = config_loader()
    mode = config.mode
    error = _argument_error(request)
    account = next(iter(config.trusted_accounts), None)
    if error or mode == "off" or account is None:
        return ConsultOutcome(status="refused", code=error or config.reason or "consult_off", consult_id=consult_id)
    try:
        brief = approved_brief(config.brief_sha256)
    except BriefUnavailable as exc:
        return ConsultOutcome(status="refused", code=exc.code, consult_id=consult_id)
    if any(module not in brief.modules for module in request.modules):
        return ConsultOutcome(status="refused", code="module_unknown", consult_id=consult_id)
    if _vault_dir() is None and config_loader is load_consult_config:
        return ConsultOutcome(status="refused", code="vault_absent", consult_id=consult_id)
    try:
        sources = _gate_sources(connection, ())
    except Exception:  # noqa: BLE001 - an unreadable source refuses, never passes
        return ConsultOutcome(status="refused", code="gate_error", consult_id=consult_id)
    gate = run_gate(question, context, sources)
    steps = _steps_json_entries(gate.steps)
    judge_job_id = next((s.get("ai_job_id") for s in steps if s.get("ai_job_id")), None)
    if gate.status != "pass":
        # The maintainer's edit failed the gate: keep the original pending, record the edit attempt without its text
        # when it was an S4/identity refusal.
        packet = build_packet(brief, request.kind, request.modules, question, context)
        keep = packet if gate.status == "rewrite_required" else None
        new_id = _record(connection, request, state="refused", code=gate.code, mode=mode, packet=keep,
                         packet_sha=_sha(packet), brief=brief, steps=steps, rewrite_count=0, account=account,
                         approval="operator", ai_job_id=judge_job_id)
        return ConsultOutcome(status="refused" if gate.status == "refused" else "rewrite_required", code=gate.code,
                              consult_id=new_id, reason=gate.steps[-1].reason, offending=gate.offending)
    packet = build_packet(brief, request.kind, request.modules, question, context)
    _set_state(connection, consult_id, "rejected", approval="operator")
    new_id = _record(connection, request, state="awaiting_approval", code="edited_from:" + consult_id, mode=mode,
                     packet=packet, packet_sha=_sha(packet), brief=brief, steps=steps, rewrite_count=0,
                     account=account, ai_job_id=judge_job_id)
    return ConsultOutcome(status="pending", consult_id=new_id)


def reject_consult(connection: sqlite3.Connection, consult_id: str) -> ConsultOutcome:
    cursor = connection.execute(
        "UPDATE consult_attempts SET state = 'rejected', approval = 'operator', updated_at = ? "
        "WHERE id = ? AND state = 'awaiting_approval'", (utc_now(), consult_id))
    if cursor.rowcount != 1:
        return ConsultOutcome(status="refused", code="not_awaiting_approval", consult_id=consult_id)
    return ConsultOutcome(status="rejected", consult_id=consult_id)


def read_answer(connection: sqlite3.Connection, consult_id: str, offset: int = 0) -> dict:
    row = connection.execute(
        "SELECT a.answer_text, a.answer_sha256, a.model_id FROM consult_answers a WHERE a.attempt_id = ?",
        (consult_id,)).fetchone()
    if row is None:
        return {"status": "not_found", "consult_id": consult_id}
    text = row[0]
    offset = max(0, int(offset))
    chunk = text[offset:offset + READ_CHUNK_CHARS]
    next_offset = offset + len(chunk)
    return {"status": "ok", "consult_id": consult_id, "untrusted": True, "model": row[2], "answer_sha256": row[1],
            "text": chunk, "next_offset": next_offset if next_offset < len(text) else None}
