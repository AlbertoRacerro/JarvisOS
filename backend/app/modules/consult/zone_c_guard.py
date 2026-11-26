"""Unified Zone-C Egress Guard (spec 382).

Evaluates outbound search queries, paper searches, and public web page fetches through
three fail-closed stages:
1. Deterministic search guard (pack terms, exact combinations, protected dimensions, min_groups,
   sanitization diff, sensitivity floor S2-S4).
2. Fresh-context local inference judge via run_ai_task on local route (writing an ai_jobs row).
3. Mosaic guard:
   - Rolling 30-day concept group counter over persisted allowed queries in zone_c_query_history.
   - Contextual mosaic judge over up to last 20 allowed queries sharing concept groups.

Fail-closed at every stage: missing/broken vault when configured, judge errors/malformed output,
and database write errors all block outbound egress.
Secret-free refusal messages provide actionable guidance without echoing sensitive terms.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core import packs, vault
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.contracts import AIRequest, AIResponse, AIUsage
from app.modules.ai.execution import AiTaskOutcome, run_ai_task
from app.modules.ai.execution_types import ProviderBinding
from app.modules.consult.config import load_consult_config, read_consult_config

logger = logging.getLogger(__name__)

JUDGE_ROUTE_CLASS = "local:llamacpp"
FAKE_TRANSPORT_ENV = "JARVISOS_CONSULT_FAKE_TRANSPORT"
FAKE_JUDGE_ENV = "JARVISOS_CONSULT_FAKE_JUDGE"
FAKE_ZONE_C_INFERENCE_ENV = "JARVISOS_ZONE_C_FAKE_INFERENCE_JUDGE"
FAKE_ZONE_C_MOSAIC_ENV = "JARVISOS_ZONE_C_FAKE_MOSAIC_JUDGE"

REFUSAL_MESSAGES: dict[str, str] = {
    "pack_term": "Request blocked: mentions protected project terms. Rephrase using generic industry terminology.",
    "exact_combination": "Request blocked: mentions a protected combination of terms. Rephrase without co-locating these concepts.",
    "protected_dimension": "Request blocked: mentions protected engineering dimensions. Rephrase using generic ranges or abstract concepts.",
    "guard_groups": "Request blocked: combines multiple protected concept groups. Rephrase more narrowly or generically.",
    "sanitized_diff": "Request blocked: detected potential secret or sensitive token. Remove specific IDs, paths, or credentials.",
    "sensitivity_floor": "Request blocked: content sensitivity exceeds public egress threshold (S2-S4).",
    "invalid_query": "Request blocked: invalid query format.",
    "pack_load_error": "Request blocked: pack configuration error.",
    "vault_load_error": "Request blocked: private vault configuration error.",
    "guard_error": "Request blocked: internal guard error.",
    "zone_c_inference_judge": "Request blocked: local inference judge detected risk of leaking protected project knowledge or local-only items. Rephrase generically without implying proprietary design details.",
    "mosaic_group_threshold": "Request blocked: 30-day concept group threshold reached. Queries over time touch too many protected concept groups. Wait for the rolling window to clear or broaden your search.",
    "zone_c_mosaic_judge": "Request blocked: cross-query mosaic analysis detected risk of inferring protected project knowledge when combined with recent searches. Rephrase to avoid correlating these topics.",
    "zone_c_judge_error": "Request blocked: local security judge unavailable or returned an error. Outbound query blocked by fail-closed policy.",
}


@dataclass(frozen=True)
class ZoneCDecision:
    allowed: bool
    reason_code: str
    refusal_message: str | None = None
    rule_index: int | None = None
    ai_job_id: str | None = None
    matched_groups: tuple[str, ...] = ()
    query_sha256: str = ""

    def __iter__(self):
        return iter((self.allowed, self.reason_code, self.rule_index))


class _FakeJudgeAdapter:
    def __init__(self, raw: str) -> None:
        self.raw = raw

    def complete(self, request: AIRequest) -> AIResponse:
        if self.raw == "provider_error":
            raise RuntimeError("judge_provider_failed")
        return AIResponse(
            provider_id="local_judge_fake",
            model_id="inkling-fake",
            request_id=request.request_id,
            text=self.raw,
            usage=AIUsage(
                provider_id="local_judge_fake",
                model_id="inkling-fake",
                input_tokens=50,
                output_tokens=20,
            ),
        )


def _strip_single_json_fence(raw: str) -> str:
    """Strip a single wrapping markdown ```json ... ``` fence if present, otherwise return raw."""
    stripped = raw.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].strip() == "```":
            inner = "\n".join(lines[1:-1]).strip()
            if inner.startswith("{") and inner.endswith("}"):
                return inner
    return stripped


def _evaluate_stage1(text: str, target_url: str | None) -> tuple[bool, str, int | None]:
    """Stage 1: deterministic search guard check via scripts/search_guard.py."""
    try:
        repo_root = Path(__file__).resolve().parents[4]
        if str(repo_root) not in sys.path and (repo_root / "scripts").is_dir():
            sys.path.insert(0, str(repo_root))
        from scripts.search_guard import evaluate_query
    except Exception as exc:
        logger.warning("Zone-C Stage 1 search_guard import failed: %s", exc)
        return False, "guard_error", None

    eval_targets: list[str] = []
    if target_url:
        eval_targets.append(target_url)
        # Unquote up to 3 times to inspect URL-encoded variants
        decoded = target_url
        for _ in range(3):
            next_dec = urllib.parse.unquote(decoded)
            if next_dec == decoded:
                break
            eval_targets.append(next_dec)
            decoded = next_dec

    if text and text not in eval_targets:
        eval_targets.append(text)

    if target_url and text and text != target_url:
        eval_targets.append(f"{text} {target_url}")

    if not eval_targets:
        return False, "invalid_query", None

    for candidate in eval_targets:
        candidate_norm = " ".join(candidate.split())
        if not candidate_norm:
            continue
        allowed, reason_code, rule_index = evaluate_query(candidate_norm)
        if not allowed:
            return False, reason_code, rule_index

    return True, "ok", None


def _call_local_judge(
    prompt: str,
    *,
    metadata: dict[str, str],
    runner: Callable[..., AiTaskOutcome],
    route_class: str = JUDGE_ROUTE_CLASS,
    fake_env_var: str | None = None,
) -> tuple[bool, list[str], str | None, str | None, bool]:
    """Execute fresh-context local judge via run_ai_task.
    
    Returns (success, hits, reason, ai_job_id, is_uncertain).
    If success is False, reason indicates error.
    """
    adapters: dict[str, Any] | None = None
    bindings: dict[str, Any] | None = None

    fake_response = None
    if os.environ.get(FAKE_TRANSPORT_ENV) == "1":
        if fake_env_var:
            fake_response = os.environ.get(fake_env_var)
        if fake_response is None:
            fake_response = os.environ.get(FAKE_JUDGE_ENV)
        if fake_response is None:
            fake_response = json.dumps({"hits": [], "reason": "clean"})

    if fake_response is not None:
        adapter = _FakeJudgeAdapter(fake_response)
        adapters = {"local_judge_fake": adapter}
        bindings = {
            route_class: ProviderBinding(
                route_class=route_class,
                provider_id="local_judge_fake",
                model_id="inkling-fake",
                requires_network=False,
                max_output_tokens=512,
                execution_class="local_compute",
            )
        }

    try:
        outcome = runner(
            user_prompt=prompt,
            task_kind="synthesis",
            route_class=route_class,
            max_output_tokens=512,
            adapters=adapters,
            bindings=bindings,
            metadata=metadata,
        )
    except Exception as exc:
        logger.warning("Zone-C local judge call failed: %s", exc)
        return False, [], f"judge_error:{type(exc).__name__}", None, False

    if outcome.status != "success" or outcome.response is None or outcome.response.text is None:
        return False, [], "judge_error", outcome.ledger_id, False

    raw_text = _strip_single_json_fence(outcome.response.text)
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        return False, [], "judge_error", outcome.ledger_id, False

    if not isinstance(parsed, dict) or "hits" not in parsed:
        return False, [], "judge_error", outcome.ledger_id, False

    parsed_hits = parsed.get("hits")
    parsed_reason = parsed.get("reason")
    if not isinstance(parsed_hits, list):
        return False, [], "judge_error", outcome.ledger_id, False
    if not all(isinstance(h, str) and h.strip() for h in parsed_hits):
        return False, [], "judge_error", outcome.ledger_id, False

    raw_uncertain = parsed.get("uncertain", False)
    if not isinstance(raw_uncertain, bool):
        return False, [], "judge_error", outcome.ledger_id, False
    is_uncertain = raw_uncertain
    cleaned_hits = [h.strip() for h in parsed_hits]

    return True, cleaned_hits, str(parsed_reason) if parsed_reason else None, outcome.ledger_id, is_uncertain


def _compute_matched_groups(text: str, loaded_packs: tuple[packs.Pack, ...] | list[packs.Pack]) -> tuple[str, ...]:
    """Compute pack SearchGuardConfig.groups identifiers touched by text, e.g. ('zephyr:0',)."""
    lowered = " ".join(text.lower().split())
    matched: list[str] = []
    for pack in loaded_packs:
        guard_cfg = pack.search_guard
        if not guard_cfg.groups:
            continue
        for idx, group in enumerate(guard_cfg.groups):
            if any(" ".join(term.lower().split()) in lowered for term in group if term.strip()):
                matched.append(f"{pack.id}:{idx}")
    return tuple(matched)


def _ensure_db_initialized(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_settings'"
        ).fetchone()
        if row is None:
            initialize_database()
        row_z = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='zone_c_query_history'"
        ).fetchone()
        if row_z is None:
            _ensure_table(conn)
    except Exception as exc:
        logger.warning("Zone-C DB initialization failed: %s", exc)


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS zone_c_query_history (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            query_text TEXT NOT NULL,
            query_sha256 TEXT NOT NULL,
            matched_groups_json TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('allowed', 'blocked')),
            reason_code TEXT NOT NULL,
            ai_job_id TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_zone_c_query_history_created ON zone_c_query_history(created_at)"
    )


def _record_query_history(
    conn: sqlite3.Connection,
    *,
    kind: str,
    query_text: str,
    query_sha256: str,
    matched_groups: tuple[str, ...] | list[str],
    decision: str,
    reason_code: str,
    ai_job_id: str | None,
    created_at: str | None = None,
) -> bool:
    """Insert query history entry. Returns True on success, False on failure."""
    if created_at is None:
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    record_id = f"zcq_{uuid.uuid4().hex}"
    for attempt in range(2):
        try:
            conn.execute(
                """
                INSERT INTO zone_c_query_history (
                    id, kind, query_text, query_sha256, matched_groups_json, decision, reason_code, ai_job_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    kind,
                    query_text,
                    query_sha256,
                    json.dumps(list(matched_groups)),
                    decision,
                    reason_code,
                    ai_job_id,
                    created_at,
                ),
            )
            conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) and attempt == 0:
                _ensure_table(conn)
                continue
            logger.warning("Zone-C query history write failed: %s", exc)
            return False
        except Exception as exc:
            logger.warning("Zone-C query history write failed: %s", exc)
            return False
    return False


def _get_rolling_groups_union(conn: sqlite3.Connection, cutoff_iso: str) -> set[str]:
    """Return union of distinct concept group identifiers from allowed queries within rolling window."""
    try:
        cursor = conn.execute(
            """
            SELECT matched_groups_json FROM zone_c_query_history
            WHERE decision = 'allowed' AND created_at >= ?
            ORDER BY created_at DESC
            """,
            (cutoff_iso,),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            _ensure_table(conn)
            return set()
        raise
    union_groups: set[str] = set()
    for row in cursor.fetchall():
        try:
            groups = json.loads(row[0])
            if isinstance(groups, list):
                union_groups.update(str(g) for g in groups if g)
        except (json.JSONDecodeError, ValueError):
            continue
    return union_groups


def _get_recent_same_group_queries(
    conn: sqlite3.Connection,
    group_indices: tuple[str, ...],
    limit: int = 20,
) -> list[str]:
    """Retrieve up to limit recent allowed queries that touched any of group_indices."""
    target_set = set(group_indices)
    try:
        cursor = conn.execute(
            """
            SELECT query_text, matched_groups_json FROM zone_c_query_history
            WHERE decision = 'allowed'
            ORDER BY created_at DESC
            LIMIT 500
            """
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            _ensure_table(conn)
            return []
        raise
    queries: list[str] = []
    for row in cursor.fetchall():
        try:
            matched = json.loads(row[1])
            if isinstance(matched, list):
                matched_set = {str(g) for g in matched if g}
                if matched_set & target_set:
                    queries.append(row[0])
                    if len(queries) >= limit:
                        break
        except (json.JSONDecodeError, ValueError):
            continue
    return queries


def evaluate_zone_c_request(
    kind: str,
    text: str,
    *,
    target_url: str | None = None,
    connection: sqlite3.Connection | None = None,
    vault_dir: Path | None = None,
    runner: Callable[..., AiTaskOutcome] = run_ai_task,
) -> ZoneCDecision:
    """Evaluate an outbound Zone-C request through deterministic, local judge, and mosaic stages."""
    combined_query_text = f"{text}\n{target_url}".strip() if target_url and text and text != target_url else (text or target_url or "").strip()
    query_sha256 = hashlib.sha256(combined_query_text.encode("utf-8", errors="replace")).hexdigest()

    # Fail closed on missing/corrupt vault if JARVISOS_VAULT_PATH is set
    loaded_vault: vault.Vault | None = None
    vault_env = os.environ.get(vault.VAULT_PATH_ENV, "").strip()
    effective_vault_dir = vault_dir or (Path(vault_env) if vault_env else None)

    if effective_vault_dir is not None:
        if not (effective_vault_dir / "jarvis_vault.toml").is_file():
            return _finalize(
                allowed=False,
                reason_code="vault_load_error",
                kind=kind,
                text=combined_query_text,
                sha256=query_sha256,
                matched_groups=(),
                connection=connection,
            )
        try:
            loaded_vault = vault.read_vault(effective_vault_dir)
        except Exception:
            return _finalize(
                allowed=False,
                reason_code="vault_load_error",
                kind=kind,
                text=combined_query_text,
                sha256=query_sha256,
                matched_groups=(),
                connection=connection,
            )
        if loaded_vault is None:
            return _finalize(
                allowed=False,
                reason_code="vault_load_error",
                kind=kind,
                text=combined_query_text,
                sha256=query_sha256,
                matched_groups=(),
                connection=connection,
            )

    # Determine strict mode
    config = read_consult_config(effective_vault_dir) if effective_vault_dir else load_consult_config()
    strict_mode = config.zone_c_strict

    # STAGE 1: Deterministic search guard
    stage1_allowed, stage1_reason, rule_idx = _evaluate_stage1(text, target_url)
    if not stage1_allowed:
        try:
            s1_packs = packs.load_packs()
            s1_groups = _compute_matched_groups(combined_query_text, s1_packs)
        except Exception:
            s1_groups = ()
        return _finalize(
            allowed=False,
            reason_code=stage1_reason,
            rule_index=rule_idx,
            kind=kind,
            text=combined_query_text,
            sha256=query_sha256,
            matched_groups=s1_groups,
            connection=connection,
        )

    # Load packs fail-closed for Stages 2 and 3
    try:
        loaded_packs = packs.load_packs()
    except Exception:
        return _finalize(
            allowed=False,
            reason_code="pack_load_error",
            kind=kind,
            text=combined_query_text,
            sha256=query_sha256,
            matched_groups=(),
            connection=connection,
        )

    # Concept groups touched by current request
    matched_groups = _compute_matched_groups(combined_query_text, loaded_packs)

    # STAGE 2: Fresh-context local inference judge
    local_items = loaded_vault.local_only if loaded_vault else ()
    prot_props = loaded_vault.protected_propositions if loaded_vault else ()
    all_propositions = (*local_items, *prot_props)
    pack_terms = tuple(t for pack in loaded_packs for t in pack.sensitivity_terms)
    should_run_judge = effective_vault_dir is not None or os.environ.get(FAKE_TRANSPORT_ENV) == "1"

    ai_job_id: str | None = None
    if should_run_judge:
        if connection is not None:
            _ensure_db_initialized(connection)
        else:
            try:
                with open_sqlite_connection() as c:
                    _ensure_db_initialized(c)
            except Exception:
                pass

        # Build Stage 2 prompt
        prompt_lines = [
            "You are a local security judge evaluating whether outbound text for an external search or page fetch",
            "states or allows inferring any local-only items, protected propositions, sensitive terms, or specific design values.",
            "",
            "Text to evaluate:",
            combined_query_text,
            "",
            "Local-only items and protected propositions:",
        ]
        if all_propositions:
            for item_id, item_text in all_propositions:
                prompt_lines.append(f"[{item_id}]: {item_text}")
        else:
            prompt_lines.append("(None)")

        if pack_terms:
            prompt_lines.append("")
            prompt_lines.append("Sensitive project terms:")
            for t in pack_terms:
                prompt_lines.append(f"- {t}")

        prompt_lines.extend([
            "",
            "Does this text state or allow inferring any of these protected items, sensitive terms, or specific design values?",
            "Set \"uncertain\": true if you are unsure whether the text could imply a protected item.",
            "Return strict JSON only: {\"hits\": [ids], \"uncertain\": false, \"reason\": \"explanation\"}",
        ])
        stage2_prompt = "\n".join(prompt_lines)

        ok, hits, judge_reason, ai_job_id, is_uncertain = _call_local_judge(
            stage2_prompt,
            metadata={"judge": "zone_c_inference"},
            runner=runner,
            fake_env_var=FAKE_ZONE_C_INFERENCE_ENV,
        )

        if not ok:
            return _finalize(
                allowed=False,
                reason_code="zone_c_judge_error",
                ai_job_id=ai_job_id,
                kind=kind,
                text=combined_query_text,
                sha256=query_sha256,
                matched_groups=matched_groups,
                connection=connection,
            )

        if hits or (strict_mode and is_uncertain):
            return _finalize(
                allowed=False,
                reason_code="zone_c_inference_judge",
                ai_job_id=ai_job_id,
                kind=kind,
                text=combined_query_text,
                sha256=query_sha256,
                matched_groups=matched_groups,
                connection=connection,
            )

    # STAGE 3: Mosaic guard
    last_ai_job_id = ai_job_id

    if matched_groups:
        # Check rolling 30-day concept group counter
        cutoff = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _eval_mosaic(conn: sqlite3.Connection) -> ZoneCDecision | None:
            nonlocal last_ai_job_id
            rolling_groups = _get_rolling_groups_union(conn, cutoff)

            for pack in loaded_packs:
                guard_cfg = pack.search_guard
                if not guard_cfg.groups or guard_cfg.min_groups <= 0:
                    continue
                prefix = f"{pack.id}:"
                pack_matched = {g for g in matched_groups if g.startswith(prefix)}
                if not pack_matched:
                    continue
                pack_rolling = {g for g in rolling_groups if g.startswith(prefix)}
                combined_groups = pack_rolling | pack_matched
                if len(combined_groups) >= guard_cfg.min_groups:
                    return _finalize_with_conn(
                        conn,
                        allowed=False,
                        reason_code="mosaic_group_threshold",
                        ai_job_id=last_ai_job_id,
                        kind=kind,
                        text=combined_query_text,
                        sha256=query_sha256,
                        matched_groups=matched_groups,
                    )

            # Contextual mosaic judge over last 20 allowed queries touching the same groups
            recent_queries = _get_recent_same_group_queries(conn, matched_groups, limit=20)
            if recent_queries and should_run_judge:
                mosaic_prompt_lines = [
                    "You are a local security judge evaluating whether a sequence of searches allows cross-query mosaic inference",
                    "of protected project knowledge, local-only items, protected propositions, or specific design values.",
                    "",
                    "Current query:",
                    combined_query_text,
                    "",
                    "Recent related queries by this workstation:",
                ]
                for idx, rq in enumerate(recent_queries, 1):
                    mosaic_prompt_lines.append(f"{idx}. {rq}")

                mosaic_prompt_lines.append("")
                mosaic_prompt_lines.append("Local-only items and protected propositions:")
                if all_propositions:
                    for item_id, item_text in all_propositions:
                        mosaic_prompt_lines.append(f"[{item_id}]: {item_text}")
                else:
                    mosaic_prompt_lines.append("(None)")

                if pack_terms:
                    mosaic_prompt_lines.append("")
                    mosaic_prompt_lines.append("Sensitive project terms:")
                    for t in pack_terms:
                        mosaic_prompt_lines.append(f"- {t}")

                mosaic_prompt_lines.extend([
                    "",
                    "Could an external observer combining this current query with the recent queries infer any of these",
                    "local-only items, protected propositions, sensitive terms, or specific design values?",
                    "Set \"uncertain\": true if you are unsure whether the combination could imply a protected item.",
                    "Return strict JSON only: {\"hits\": [ids], \"uncertain\": false, \"reason\": \"explanation\"}",
                ])
                mosaic_prompt = "\n".join(mosaic_prompt_lines)

                m_ok, m_hits, m_reason, m_job_id, m_uncertain = _call_local_judge(
                    mosaic_prompt,
                    metadata={"judge": "zone_c_mosaic"},
                    runner=runner,
                    fake_env_var=FAKE_ZONE_C_MOSAIC_ENV,
                )
                if m_job_id:
                    last_ai_job_id = m_job_id

                if not m_ok:
                    return _finalize_with_conn(
                        conn,
                        allowed=False,
                        reason_code="zone_c_judge_error",
                        ai_job_id=last_ai_job_id,
                        kind=kind,
                        text=combined_query_text,
                        sha256=query_sha256,
                        matched_groups=matched_groups,
                    )

                if m_hits or (strict_mode and m_uncertain):
                    return _finalize_with_conn(
                        conn,
                        allowed=False,
                        reason_code="zone_c_mosaic_judge",
                        ai_job_id=last_ai_job_id,
                        kind=kind,
                        text=combined_query_text,
                        sha256=query_sha256,
                        matched_groups=matched_groups,
                    )

            return None

        if connection is not None:
            mosaic_res = _eval_mosaic(connection)
        else:
            try:
                with open_sqlite_connection() as conn:
                    mosaic_res = _eval_mosaic(conn)
            except Exception as exc:
                logger.warning("Zone-C mosaic evaluation DB error: %s", exc)
                return ZoneCDecision(
                    allowed=False,
                    reason_code="guard_error",
                    refusal_message=REFUSAL_MESSAGES["guard_error"],
                    query_sha256=query_sha256,
                )

        if mosaic_res is not None:
            return mosaic_res

    # ALL STAGES PASSED -> Record as allowed
    return _finalize(
        allowed=True,
        reason_code="ok",
        ai_job_id=last_ai_job_id,
        kind=kind,
        text=combined_query_text,
        sha256=query_sha256,
        matched_groups=matched_groups,
        connection=connection,
    )


def _finalize_with_conn(
    conn: sqlite3.Connection,
    *,
    allowed: bool,
    reason_code: str,
    kind: str,
    text: str,
    sha256: str,
    matched_groups: tuple[str, ...],
    ai_job_id: str | None = None,
    rule_index: int | None = None,
) -> ZoneCDecision:
    decision_str = "allowed" if allowed else "blocked"
    written = _record_query_history(
        conn,
        kind=kind,
        query_text=text,
        query_sha256=sha256,
        matched_groups=matched_groups,
        decision=decision_str,
        reason_code=reason_code,
        ai_job_id=ai_job_id,
    )
    if not written:
        # DB write error must fail closed
        return ZoneCDecision(
            allowed=False,
            reason_code="guard_error",
            refusal_message=REFUSAL_MESSAGES["guard_error"],
            query_sha256=sha256,
        )

    refusal = REFUSAL_MESSAGES.get(reason_code) if not allowed else None
    return ZoneCDecision(
        allowed=allowed,
        reason_code=reason_code,
        refusal_message=refusal,
        rule_index=rule_index,
        ai_job_id=ai_job_id,
        matched_groups=matched_groups,
        query_sha256=sha256,
    )


def _finalize(
    *,
    allowed: bool,
    reason_code: str,
    kind: str,
    text: str,
    sha256: str,
    matched_groups: tuple[str, ...],
    connection: sqlite3.Connection | None = None,
    ai_job_id: str | None = None,
    rule_index: int | None = None,
) -> ZoneCDecision:
    if connection is not None:
        return _finalize_with_conn(
            connection,
            allowed=allowed,
            reason_code=reason_code,
            kind=kind,
            text=text,
            sha256=sha256,
            matched_groups=matched_groups,
            ai_job_id=ai_job_id,
            rule_index=rule_index,
        )
    try:
        with open_sqlite_connection() as conn:
            return _finalize_with_conn(
                conn,
                allowed=allowed,
                reason_code=reason_code,
                kind=kind,
                text=text,
                sha256=sha256,
                matched_groups=matched_groups,
                ai_job_id=ai_job_id,
                rule_index=rule_index,
            )
    except Exception as exc:
        logger.warning("Zone-C query finalize failed to open DB: %s", exc)
        return ZoneCDecision(
            allowed=False,
            reason_code="guard_error",
            refusal_message=REFUSAL_MESSAGES["guard_error"],
            query_sha256=sha256,
        )
