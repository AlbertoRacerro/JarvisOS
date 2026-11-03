import copy

import pytest

from app.modules.ai.egress_policy import (
    EXTERNAL_PROVIDER_OPERATION,
    default_egress_policy_path,
    load_egress_policy,
    parse_egress_policy,
)


def test_default_egress_policy_loads_from_canonical_path():
    policy = load_egress_policy()

    assert default_egress_policy_path().name == "ai_egress_policy.json"
    assert policy.schema_version == 1
    assert policy.policy_version == "egress-policy-v1"
    assert policy.trigger_version == "egress-triggers-v1"
    assert policy.sample_rate_bps == 500
    assert policy.confirmable_triggers == ("t1", "t2", "t5")
    assert policy.supported_operations == (EXTERNAL_PROVIDER_OPERATION,)
    assert len(policy.config_digest) == 64


def test_policy_digest_is_canonical_across_key_order():
    raw = _minimal_policy()
    reordered = dict(reversed(list(raw.items())))

    assert parse_egress_policy(raw).config_digest == parse_egress_policy(reordered).config_digest


def test_policy_rejects_noncanonical_load_path(tmp_path):
    alternate = tmp_path / "ai_egress_policy.json"
    alternate.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="must load from configs/ai_egress_policy.json"):
        load_egress_policy(alternate)


def test_policy_rejects_missing_or_extra_keys():
    raw = _minimal_policy()
    del raw["policy_version"]
    with pytest.raises(ValueError, match="missing keys: policy_version"):
        parse_egress_policy(raw)

    raw = _minimal_policy()
    raw["unexpected"] = True
    with pytest.raises(ValueError, match="unsupported keys: unexpected"):
        parse_egress_policy(raw)


def test_policy_rejects_sampling_below_five_percent():
    raw = _minimal_policy()
    raw["sample_rate_bps"] = 499

    with pytest.raises(ValueError, match="between 500 and 10000"):
        parse_egress_policy(raw)


def test_policy_rejects_nonconfirmable_or_duplicate_triggers():
    raw = _minimal_policy()
    raw["confirmable_triggers"] = ["t1", "t3"]
    with pytest.raises(ValueError, match="non-confirmable trigger"):
        parse_egress_policy(raw)

    raw = _minimal_policy()
    raw["confirmable_triggers"] = ["t1", "t1"]
    with pytest.raises(ValueError, match="contains duplicates"):
        parse_egress_policy(raw)


def test_policy_rejects_unsupported_operations_and_invalid_limits():
    raw = _minimal_policy()
    raw["supported_operations"] = ["external_provider_call", "other"]
    with pytest.raises(ValueError, match="only external_provider_call"):
        parse_egress_policy(raw)

    for field in (
        "max_prompt_chars",
        "max_context_blocks",
        "max_context_chars",
        "confirmation_ticket_ttl_seconds",
        "reservation_ttl_seconds",
    ):
        raw = _minimal_policy()
        raw[field] = 0
        with pytest.raises(ValueError, match=f"{field} must be a positive integer"):
            parse_egress_policy(raw)

    raw = _minimal_policy()
    raw["daily_soft_spend_usd"] = -0.01
    with pytest.raises(ValueError, match="must be non-negative"):
        parse_egress_policy(raw)


def _minimal_policy():
    return copy.deepcopy(
        {
            "schema_version": 1,
            "policy_version": "test-policy-v1",
            "trigger_version": "test-triggers-v1",
            "max_prompt_chars": 100,
            "max_context_blocks": 2,
            "max_context_chars": 200,
            "sample_rate_bps": 500,
            "confirmation_ticket_ttl_seconds": 60,
            "daily_soft_spend_usd": 1.0,
            "reservation_ttl_seconds": 30,
            "confirmable_triggers": ["t1", "t2", "t5"],
            "supported_operations": ["external_provider_call"],
        }
    )
