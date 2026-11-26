"""Deterministic local_only pre-check and fresh-context local judge (spec 381, §3.4).

Step 4 of the zone-A gate (and also executed on maintainer edits). Before any packet leaves
for zone A:
1. Vault fail-closed check: if JARVISOS_VAULT_PATH is unset or load_vault() fails, refuse
   (vault_absent / gate_error).
2. Deterministic pre-check: outbound text against pack exact combinations / min_groups,
   and against vault local_only_items() + protected_propositions() normalized literals.
   Hits return rewrite_required naming only the hit IDs without echoing proposition text.
3. Fresh-context local judge: invoke the active local model (Inkling) through run_ai_task
   (writing an ai_jobs row, invariant 2, local route only) with strict JSON verdict.
   Parse/provider errors refuse (judge_error); non-empty hits return rewrite_required.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.core import packs, vault
from app.modules.ai.contracts import AIRequest, AIResponse, AIUsage
from app.modules.ai.execution import AiTaskOutcome, run_ai_task
from app.modules.ai.execution_types import ProviderBinding

JUDGE_ROUTE_CLASS = "local:llamacpp"
FAKE_TRANSPORT_ENV = "JARVISOS_CONSULT_FAKE_TRANSPORT"
FAKE_JUDGE_ENV = "JARVISOS_CONSULT_FAKE_JUDGE"

JudgeStatus = Literal["pass", "rewrite_required", "refused"]


@dataclass(frozen=True)
class JudgeOutcome:
    status: JudgeStatus
    code: str | None = None
    hits: tuple[str, ...] = ()
    reason: str | None = None
    judge_reason: str | None = None
    ai_job_id: str | None = None


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


def _matches_exact_combinations(text: str, combinations: tuple[tuple[str, ...], ...]) -> bool:
    for combination in combinations:
        if combination and all(" ".join(term.lower().split()) in text for term in combination if term.strip()):
            return True
    return False


def _matches_min_groups(text: str, groups: tuple[tuple[str, ...], ...], min_groups: int) -> bool:
    if not groups or min_groups <= 0:
        return False
    matched = 0
    for group in groups:
        if any(" ".join(term.lower().split()) in text for term in group if term.strip()):
            matched += 1
    return matched >= min_groups


def run_local_judge(
    question: str,
    context: str = "",
    *,
    vault_dir: Path | None = None,
    runner: Callable[..., AiTaskOutcome] = run_ai_task,
    route_class: str = JUDGE_ROUTE_CLASS,
) -> JudgeOutcome:
    """Execute gate step 4: vault fail-closed, deterministic pre-check, and local judge."""
    # 1. Vault fail-closed check
    loaded_vault: vault.Vault | None
    if vault_dir is not None and (vault_dir / "jarvis_vault.toml").is_file():
        try:
            loaded_vault = vault.read_vault(vault_dir)
        except Exception:
            return JudgeOutcome(status="refused", code="gate_error", reason="gate_error")
    else:
        vault_path = os.environ.get(vault.VAULT_PATH_ENV, "").strip()
        if not vault_path:
            return JudgeOutcome(status="refused", code="vault_absent", reason="vault_absent")
        try:
            loaded_vault = vault.load_vault()
        except Exception:
            return JudgeOutcome(status="refused", code="gate_error", reason="gate_error")
        if loaded_vault is None:
            return JudgeOutcome(status="refused", code="vault_absent", reason="vault_absent")

    outbound_text = f"{question}\n\n{context}".strip()
    normalized_outbound = " ".join(outbound_text.lower().split())

    # 2. Deterministic pre-check
    hit_ids: list[str] = []

    # Pack search guard exact combinations and min_groups
    for pack in packs.load_packs():
        guard = pack.search_guard
        if guard.exact_combinations and _matches_exact_combinations(normalized_outbound, guard.exact_combinations):
            hit_ids.append("exact_combination")
        if guard.groups and _matches_min_groups(normalized_outbound, guard.groups, guard.min_groups):
            hit_ids.append("min_groups")

    # Vault local_only and protected_propositions normalized literals
    local_items = loaded_vault.local_only
    prot_props = loaded_vault.protected_propositions
    all_propositions = (*local_items, *prot_props)

    for item_id, item_text in all_propositions:
        norm_item = " ".join(item_text.lower().split())
        if norm_item and norm_item in normalized_outbound:
            if item_id not in hit_ids:
                hit_ids.append(item_id)

    if hit_ids:
        hits_tuple = tuple(dict.fromkeys(hit_ids))
        reason = (
            f"These items or concepts cannot leave the machine: {', '.join(hits_tuple)}. "
            "Rewrite without stating or implying them."
        )
        return JudgeOutcome(
            status="rewrite_required",
            code="local_only",
            hits=hits_tuple,
            reason=reason,
        )

    # 3. Fresh-context local judge via run_ai_task
    prompt_lines = [
        "You are a local security judge evaluating whether outbound text for an external consultation",
        "states or allows inferring any local-only items or protected propositions, or any specific design value.",
        "",
        "Text to evaluate:",
        outbound_text,
        "",
        "Local-only items and protected propositions:",
    ]
    if all_propositions:
        for item_id, item_text in all_propositions:
            prompt_lines.append(f"[{item_id}]: {item_text}")
    else:
        prompt_lines.append("(No specific items; evaluate if text states or allows inferring any specific design value)")

    prompt_lines.extend([
        "",
        "Does this text state or allow inferring any of these local-only items, or any specific design value? JSON {hits:[ids], reason}",
        "Return strict JSON only: {\"hits\": [ids], \"reason\": \"explanation\"}",
    ])
    prompt = "\n".join(prompt_lines)

    adapters: dict[str, Any] | None = None
    bindings: dict[str, Any] | None = None

    # Fakes exist only for offline tests and walks, and only together with the fake consult transport,
    # under which nothing leaves the machine.
    fake_response = None
    if os.environ.get(FAKE_TRANSPORT_ENV) == "1":
        fake_response = os.environ.get(FAKE_JUDGE_ENV) or json.dumps({"hits": [], "reason": "clean"})
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
            metadata={"judge": "local_only"},
        )
    except Exception as exc:
        return JudgeOutcome(status="refused", code="judge_error", reason=f"judge_error:{type(exc).__name__}")

    if outcome.status != "success" or outcome.response is None or outcome.response.text is None:
        return JudgeOutcome(
            status="refused",
            code="judge_error",
            reason=f"judge_error:{outcome.status}:{outcome.error_type}",
            ai_job_id=outcome.ledger_id,
        )

    raw_text = outcome.response.text.strip()
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        return JudgeOutcome(
            status="refused",
            code="judge_error",
            reason="judge_error",
            ai_job_id=outcome.ledger_id,
        )

    if not isinstance(parsed, dict) or set(parsed) != {"hits", "reason"}:
        return JudgeOutcome(
            status="refused",
            code="judge_error",
            reason="judge_error",
            ai_job_id=outcome.ledger_id,
        )

    parsed_hits = parsed.get("hits")
    parsed_reason = parsed.get("reason")
    if not isinstance(parsed_hits, list) or not isinstance(parsed_reason, str):
        return JudgeOutcome(
            status="refused",
            code="judge_error",
            reason="judge_error",
            ai_job_id=outcome.ledger_id,
        )

    if not all(isinstance(h, str) and h.strip() for h in parsed_hits):
        return JudgeOutcome(
            status="refused",
            code="judge_error",
            reason="judge_error",
            ai_job_id=outcome.ledger_id,
        )

    hits_tuple = tuple(dict.fromkeys(h.strip() for h in parsed_hits))
    if hits_tuple:
        reason = (
            f"These items or concepts cannot leave the machine: {', '.join(hits_tuple)}. "
            "Rewrite without stating or implying them."
        )
        return JudgeOutcome(
            status="rewrite_required",
            code="local_only",
            hits=hits_tuple,
            reason=reason,
            judge_reason=parsed_reason,
            ai_job_id=outcome.ledger_id,
        )

    return JudgeOutcome(
        status="pass",
        code=None,
        hits=(),
        reason=None,
        judge_reason=parsed_reason,
        ai_job_id=outcome.ledger_id,
    )
