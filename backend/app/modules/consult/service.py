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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

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
    reply_to: str | None = None
    turn_id: str | None = None
    thread_id: str | None = None
    workspace_id: str | None = None
    tool_outputs: tuple[str, ...] = field(default_factory=tuple)
    trigger_codes: list[str] = field(default_factory=list)


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


def _queue_for_sage(connection: sqlite3.Connection, request: ConsultRequest, config: ConsultConfig, *, mode: str,
                    context: str, packet: str | None, packet_sha: str, brief: Brief | None, steps: list[dict],
                    rewrite_count: int, ai_job_id: str | None = None) -> ConsultOutcome:
    """Record an approved local attempt for Sage and queue it for the end-of-turn swap."""
    from app.modules.consult.sage import SageQueueItem, enqueue_sage_consult

    consult_id = _record(connection, request, state="approved", code=None, mode=mode, packet=packet,
                         packet_sha=packet_sha, brief=brief, steps=steps, rewrite_count=rewrite_count, account=None,
                         approval="policy_auto", ai_job_id=ai_job_id)
    connection.execute(
        "UPDATE consult_attempts SET local_only = 1, account_id = 'sage_local', model_id = ? WHERE id = ?",
        (config.sage_model_id, consult_id),
    )
    enqueue_sage_consult(SageQueueItem(consult_id=consult_id, turn_id=request.turn_id,
                                       workspace_id=request.workspace_id, thread_id=request.thread_id,
                                       kind=request.kind, question=request.question, context=context,
                                       modules=list(request.modules)))
    return ConsultOutcome(
        status="queued_for_sage",
        consult_id=consult_id,
        reason=f"End your answer with {{{{consult:{consult_id}}}}}; Sage will answer at the end of the turn.",
    )


def request_consult(connection: sqlite3.Connection, request: ConsultRequest, *,
                    transport: ConsultTransport | None = None,
                    config_loader: Callable[[], ConsultConfig] = load_consult_config) -> ConsultOutcome:
    ended, streak = _turn_counts(connection, request.turn_id)
    config = config_loader()
    mode = config.mode

    fired_trigger_codes: list[str] = list(request.trigger_codes)
    if not fired_trigger_codes and request.turn_id:
        try:
            ev_rows = connection.execute(
                "SELECT payload FROM events WHERE event_type = 'hermes.consult_trigger' AND target_id = ? ORDER BY created_at, rowid",
                (request.turn_id,),
            ).fetchall()
            for r in ev_rows:
                payload = json.loads(r[0]) if r[0] else {}
                code = payload.get("trigger_code")
                if code and code not in fired_trigger_codes:
                    fired_trigger_codes.append(code)
        except Exception:
            pass

    trigger_steps: list[dict[str, Any]] = []
    if fired_trigger_codes:
        trigger_steps.append({"step": "triggers", "result": "pass", "trigger_codes": list(fired_trigger_codes)})

    def refuse(code: str, *, steps: list[dict] | None = None, packet: str | None = None, brief: Brief | None = None,
               account: TrustedAccount | None = None, status: str = "refused", ai_job_id: str | None = None) -> ConsultOutcome:
        raw = packet if packet is not None else f"{request.question}\n\n{request.context}"
        combined_steps = [*trigger_steps, *(steps or [])]
        consult_id = _record(connection, request, state="refused", code=code, mode=mode, packet=packet,
                             packet_sha=_sha(raw), brief=brief, steps=combined_steps, rewrite_count=streak,
                             account=account, ai_job_id=ai_job_id)
        return ConsultOutcome(status=status, code=code, consult_id=consult_id)

    if ended >= MAX_CONSULTS_PER_TURN:
        return refuse("loop_guard")
    error = _argument_error(request)
    if error:
        return refuse(error)

    # Bounded 2-round exchanges: check reply_to
    reply_steps: list[dict[str, Any]] = []
    effective_context = request.context
    if request.reply_to:
        target_row = connection.execute(
            "SELECT id, workspace_id, thread_id, state, gate_steps_json, packet_text FROM consult_attempts WHERE id = ?",
            (request.reply_to,),
        ).fetchone()
        if not target_row or target_row[3] != "sent":
            return refuse("reply_target_invalid")
        if request.workspace_id and target_row[1] and target_row[1] != request.workspace_id:
            return refuse("reply_target_invalid")
        if request.thread_id and target_row[2] and target_row[2] != request.thread_id:
            return refuse("reply_target_invalid")
        local_target = connection.execute(
            "SELECT local_only FROM consult_attempts WHERE id = ?", (request.reply_to,)).fetchone()
        if local_target and local_target[0] and not request.local_only:
            # A round 1 answered by Sage was written from local context: its text never goes to zone A.
            return refuse("reply_target_local_only")

        target_steps: list[Any] = []
        try:
            target_steps = json.loads(target_row[4]) if target_row[4] else []
        except Exception:
            target_steps = []
        target_round = 1
        for s in target_steps:
            if s.get("step") == "exchange" and (s.get("round", 1) >= 2 or s.get("reply_to")):
                target_round = 2
                break
        if target_round >= 2:
            return refuse("exchange_limit_reached")

        ans_row = connection.execute(
            "SELECT answer_text FROM consult_answers WHERE attempt_id = ?",
            (request.reply_to,),
        ).fetchone()
        ans_text = ans_row[0] if ans_row else ""
        round_1_summary = f"[Round 1 exchange: {request.reply_to}]\n{ans_text[:1000]}"
        effective_context = f"{round_1_summary}\n\n{request.context}".strip()
        reply_steps.append({"step": "exchange", "result": "pass", "round": 2, "reply_to": request.reply_to})

    from app.modules.consult.sage import is_sage_available

    if request.local_only:
        if not is_sage_available(config.sage_model_id):
            return refuse("sage_unavailable", status="sage_unavailable")
        return _queue_for_sage(connection, request, config, mode=mode, context=effective_context,
                                packet=None, packet_sha=_sha(f"{request.question}\n\n{effective_context}"), brief=None,
                                steps=[*trigger_steps, *reply_steps], rewrite_count=streak)

    if mode == "off":
        if config.sage_model_id and is_sage_available(config.sage_model_id):
            return _queue_for_sage(connection, request, config, mode=mode, context=effective_context,
                                    packet=None, packet_sha=_sha(f"{request.question}\n\n{effective_context}"), brief=None,
                                    steps=[*trigger_steps, *reply_steps], rewrite_count=streak)
        return refuse(config.reason or "consult_off")

    account = next(iter(config.trusted_accounts), None)
    if account is None:
        if config.sage_model_id and is_sage_available(config.sage_model_id):
            return _queue_for_sage(connection, request, config, mode=mode, context=effective_context,
                                    packet=None, packet_sha=_sha(f"{request.question}\n\n{effective_context}"), brief=None,
                                    steps=[*trigger_steps, *reply_steps], rewrite_count=streak)
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
    # The local judge writes its ai_jobs row on its own connection: never hold a write lock across it.
    if connection.in_transaction:
        connection.commit()
    gate = run_gate(request.question, effective_context, sources)
    steps = [*trigger_steps, *reply_steps, *_steps_json_entries(gate.steps)]
    if gate.literature_numbers or gate.constant_numbers:
        steps.append({"step": "logged_matches", "result": "pass",
                      "reason": json.dumps({"literature": gate.literature_numbers, "constants": gate.constant_numbers})})
    judge_job_id = next((s.get("ai_job_id") for s in steps if s.get("ai_job_id")), None)
    if gate.status == "refused":
        # An S4 or identity hit stores the hash and the reason only, never the text.
        return refuse(gate.code or "refused", steps=steps, brief=brief, account=account, ai_job_id=judge_job_id)
    packet = build_packet(brief, request.kind, request.modules, request.question, effective_context)
    if gate.status == "rewrite_required":
        if streak >= MAX_REWRITES:
            if config.sage_model_id and is_sage_available(config.sage_model_id):
                return _queue_for_sage(connection, request, config, mode=mode, context=effective_context,
                                        packet=packet, packet_sha=_sha(packet), brief=brief,
                                        steps=steps, rewrite_count=streak, ai_job_id=judge_job_id)
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
    return _send(connection, consult_id, packet, config.trusted_accounts, transport, config=config, request=request)


def _gate_sources(connection: sqlite3.Connection, tool_outputs: tuple[str, ...]) -> GateSources:
    return GateSources(tool_outputs=tool_outputs, stored_numbers=tuple(stored_project_numbers(connection)),
                       protected_values=tuple(protected_values()), vault_dir=_vault_dir(),
                       data_root=_data_root(), pack_roots=tuple(pack.root for pack in packs.load_packs()))


def _data_root() -> Path:
    return build_paths().data_root


def _vault_dir() -> Path | None:
    value = os.environ.get(vault.VAULT_PATH_ENV, "").strip()
    return Path(value) if value else None


def _send(
    connection: sqlite3.Connection,
    consult_id: str,
    packet: str,
    account_or_accounts: TrustedAccount | Iterable[TrustedAccount],
    transport: ConsultTransport,
    *,
    config: ConsultConfig | None = None,
    turn_id: str | None = None,
    request: ConsultRequest | None = None,
) -> ConsultOutcome:
    if isinstance(account_or_accounts, TrustedAccount):
        account_list = [account_or_accounts]
    else:
        account_list = list(account_or_accounts)

    tried_steps: list[dict[str, Any]] = []
    success_account: TrustedAccount | None = None
    last_answer: str | None = None

    for account in account_list:
        try:
            try:
                answer = transport.send(packet, account, consult_id=consult_id)
            except TypeError:
                answer = transport.send(packet, account)
            success_account = account
            last_answer = answer
            break
        except Exception as exc:  # Transport/quota error
            tried_steps.append({
                "step": "transport_attempt",
                "result": "refused",
                "account_id": account.id,
                "reason": f"transport_failed:{type(exc).__name__}",
            })

    if tried_steps:
        row = connection.execute("SELECT gate_steps_json FROM consult_attempts WHERE id = ?", (consult_id,)).fetchone()
        cur_steps = []
        if row and row[0]:
            try:
                cur_steps = json.loads(row[0])
            except Exception:
                cur_steps = []
        cur_steps.extend(tried_steps)
        connection.execute(
            "UPDATE consult_attempts SET gate_steps_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(cur_steps), utc_now(), consult_id),
        )

    if success_account is not None and last_answer is not None:
        answer_id = f"answer-{uuid.uuid4().hex[:16]}"
        connection.execute(
            "INSERT INTO consult_answers (id, attempt_id, answer_text, answer_sha256, model_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (answer_id, consult_id, last_answer, _sha(last_answer), success_account.model, utc_now()),
        )
        connection.execute(
            "UPDATE consult_attempts SET state = 'sent', account_id = ?, model_id = ?, updated_at = ? WHERE id = ?",
            (success_account.id, success_account.model, utc_now(), consult_id),
        )
        connection.commit()
        return ConsultOutcome(status="answered", consult_id=consult_id, answer=last_answer, answer_id=answer_id)

    # All accounts failed! Fall back to Sage if configured and available
    cfg = config or load_consult_config()
    from app.modules.consult.sage import SageQueueItem, enqueue_sage_consult, is_sage_available

    # Sage answers only inside a turn, where the end-of-turn flush runs; an approval from the UI has no turn.
    if request is not None and request.turn_id and cfg.sage_model_id and is_sage_available(cfg.sage_model_id):
        connection.execute(
            "UPDATE consult_attempts SET local_only = 1, account_id = 'sage_local', model_id = ?, state = 'approved', approval = 'policy_auto', updated_at = ? WHERE id = ?",
            (cfg.sage_model_id, utc_now(), consult_id),
        )
        att_row = connection.execute(
            "SELECT kind, packet_text, workspace_id, thread_id, turn_id, modules_json FROM consult_attempts WHERE id = ?",
            (consult_id,),
        ).fetchone()
        q = request.question if request else (att_row[1] if att_row and att_row[1] else "")
        ctx = request.context if request else ""
        mods = list(request.modules) if request else (json.loads(att_row[5] or "[]") if att_row else [])
        ws = request.workspace_id if request else (att_row[2] if att_row else None)
        th = request.thread_id if request else (att_row[3] if att_row else None)
        t_id = turn_id or (request.turn_id if request else (att_row[4] if att_row else None))

        enqueue_sage_consult(
            SageQueueItem(
                consult_id=consult_id,
                turn_id=t_id,
                workspace_id=ws,
                thread_id=th,
                kind=att_row[0] if att_row else "general",
                question=q,
                context=ctx,
                modules=mods,
            )
        )
        return ConsultOutcome(
            status="queued_for_sage",
            consult_id=consult_id,
            reason=f"End your answer with {{{{consult:{consult_id}}}}}; Sage will answer at the end of the turn.",
        )

    # Sage not configured: mark attempt failed with zone_a_unavailable
    _set_state(connection, consult_id, "failed")
    connection.execute(
        "UPDATE consult_attempts SET result_code = 'zone_a_unavailable', updated_at = ? WHERE id = ?",
        (utc_now(), consult_id),
    )
    return ConsultOutcome(status="refused", code="zone_a_unavailable", consult_id=consult_id)


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
    return _send(connection, consult_id, packet, config.trusted_accounts, transport, config=config)


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
    # The local judge writes its ai_jobs row on its own connection: never hold a write lock across it.
    if connection.in_transaction:
        connection.commit()
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
