"""Stage 2 — Golden characterization of current sensitivity / egress behavior.

These tests are a SAFETY NET, not a specification of desired behavior. They lock
the *current* verdicts of the duplicated sensitivity logic so that any future
consolidation (Stage 5) is deliberate and visible, never accidental.

Where current behavior is weak, inconsistent, or permissive, we characterize it
AS-IS and mark it explicitly. Do not "fix" anything here — fixing belongs to a
later, dedicated slice gated by these golden values.

Call sites characterized:
  - backend/app/modules/ai/privacy.py        (PrivacyPolicyEngine)
  - backend/app/modules/events/service.py    (redact_event_payload)
  - backend/app/modules/runner/safety.py     (preflight_script_policy)

Existing coverage NOT duplicated here:
  - classify() full table          -> test_ai_contracts.py
  - redact_event_payload basics    -> test_data_infrastructure.py
  - FAST_DEV allow/block/bypass     -> test_ai_fast_dev_policy.py
  - naive forbidden-marker blocking -> test_python_runner.py
"""

from __future__ import annotations

import pytest

from app.modules.ai.contracts import AIPolicyMode
from app.modules.ai.privacy import PrivacyPolicyEngine
from app.modules.events.service import redact_event_payload
from app.modules.runner.safety import RunnerSafetyError, preflight_script_policy

# ---------------------------------------------------------------------------
# Small pure helpers — no network, no providers, no real data root.
# ---------------------------------------------------------------------------

def _event_key_redacted(key: str) -> bool:
    """True if an event payload key of this name redacts its value."""
    redacted = redact_event_payload({key: "SENTINEL_VALUE"})
    assert isinstance(redacted, dict)
    return redacted[key] == "[REDACTED]"


def _runner_preflight_blocks(content: str, tmp_path) -> bool:
    """True if the runner preflight rejects a script containing this content."""
    script = tmp_path / "candidate.py"
    script.write_text(content + "\n", encoding="utf-8")
    try:
        preflight_script_policy(script)
        return False
    except RunnerSafetyError:
        return True


# ---------------------------------------------------------------------------
# 1. Intra-file privacy drift: two methods in privacy.py disagree on the
#    SAME ambiguous input. This is the highest-value golden for Stage 5.
# ---------------------------------------------------------------------------

AMBIGUOUS_INPUTS = (
    "Unlabeled fragment.",
    "Some neutral sentence with no markers at all.",
    "qwerty random text 12345",
)


@pytest.mark.parametrize("text", AMBIGUOUS_INPUTS)
def test_golden_smoke_console_fast_dev_allows_ambiguous_as_internal(text: str) -> None:
    """CURRENT BEHAVIOR: FAST_DEV smoke console treats ambiguous text as
    'internal' and ALLOWS external egress. This is fail-open by design of the
    dev mode. Future hardening target — locked here, not changed."""
    decision = PrivacyPolicyEngine().decide_for_smoke_console(
        text, policy_mode=AIPolicyMode.FAST_DEV
    )
    assert decision.privacy_class == "internal"
    assert decision.external_allowed is True
    assert decision.blocking_reason is None


@pytest.mark.parametrize("text", AMBIGUOUS_INPUTS)
def test_golden_external_smoke_test_blocks_same_ambiguous_as_unknown(text: str) -> None:
    """CURRENT BEHAVIOR: the SAME ambiguous text is classified 'unknown' and
    BLOCKED by decide_for_external_smoke_test. Two methods of the same engine
    reach opposite egress verdicts on identical input — this is the drift the
    consolidation step must resolve deliberately."""
    decision = PrivacyPolicyEngine().decide_for_external_smoke_test(
        text, confidential_allowed=False
    )
    assert decision.privacy_class == "unknown"
    assert decision.external_allowed is False
    assert decision.blocking_reason == "privacy_policy_unknown_blocked"


# ---------------------------------------------------------------------------
# 2. Cross-site sensitivity drift table on bare tokens.
#    Same token string -> three independent verdicts. The systems are NOT
#    required to agree; the table documents the current disagreement.
#
#    Columns (current behavior):
#      classify_verdict : PrivacyPolicyEngine().classify(token)
#      event_key_redacted : value redacted when token is used as a payload key
#      runner_blocks    : preflight rejects a script containing the token
# ---------------------------------------------------------------------------

# (token, classify_verdict, event_key_redacted, runner_blocks)
SENSITIVITY_DRIFT_TABLE = (
    # Full agreement: all three treat "password" as sensitive.
    ("password", "secret", True, True),
    # DRIFT: classify misses bare "token", event key not redacted, runner blocks.
    ("token", "unknown", False, True),
    # DRIFT: classify misses bare "secret", but event key + runner catch it.
    ("secret", "unknown", True, True),
    # DRIFT: classify is space-sensitive — "api_key" (underscore) is NOT caught
    # by classify (which looks for "api key" with a space), yet events + runner
    # catch the underscore form.
    ("api_key", "unknown", True, True),
    # DRIFT: "api key" (space) IS caught by classify and runner, but as an event
    # KEY it is not redacted (exact/ fragment lists use the underscore form).
    ("api key", "secret", False, True),
)


@pytest.mark.parametrize(
    ("token", "classify_verdict", "event_key_redacted", "runner_blocks"),
    SENSITIVITY_DRIFT_TABLE,
)
def test_golden_cross_site_sensitivity_drift(
    token: str,
    classify_verdict: str,
    event_key_redacted: bool,
    runner_blocks: bool,
    tmp_path,
) -> None:
    """Locks the current per-site verdict for each token. Rows where the three
    columns disagree are intentional drift, kept visible for Stage 5."""
    assert PrivacyPolicyEngine().classify(token) == classify_verdict
    assert _event_key_redacted(token) is event_key_redacted
    assert _runner_preflight_blocks(token, tmp_path) is runner_blocks


def test_golden_classify_is_space_sensitive_for_api_key() -> None:
    """CURRENT BEHAVIOR: classify() distinguishes 'api key' (space -> secret)
    from 'api_key' (underscore -> unknown). This narrow string matching is a
    future hardening target, not a desired distinction."""
    engine = PrivacyPolicyEngine()
    assert engine.classify("api key") == "secret"
    assert engine.classify("api_key") == "unknown"


# ---------------------------------------------------------------------------
# 3. Runner preflight is a substring blocklist, not a real sandbox.
#    Naive imports are blocked; trivially obfuscated equivalents are NOT.
#    The real protection is hash-pinning of a single reviewed script — the
#    blocklist is a secondary, bypassable layer.
# ---------------------------------------------------------------------------

def test_golden_runner_blocks_naive_socket_import(tmp_path) -> None:
    """Sanity anchor: the naive form is blocked (substring 'import socket')."""
    assert _runner_preflight_blocks("import socket", tmp_path) is True


@pytest.mark.parametrize(
    "obfuscated",
    [
        "importlib.import_module('socket')",
        "__import__('socket')",
        "mod = 'so' + 'cket'\nimportlib.import_module(mod)",
    ],
)
def test_golden_runner_misses_obfuscated_socket_import(obfuscated: str, tmp_path) -> None:
    """CURRENT BEHAVIOR / KNOWN WEAKNESS: the FORBIDDEN_SCRIPT_MARKERS substring
    check does NOT catch obfuscated imports. These pass preflight today. We lock
    this as current behavior — the actual safety guarantee is the hash-pinned
    single reviewed script, not this blocklist. Future hardening target."""
    assert _runner_preflight_blocks(obfuscated, tmp_path) is False
