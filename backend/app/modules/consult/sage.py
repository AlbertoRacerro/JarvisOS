"""Sage local consultant integration (spec 381 §Scope 3).

Triggers:
1. local_only=true
2. Zone-A gate exceeds MAX_REWRITES (2 rewrites)
3. Zone A is unavailable (all trusted accounts failed/exhausted, or consult_mode == "off" with sage_model_id configured)

Unavailable fallback:
If sage_model_id is absent, unknown in local_models.json, or not host-startable, return sage_unavailable.

One batched swap per turn:
During a Hermes turn, Sage consults enqueue in a per-turn queue.
At the end of the Hermes turn, flush_sage_queue performs exactly one unload-Inkling -> load-Sage ->
answer questions -> unload-Sage -> reload-Inkling swap (with persist_choice=False), recording measured
timings (swap_to_sage_s, swap_back_s, total_swap_s) in gate_steps_json and emitting Italian status events.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.database import open_sqlite_connection
from app.modules.ai.execution import run_ai_task
from app.modules.consult.config import load_consult_config
from app.modules.events.service import log_event, utc_now

logger = logging.getLogger(__name__)

FAKE_SAGE_ENV = "JARVISOS_FAKE_SAGE_RESPONSE"
# Sage fakes act only together with the fake consult transport (offline tests and walks).
FAKE_TRANSPORT_ENV = "JARVISOS_CONSULT_FAKE_TRANSPORT"


def _fakes_enabled() -> bool:
    return os.environ.get(FAKE_TRANSPORT_ENV) == "1"


@dataclass
class SageQueueItem:
    consult_id: str
    turn_id: str | None
    workspace_id: str | None
    thread_id: str | None
    kind: str
    question: str
    context: str
    modules: list[str]


_QUEUE_LOCK = threading.Lock()
_SAGE_QUEUE: list[SageQueueItem] = []


def is_sage_available(sage_model_id: str | None = None) -> bool:
    """Check if the configured Sage model exists, is qualified/startable, and has model files."""
    if _fakes_enabled() and os.environ.get("JARVISOS_CONSULT_FAKE_SAGE") == "1":
        return True
    if sage_model_id is None:
        cfg = load_consult_config()
        sage_model_id = cfg.sage_model_id
    if not sage_model_id or not isinstance(sage_model_id, str):
        return False

    from app.modules.local_ai.runtime.model_registry import _host_startable, read_registry

    try:
        entries, present = read_registry()
    except Exception:
        return False
    if not present or not entries:
        return False

    entry = next((e for e in entries if e.id == sage_model_id), None)
    if entry is None or not _host_startable(entry):
        return False
    if entry.runtime == "llama_cpp" and (not entry.model_path or not Path(entry.model_path).is_file()):
        return False
    return True


def enqueue_sage_consult(item: SageQueueItem) -> None:
    with _QUEUE_LOCK:
        _SAGE_QUEUE.append(item)


def get_queued_sage_consults(turn_id: str | None = None) -> list[SageQueueItem]:
    with _QUEUE_LOCK:
        if turn_id is None:
            return list(_SAGE_QUEUE)
        return [item for item in _SAGE_QUEUE if item.turn_id == turn_id]


def clear_sage_queue(turn_id: str | None = None) -> None:
    with _QUEUE_LOCK:
        global _SAGE_QUEUE
        if turn_id is None:
            _SAGE_QUEUE.clear()
        else:
            _SAGE_QUEUE = [item for item in _SAGE_QUEUE if item.turn_id != turn_id]


def flush_sage_queue(
    turn_id: str | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Flush the Sage queue for the turn:

    1. Emit thread status event ('Sage sta ragionando in locale')
    2. Swap from active model to sage_model_id (persist_choice=False), measure swap_to_sage_s
    3. Answer queued questions sequentially via run_ai_task, store answers, update consult_attempts
    4. In finally:, restore original model (persist_choice=False), measure swap_back_s
    5. Update gate_steps_json with timings and emit completion event in Italian
    """
    with _QUEUE_LOCK:
        global _SAGE_QUEUE
        if turn_id is None:
            items_to_process = list(_SAGE_QUEUE)
            _SAGE_QUEUE.clear()
        else:
            items_to_process = [item for item in _SAGE_QUEUE if item.turn_id == turn_id]
            _SAGE_QUEUE = [item for item in _SAGE_QUEUE if item.turn_id != turn_id]

    if not items_to_process:
        return []

    cfg = load_consult_config()
    sage_model_id = cfg.sage_model_id

    from app.modules.local_ai.runtime.model_registry import (
        activate_model,
        get_active_model_entry,
        read_registry,
    )

    active = get_active_model_entry()
    original_model_id = active.id if active else None
    if not original_model_id:
        entries, _ = read_registry()
        default_entry = next((e for e in entries if e.qualification == "default"), None)
        original_model_id = default_entry.id if default_entry else "inkling"

    def _mark_failed(conn: sqlite3.Connection, consult_id: str, code: str, ai_job_id: str | None = None) -> None:
        row = conn.execute("SELECT gate_steps_json FROM consult_attempts WHERE id = ?", (consult_id,)).fetchone()
        try:
            steps = json.loads(row[0]) if row and row[0] else []
        except (TypeError, ValueError):
            steps = []
        steps.append({"step": "sage", "result": "refused", "reason": code})
        conn.execute(
            "UPDATE consult_attempts SET state = 'failed', ai_job_id = COALESCE(?, ai_job_id), gate_steps_json = ?, "
            "updated_at = ? WHERE id = ?",
            (ai_job_id, json.dumps(steps), utc_now(), consult_id),
        )
        conn.commit()

    def _do_flush(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        if not sage_model_id:
            for item in items_to_process:
                _mark_failed(conn, item.consult_id, "sage_unavailable")
            return []
        model_id: str = sage_model_id
        # 1. Emit thread status event
        first_item = items_to_process[0]
        try:
            log_event(
                conn,
                event_type="hermes.status",
                actor="sage",
                target_type="thread",
                target_id=first_item.thread_id,
                workspace_id=first_item.workspace_id,
                payload={"status": "Sage sta ragionando in locale", "interaction_id": turn_id},
            )
            conn.commit()
        except Exception as exc:
            logger.debug("Could not emit initial sage status event: %s", exc)

        conn.commit()

        # 2. Swap to Sage
        t0 = time.perf_counter()
        sage_active = True
        swap_to_sage_s = 0.0
        results: list[dict[str, Any]] = []
        try:
            try:
                activate_model(sage_model_id, persist_choice=False)
            except Exception as exc:
                # Never let the model still loaded answer in Sage's name.
                logger.warning("Could not activate Sage model %s: %s", sage_model_id, type(exc).__name__)
                sage_active = False
            swap_to_sage_s = round(max(0.001, time.perf_counter() - t0), 3)

            # 3. Answer each queued question
            for item in items_to_process:
                if not sage_active:
                    _mark_failed(conn, item.consult_id, "sage_unavailable")
                    continue
                parts = []
                if item.context.strip():
                    parts.append(f"Context:\n{item.context.strip()}")
                parts.append(f"Question:\n{item.question.strip()}")
                user_prompt = "\n\n".join(parts)

                adapters: dict[str, Any] | None = None
                fake_resp = os.environ.get(FAKE_SAGE_ENV) if _fakes_enabled() else None
                if fake_resp:
                    from app.modules.ai.contracts import AIResponse, AIUsage

                    class _FakeSageAdapter:
                        provider_id = "local_llamacpp"

                        def __init__(self, response_text: str) -> None:
                            self.response_text = response_text

                        def complete(self, request: Any) -> AIResponse:
                            return AIResponse(
                                provider_id="local_llamacpp",
                                model_id=model_id,
                                request_id=getattr(request, "request_id", "req-fake"),
                                text=self.response_text,
                                usage=AIUsage(
                                    input_tokens=100,
                                    output_tokens=50,
                                    total_tokens=150,
                                    provider_id="local_llamacpp",
                                    model_id=model_id,
                                ),
                            )

                    adapters = {"local_llamacpp": _FakeSageAdapter(fake_resp)}

                outcome = run_ai_task(
                    user_prompt=user_prompt,
                    task_kind="consult_sage",
                    route_class="local:llamacpp",
                    workspace_id=item.workspace_id,
                    adapters=adapters,
                )

                ai_job_id = outcome.ledger_id
                if outcome.status != "success" or outcome.response is None or not outcome.response.text:
                    _mark_failed(conn, item.consult_id, f"sage_error:{outcome.error_type or outcome.status}", ai_job_id)
                    continue
                answer_text = outcome.response.text

                now = utc_now()
                answer_id = f"answer-{uuid.uuid4().hex[:16]}"
                answer_sha = hashlib.sha256(answer_text.encode("utf-8")).hexdigest()

                conn.execute(
                    """
                    INSERT INTO consult_answers (id, attempt_id, answer_text, answer_sha256, model_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (answer_id, item.consult_id, answer_text, answer_sha, sage_model_id, now),
                )
                conn.execute(
                    """
                    UPDATE consult_attempts
                    SET state = 'sent', ai_job_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (ai_job_id, now, item.consult_id),
                )
                conn.commit()
                results.append({"consult_id": item.consult_id, "answer_id": answer_id, "ai_job_id": ai_job_id})
        finally:
            # 4. In finally: restore original model
            t_back = time.perf_counter()
            if original_model_id:
                try:
                    activate_model(original_model_id, persist_choice=False)
                except Exception as exc:
                    logger.warning("Could not restore original model %s: %s", original_model_id, exc)
            swap_back_s = round(max(0.001, time.perf_counter() - t_back), 3)
            total_swap_s = round(swap_to_sage_s + swap_back_s, 3)

        # 5. Store swap timings in gate_steps_json and emit completion event
        for item in items_to_process:
            row = conn.execute("SELECT gate_steps_json FROM consult_attempts WHERE id = ?", (item.consult_id,)).fetchone()
            current_steps = []
            if row and row[0]:
                try:
                    current_steps = json.loads(row[0])
                except Exception:
                    current_steps = []
            current_steps.append({
                "step": "sage_swap",
                "result": "pass",
                "swap_to_sage_s": swap_to_sage_s,
                "swap_back_s": swap_back_s,
                "total_swap_s": total_swap_s,
            })
            conn.execute(
                "UPDATE consult_attempts SET gate_steps_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(current_steps), utc_now(), item.consult_id),
            )

        try:
            log_event(
                conn,
                event_type="hermes.status",
                actor="sage",
                target_type="thread",
                target_id=first_item.thread_id,
                workspace_id=first_item.workspace_id,
                payload={
                    "status": f"Sage ha risposto in locale (tempo di swap: {total_swap_s:.2f}s)",
                    "interaction_id": turn_id,
                    "swap_to_sage_s": swap_to_sage_s,
                    "swap_back_s": swap_back_s,
                    "total_swap_s": total_swap_s,
                },
            )
            conn.commit()
        except Exception as exc:
            logger.debug("Could not emit completion sage status event: %s", exc)

        return results

    if connection is not None:
        return _do_flush(connection)
    with open_sqlite_connection() as conn:
        res = _do_flush(conn)
        conn.commit()
        return res
