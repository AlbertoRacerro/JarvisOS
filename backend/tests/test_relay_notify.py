"""Offline unit tests for relay_continuity_notify (spec 390)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_NOTIFY = Path(__file__).resolve().parent.parent.parent / "scripts" / "relay_continuity_notify.py"
_SPEC = importlib.util.spec_from_file_location("relay_continuity_notify", SCRIPT_NOTIFY)
assert _SPEC and _SPEC.loader
rc_notify = importlib.util.module_from_spec(_SPEC)
sys.modules["relay_continuity_notify"] = rc_notify
_SPEC.loader.exec_module(rc_notify)


def test_provider_auth_expired_template_rendering_across_agents() -> None:
    # Claude mentions plain claude without CLAUDE_CONFIG_DIR and /login
    claude_text = rc_notify.render("provider_auth_expired", {"agent": "claude"})
    assert claude_text == "Autenticazione scaduta per claude. Per ripristinare: claude (senza CLAUDE_CONFIG_DIR), poi /login."
    assert "CLAUDE_CONFIG_DIR" in claude_text
    assert "/login" in claude_text

    # Codex mentions codex login
    codex_text = rc_notify.render("provider_auth_expired", {"agent": "codex"})
    assert codex_text == "Autenticazione scaduta per codex. Per ripristinare: codex login."

    # Native agents mention agy login on host
    for ag in ("neon", "argon", "agy_opus", "hydrogen"):
        t = rc_notify.render("provider_auth_expired", {"agent": ag})
        assert t == f"Autenticazione scaduta per {ag}. Per ripristinare: login agy sul sistema host."


def test_provider_auth_expired_validation_rejects_invalid_inputs() -> None:
    # Invalid agent rejected
    assert rc_notify.render("provider_auth_expired", {"agent": "invalid_agent"}) is None
    # Missing required field rejected
    assert rc_notify.render("provider_auth_expired", {}) is None
    # Extra field rejected
    assert rc_notify.render("provider_auth_expired", {"agent": "claude", "extra": "leak"}) is None


def test_provider_auth_expired_notifier_delivery_and_deduplication() -> None:
    now = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)
    state = rc_notify.default_state() if hasattr(rc_notify, "default_state") else {
        "notify": {"window": [], "sent": {}, "failures": 0, "dropped": 0, "retry": None}
    }
    delivered = []

    def fake_sender(text: str) -> None:
        delivered.append(text)

    notifier = rc_notify.Notifier(state, sender=fake_sender, log=lambda _: None)

    # First send succeeds
    ok1 = notifier.send("provider_auth_expired", now, dedupe_key="auth_expired|claude", agent="claude")
    assert ok1 is True
    assert len(delivered) == 1
    assert "claude (senza CLAUDE_CONFIG_DIR), poi /login" in delivered[0]

    # Duplicate send within dedupe window is rejected
    ok2 = notifier.send("provider_auth_expired", now, dedupe_key="auth_expired|claude", agent="claude")
    assert ok2 is False
    assert len(delivered) == 1
