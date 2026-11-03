"""Deterministic RouterPolicy decision producer for A2.

This module is a contract probe only. It produces RouterPolicy decision objects
from normalized input. It does not route chat, call providers or models, execute
tools, browse, run terminal commands, write files or memory, or retrieve data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

POLICY_VERSION = "router_policy_v0_3_1_1"
SCHEMA_VERSION = "router_policy_decision_v0_3_1_1"
DEFAULT_NOW = "1970-01-01T00:00:00+00:00"
LOCAL_PROVIDER = "local:qwen"
TIER_RANK = {
    "LOCAL_ONLY": 0,
    "LOCAL_FAST": 1,
    "CHEAP_EXTERNAL": 2,
    "SCIENTIFIC_MEDIUM": 3,
    "FRONTIER": 4,
}
SAFE_SENSITIVITY = {"public", "internal"}
UNSAFE_OR_UNKNOWN_SENSITIVITY = {"unknown", "sensitive", "secret"}
SCIENTIFIC_DOMAINS = {"scientific", "engineering", "code"}
SIMPLE_TASK_TYPES = {"casual_or_general", "general_question", "explanation", "answer"}
VALID_EXTERNAL_TARGETS = {
    "external:cheap",
    "external:scientific_medium",
    "external:frontier",
}
_DIGEST_PURPOSE = "router_confirmation_intent"
_DIGEST_VERSION = "v1"
_INCLUDED_DIGEST_FIELDS = (
    "proposed_external_target",
    "provider_call_allowed_now",
    "external_network_allowed_now",
    "confirmation_required",
    "confirmation_payload_required",
    "confirmation_payload",
    "confirmation_options",
)
_EXCLUDED_EXISTING_DIGEST_FIELDS = frozenset({"confirmation_digest", "digest"})


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _canonicalize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _canonicalize_value(val)
            for key, val in sorted(value.items())
            if key not in _EXCLUDED_EXISTING_DIGEST_FIELDS
        }
    if isinstance(value, list):
        return [_canonicalize_value(item) for item in value]
    return value


def _canonicalize_confirmation_intent(decision_or_payload: Mapping[str, Any]) -> dict[str, Any]:
    canonical_payload: dict[str, Any] = {}
    for field in _INCLUDED_DIGEST_FIELDS:
        if field in decision_or_payload:
            canonical_payload[field] = _canonicalize_value(decision_or_payload[field])
    return canonical_payload


def _compute_confirmation_digest(decision_or_payload: Mapping[str, Any]) -> dict[str, Any]:
    canonical_payload = _canonicalize_confirmation_intent(decision_or_payload)
    canonical_envelope = {
        "digest_purpose": _DIGEST_PURPOSE,
        "digest_version": _DIGEST_VERSION,
        "confirmation_intent": canonical_payload,
    }
    encoded = json.dumps(canonical_envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "canonical_payload": canonical_payload,
        "canonical_envelope": canonical_envelope,
        "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def _normalize_allowed_targets(allowed_targets: set[str] | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if allowed_targets is None:
        return ()
    normalized = []
    for value in allowed_targets:
        if value in VALID_EXTERNAL_TARGETS and value not in normalized:
            normalized.append(value)
    return tuple(sorted(normalized))


def evaluate_external_egress_scope(
    proposed_external_target: str | None,
    allowed_targets: set[str] | list[str] | tuple[str, ...] | None,
) -> dict[str, object]:
    normalized_allowed_targets = _normalize_allowed_targets(allowed_targets)
    if proposed_external_target is None:
        return {
            "allowed": False,
            "proposed_external_target": None,
            "normalized_allowed_targets": normalized_allowed_targets,
            "reason_code": "missing_target",
            "reason_codes": ["missing_target"],
        }
    if proposed_external_target not in VALID_EXTERNAL_TARGETS:
        return {
            "allowed": False,
            "proposed_external_target": proposed_external_target,
            "normalized_allowed_targets": normalized_allowed_targets,
            "reason_code": "invalid_target",
            "reason_codes": ["invalid_target"],
        }
    if proposed_external_target not in normalized_allowed_targets:
        return {
            "allowed": False,
            "proposed_external_target": proposed_external_target,
            "normalized_allowed_targets": normalized_allowed_targets,
            "reason_code": "target_not_in_allowed_targets",
            "reason_codes": ["target_not_in_allowed_targets"],
        }
    return {
        "allowed": True,
        "proposed_external_target": proposed_external_target,
        "normalized_allowed_targets": normalized_allowed_targets,
        "reason_code": "target_allowed",
        "reason_codes": [],
    }


def _phase_a(input_obj: dict[str, Any]) -> dict[str, Any]:
    return input_obj.get("phase_a_signals") or {}


def _phase_b(input_obj: dict[str, Any]) -> dict[str, Any]:
    return input_obj.get("phase_b_soft_proposal") or {}


def _router_hint(input_obj: dict[str, Any]) -> dict[str, Any]:
    return input_obj.get("router_hint") or {}


def _user_policy(input_obj: dict[str, Any]) -> dict[str, Any]:
    return input_obj.get("user_policy") or {}


def _provider_policy(input_obj: dict[str, Any]) -> dict[str, Any]:
    return input_obj.get("provider_policy") or {}


def _budget_policy(input_obj: dict[str, Any]) -> dict[str, Any]:
    return input_obj.get("budget_policy") or {}


def _budget_allows(input_obj: dict[str, Any], tier: str) -> bool:
    max_tier = _budget_policy(input_obj).get("max_tier")
    if tier not in TIER_RANK or max_tier not in TIER_RANK:
        return False
    return TIER_RANK[tier] <= TIER_RANK[max_tier]


def _provider_allows(input_obj: dict[str, Any], tier: str) -> bool:
    policy = _provider_policy(input_obj)
    allowed = set(policy.get("allowed_provider_tiers") or [])
    blocked = set(policy.get("blocked_provider_tiers") or [])
    return tier in allowed and tier not in blocked


def _candidate_tier(input_obj: dict[str, Any]) -> str | None:
    for tier in ("SCIENTIFIC_MEDIUM", "FRONTIER"):
        if _budget_allows(input_obj, tier) and _provider_allows(input_obj, tier):
            return tier
    return None


def _external_target_for_tier(tier: str | None) -> str | None:
    if tier == "FRONTIER":
        return "external:frontier"
    if tier == "SCIENTIFIC_MEDIUM":
        return "external:scientific_medium"
    if tier == "CHEAP_EXTERNAL":
        return "external:cheap"
    return None


def _allowed_external_targets(input_obj: dict[str, Any]) -> tuple[str, ...]:
    policy = _provider_policy(input_obj)
    allowed = policy.get("allowed_provider_tiers") or []
    blocked = set(policy.get("blocked_provider_tiers") or [])
    targets = []
    for tier in allowed:
        if tier in blocked:
            continue
        if not _budget_allows(input_obj, tier):
            continue
        target = _external_target_for_tier(tier)
        if target is not None:
            targets.append(target)
    return tuple(sorted(set(targets)))


def _has_external_pressure(input_obj: dict[str, Any]) -> bool:
    phase_a = _phase_a(input_obj)
    hint = _router_hint(input_obj)
    action = input_obj.get("action_hint") or {}
    return bool(
        phase_a.get("mentions_external_provider_or_upload_intent")
        or action.get("needs_provider_call")
        or action.get("requested_action_type") in {"provider_call", "browser_search", "tool_call", "mcp_call"}
        or hint.get("needs_scientific_depth")
        or hint.get("needs_current_info")
        or (hint.get("complexity") == "high" and hint.get("domain") in SCIENTIFIC_DOMAINS)
    )


def _qualifies_for_external_candidate(input_obj: dict[str, Any]) -> bool:
    phase_a = _phase_a(input_obj)
    hint = _router_hint(input_obj)
    return bool(
        phase_a.get("sensitivity_bucket_proposal") in SAFE_SENSITIVITY
        and hint.get("complexity") == "high"
        and (hint.get("needs_scientific_depth") or hint.get("domain") in SCIENTIFIC_DOMAINS)
        and _user_policy(input_obj).get("external_routing_enabled") is True
    )


def _external_routing_enabled(input_obj: dict[str, Any]) -> bool:
    return _user_policy(input_obj).get("external_routing_enabled") is True


def _confirmation_payload(target: str) -> dict[str, Any]:
    return {
        "scope": "external_provider_call",
        "target": target,
        "payload_preview": "Redacted external-provider routing proposal for user review.",
        "payload_preview_truncated": False,
        "full_payload_available_for_review": True,
        "payload_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "full_payload_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
        "redaction_status": "redacted",
        "estimated_tokens": 0,
        "estimated_cost_class": "medium",
        "side_effect_level": "none",
        "reversibility": "reversible",
        "diff_summary": None,
        "full_diff_available_for_review": False,
        "full_diff_digest": None,
        "file_operations": [],
        "command": None,
        "cwd": None,
        "terminal_risk_summary": None,
        "env_preview_redacted": None,
        "network_access_expected": True,
        "writes_outside_workspace": False,
        "destructive_command_detected": False,
        "file_paths": [],
    }


def _base_decision(input_obj: dict[str, Any], now: str | None) -> dict[str, Any]:
    input_digest = _canonical_digest(input_obj)
    created_at = now or DEFAULT_NOW
    max_tokens = _budget_policy(input_obj).get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens < 0:
        max_tokens = 0
    return {
        "policy_version": POLICY_VERSION,
        "schema_version": SCHEMA_VERSION,
        "decision_id": "decision-" + input_digest.removeprefix("sha256:")[:16],
        "input_digest": input_digest,
        "created_at": created_at,
        "expires_at": None,
        "lifecycle_stage": "initial_request",
        "route_action": "route_local",
        "route_tier": "LOCAL_FAST",
        "provider_candidate": LOCAL_PROVIDER,
        "proposed_external_target": None,
        "external_allowed": False,
        "external_network_allowed_now": False,
        "local_allowed": True,
        "response_allowed_now": True,
        "tool_execution_allowed_now": False,
        "provider_call_allowed_now": False,
        "state_change_allowed_now": False,
        "confirmation_required": False,
        "requires_new_decision_after_confirmation": False,
        "redaction_required": False,
        "redaction_status": "not_required",
        "manual_review_required": False,
        "simulation_required": False,
        "dry_run_required": False,
        "requested_action_type": "answer",
        "modifies_state": False,
        "state_scope": "none",
        "side_effect_level": "none",
        "reversibility": "reversible",
        "environment_type": "chat",
        "allowed_execution_mode": "propose_only",
        "confirmation_payload_required": False,
        "confirmation_payload": None,
        "confirmation_digest": None,
        "confirmation_options": [],
        "consent_context": None,
        "memory_policy_result": None,
        "reason_codes": ["default_local_fallback"],
        "audit_notes": ["Deterministic safe local/propose fallback."],
        "budget_class": "local",
        "max_tokens_allowed": max_tokens,
    }


def _block_secret(decision: dict[str, Any]) -> dict[str, Any]:
    decision.update(
        {
            "lifecycle_stage": "blocked",
            "route_action": "blocked",
            "route_tier": "BLOCKED",
            "provider_candidate": "none",
            "local_allowed": False,
            "response_allowed_now": False,
            "allowed_execution_mode": "blocked",
            "redaction_required": True,
            "redaction_status": "required_pending",
            "manual_review_required": True,
            "reason_codes": ["secret_or_credential", "redaction_required"],
            "audit_notes": ["Blocked because the input contains credential or secret material."],
            "budget_class": "blocked",
        }
    )
    return decision


def _private_local(decision: dict[str, Any]) -> dict[str, Any]:
    decision.update(
        {
            "route_action": "route_local",
            "route_tier": "LOCAL_ONLY",
            "provider_candidate": LOCAL_PROVIDER,
            "allowed_execution_mode": "propose_only",
            "response_allowed_now": True,
            "manual_review_required": True,
            "reason_codes": ["local_only_sensitive_context"],
            "audit_notes": ["Private or IP-sensitive context remains local-only."],
        }
    )
    return decision


def _private_provider_boundary(decision: dict[str, Any]) -> dict[str, Any]:
    decision.update(
        {
            "route_action": "ask_user_confirm",
            "route_tier": "USER_CONFIRM",
            "provider_candidate": "none",
            "proposed_external_target": "external:scientific_medium",
            "allowed_execution_mode": "propose_only",
            "response_allowed_now": True,
            "manual_review_required": True,
            "redaction_required": True,
            "redaction_status": "required_pending",
            "reason_codes": ["local_only_sensitive_context", "provider_boundary"],
            "audit_notes": ["Private provider-boundary request requires review before any external action."],
        }
    )
    return decision


def _clarification(decision: dict[str, Any]) -> dict[str, Any]:
    decision.update(
        {
            "route_action": "ask_clarification",
            "route_tier": "USER_CONFIRM",
            "provider_candidate": "none",
            "allowed_execution_mode": "propose_only",
            "response_allowed_now": True,
            "reason_codes": ["clarification_required"],
            "audit_notes": ["Clarification is required before routing."],
        }
    )
    return decision


def _unknown_external_pressure(decision: dict[str, Any]) -> dict[str, Any]:
    decision.update(
        {
            "route_action": "ask_user_confirm",
            "route_tier": "USER_CONFIRM",
            "provider_candidate": "none",
            "proposed_external_target": "external:scientific_medium",
            "allowed_execution_mode": "propose_only",
            "response_allowed_now": True,
            "manual_review_required": True,
            "reason_codes": ["ambiguous_external_routing", "default_local_fallback"],
            "audit_notes": ["Sensitivity is not positively safe, so external escalation requires review."],
        }
    )
    return decision


def _external_disabled_local_fallback(decision: dict[str, Any]) -> dict[str, Any]:
    decision.update(
        {
            "route_action": "route_local",
            "route_tier": "LOCAL_FAST",
            "provider_candidate": LOCAL_PROVIDER,
            "proposed_external_target": None,
            "external_allowed": False,
            "external_network_allowed_now": False,
            "provider_call_allowed_now": False,
            "allowed_execution_mode": "propose_only",
            "response_allowed_now": True,
            "confirmation_required": False,
            "requires_new_decision_after_confirmation": False,
            "redaction_required": False,
            "redaction_status": "not_required",
            "confirmation_payload_required": False,
            "confirmation_payload": None,
            "confirmation_digest": None,
            "confirmation_options": [],
            "reason_codes": ["default_local_fallback"],
            "audit_notes": ["External routing is disabled, so the decision is normalized to local/no-external."],
        }
    )
    return decision


def _external_scope_denied_proposal_only(decision: dict[str, Any]) -> dict[str, Any]:
    decision.update(
        {
            "external_allowed": False,
            "external_network_allowed_now": False,
            "provider_call_allowed_now": False,
            "confirmation_required": False,
            "requires_new_decision_after_confirmation": False,
            "confirmation_payload_required": False,
            "confirmation_payload": None,
            "confirmation_digest": None,
            "confirmation_options": [],
        }
    )
    return decision


def _has_live_external_companion_artifacts(decision: dict[str, Any]) -> bool:
    return bool(
        decision.get("external_allowed") is True
        or decision.get("external_network_allowed_now") is True
        or decision.get("provider_call_allowed_now") is True
        or decision.get("confirmation_required") is True
        or decision.get("confirmation_payload_required") is True
        or decision.get("confirmation_payload") is not None
        or decision.get("confirmation_digest") is not None
        or decision.get("confirmation_options")
    )


def _is_private_provider_boundary(decision: dict[str, Any]) -> bool:
    reason_codes = decision.get("reason_codes")
    return isinstance(reason_codes, list) and "provider_boundary" in reason_codes


def _has_hard_private_provider_boundary(input_obj: dict[str, Any]) -> bool:
    hard_reason_codes = _phase_a(input_obj).get("hard_reason_codes") or []
    return bool({"local_only_sensitive_context", "provider_or_upload_intent"} & set(hard_reason_codes))


def _preserves_private_provider_boundary_review(input_obj: dict[str, Any], decision: dict[str, Any]) -> bool:
    action = input_obj.get("action_hint") or {}
    return (
        _is_private_provider_boundary(decision)
        and action.get("needs_provider_call") is not True
        and not _has_hard_private_provider_boundary(input_obj)
    )


def _enforce_external_proposal_flag_invariant(input_obj: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    if _external_routing_enabled(input_obj):
        target = decision.get("proposed_external_target")
        if target is None:
            if _has_live_external_companion_artifacts(decision):
                return _external_scope_denied_proposal_only(decision)
            return decision
        egress_scope = evaluate_external_egress_scope(target, _allowed_external_targets(input_obj))
        if egress_scope["allowed"] is not True:
            return _external_scope_denied_proposal_only(decision)
        return decision
    if decision.get("proposed_external_target") is None:
        if _has_live_external_companion_artifacts(decision):
            return _external_scope_denied_proposal_only(decision)
        return decision
    if _preserves_private_provider_boundary_review(input_obj, decision):
        return decision
    return _external_disabled_local_fallback(decision)


def _external_candidate_proposal(input_obj: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    tier = _candidate_tier(input_obj)
    if tier is None:
        return _budget_or_policy_fallback(input_obj, decision)
    target = _external_target_for_tier(tier)
    decision.update(
        {
            "route_action": "ask_user_confirm",
            "route_tier": "USER_CONFIRM",
            "provider_candidate": "none",
            "proposed_external_target": target,
            "external_allowed": False,
            "allowed_execution_mode": "propose_only",
            "response_allowed_now": True,
            "confirmation_required": _user_policy(input_obj).get("external_requires_confirmation") is True,
            "requires_new_decision_after_confirmation": _user_policy(input_obj).get("external_requires_confirmation") is True,
            "reason_codes": ["high_complexity_external_candidate"],
            "audit_notes": ["Non-sensitive high-complexity request may be proposed for external review."],
            "budget_class": "medium" if tier == "SCIENTIFIC_MEDIUM" else "frontier",
        }
    )
    if decision["confirmation_required"]:
        payload = _confirmation_payload(target or "external:scientific_medium")
        decision["confirmation_payload_required"] = True
        decision["confirmation_payload"] = payload
        decision["confirmation_options"] = ["allow_once", "deny", "view_details"]
        decision["confirmation_digest"] = _compute_confirmation_digest(decision)["digest"]
        decision["reason_codes"].append("confirmation_required")
    return decision


def _simple_local(decision: dict[str, Any]) -> dict[str, Any]:
    decision.update(
        {
            "route_action": "answer_local",
            "route_tier": "LOCAL_FAST",
            "provider_candidate": LOCAL_PROVIDER,
            "response_allowed_now": True,
            "allowed_execution_mode": "answer_only",
            "reason_codes": ["low_complexity_local"],
            "audit_notes": ["Low-complexity non-sensitive request can be answered locally."],
        }
    )
    return decision


def _budget_or_policy_fallback(input_obj: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    hint = _router_hint(input_obj)
    if hint.get("complexity") == "high" or hint.get("needs_scientific_depth") is True:
        decision.update(
            {
                "route_action": "ask_user_confirm",
                "route_tier": "USER_CONFIRM",
                "provider_candidate": "none",
                "proposed_external_target": "external:scientific_medium",
                "allowed_execution_mode": "propose_only",
                "response_allowed_now": True,
                "manual_review_required": True,
                "reason_codes": ["budget_cap", "default_local_fallback"],
                "audit_notes": ["External routing is unavailable under current budget or provider policy."],
            }
        )
        return decision
    return decision


def _default_fallback(input_obj: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return _budget_or_policy_fallback(input_obj, decision)


def decide_router_policy(input_obj: dict, now: str | None = None) -> dict:
    """Return a full RouterPolicy v3.1.1 decision using first-match rules."""

    decision = _base_decision(input_obj, now)
    phase_a = _phase_a(input_obj)
    phase_b = _phase_b(input_obj)
    hint = _router_hint(input_obj)
    sensitivity = phase_a.get("sensitivity_bucket_proposal")

    # 1. Secret/credential hard rule.
    if phase_a.get("contains_secret_or_credential") is True:
        decision = _block_secret(decision)
        return _enforce_external_proposal_flag_invariant(input_obj, decision)

    # 2/3. Raw private/IP context, with provider/export intent as a stricter boundary.
    if phase_a.get("contains_raw_private_or_ip_sensitive_context") is True:
        if phase_a.get("mentions_external_provider_or_upload_intent") is True:
            decision = _private_provider_boundary(decision)
            return _enforce_external_proposal_flag_invariant(input_obj, decision)
        decision = _private_local(decision)
        return _enforce_external_proposal_flag_invariant(input_obj, decision)

    # 4. Clarification / ambiguity before escalation.
    if phase_a.get("clarification_required") is True or phase_b.get("soft_reason_code") == "clarification_context":
        decision = _clarification(decision)
        return _enforce_external_proposal_flag_invariant(input_obj, decision)

    # 8. Unknown/not-positively-safe sensitivity blocks external escalation pressure.
    if sensitivity in UNSAFE_OR_UNKNOWN_SENSITIVITY and _has_external_pressure(input_obj):
        if not _external_routing_enabled(input_obj):
            decision = _external_disabled_local_fallback(decision)
            return _enforce_external_proposal_flag_invariant(input_obj, decision)
        decision = _unknown_external_pressure(decision)
        return _enforce_external_proposal_flag_invariant(input_obj, decision)

    # 6. Positively non-sensitive high-complexity external candidate proposal.
    if _qualifies_for_external_candidate(input_obj):
        decision = _external_candidate_proposal(input_obj, decision)
        return _enforce_external_proposal_flag_invariant(input_obj, decision)

    # 5. Non-sensitive simple local chat.
    if hint.get("complexity") == "low" and hint.get("task_type") in SIMPLE_TASK_TYPES:
        decision = _simple_local(decision)
        return _enforce_external_proposal_flag_invariant(input_obj, decision)

    # 9. Deterministic fail-safe fallback.
    decision = _default_fallback(input_obj, decision)
    return _enforce_external_proposal_flag_invariant(input_obj, decision)


def decision_to_json(decision: dict[str, Any]) -> str:
    """Serialize decisions deterministically for tests and local inspection."""

    return json.dumps(decision, indent=2, sort_keys=True) + "\n"
