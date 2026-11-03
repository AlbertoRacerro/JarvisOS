import re
from dataclasses import dataclass

from app.core.packs import sensitivity_terms
from app.modules.ai.contracts import AIPolicyMode


@dataclass(frozen=True)
class PrivacyDecision:
    privacy_class: str
    external_allowed: bool
    blocking_reason: str | None


class PrivacyPolicyEngine:
    """Small local policy engine. Providers may suggest classes; JarvisOS enforces routing."""

    fast_dev_secret_markers = (
        "api key",
        "apikey",
        "x-api-key",
        ".env",
        "secret_key",
        "private key",
        "authorization:",
        "bearer ",
        "begin private key",
        "scale_way_api_key",
        "scaleway_api_key",
        "openai_api_key",
        "anthropic_api_key",
        "mistral_api_key",
        "deepseek_api_key",
        "aws_secret_access_key",
    )
    fast_dev_secret_patterns = (
        re.compile(r"\b[A-Z0-9_]*API_KEY\s*=", re.IGNORECASE),
        re.compile(r"\b(password|token|access_token|refresh_token)\s*[:=]", re.IGNORECASE),
        re.compile(r"\b(sk|sk-proj|sk-ant|scw)-[A-Za-z0-9_\-]{12,}\b", re.IGNORECASE),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    )
    fast_dev_public_markers = (
        "public research",
        "published",
        "literature",
        "public physics",
        "textbook",
        "hello",
        "ciao",
        "come va",
        "greeting",
    )
    smoke_console_secret_markers = (
        "api key",
        "password",
        ".env",
        "secret_key",
        "token=",
        "token",
        "secret",
        "private key",
        "bearer",
    )
    smoke_console_sensitive_base_markers = (
        "quick coupler",
        "proprietary geometry",
        "patent-like",
        "patent",
        "sensitive design",
        "private strategy",
        "proprietary",
    )
    smoke_console_confidential_base_markers = ("confidential",)
    smoke_console_bypass_markers = (
        "bypass restrictions",
        "ignore previous",
        "ignore the rules",
        "jailbreak",
        "system prompt",
    )
    smoke_console_allowed_exact = {
        "ciao",
        "come va?",
        "hello",
        "hi",
        "say hello in one sentence",
        "reply with a short harmless greeting",
    }
    smoke_console_allowed_greeting_markers = (
        "ciao",
        "come va",
        "hello",
        "greeting",
        "greet",
        "good morning",
        "good evening",
        "buongiorno",
        "buonasera",
    )

    @property
    def smoke_console_sensitive_markers(self) -> tuple[str, ...]:
        return self.smoke_console_sensitive_base_markers + tuple(f"{term} sensitive" for term in sensitivity_terms())

    @property
    def smoke_console_confidential_markers(self) -> tuple[str, ...]:
        return self.smoke_console_confidential_base_markers + sensitivity_terms()

    def classify(self, text: str) -> str:
        lowered = text.lower()
        if any(marker in lowered for marker in ["api key", "password", ".env", "secret_key", "token="]):
            return "secret"
        if any(marker in lowered for marker in ["quick coupler", "proprietary geometry", "patent-like", "patent"]):
            return "sensitive_ip"
        if any(marker in lowered for marker in [*sensitivity_terms(), "brainstorming", "concept sketch"]):
            return "confidential"
        if any(marker in lowered for marker in ["internal note", "engineering note", "rough sizing"]):
            return "internal"
        if any(marker in lowered for marker in ["public research", "published", "literature"]):
            return "public"
        return "unknown"

    def decide_for_external_smoke_test(self, text: str, *, confidential_allowed: bool) -> PrivacyDecision:
        privacy_class = self.classify(text)
        if privacy_class == "secret":
            return PrivacyDecision(privacy_class, False, "privacy_policy_secret_blocked")
        if privacy_class == "sensitive_ip":
            return PrivacyDecision(privacy_class, False, "privacy_policy_sensitive_ip_blocked")
        if privacy_class == "unknown":
            return PrivacyDecision(privacy_class, False, "privacy_policy_unknown_blocked")
        if privacy_class == "confidential" and not confidential_allowed:
            return PrivacyDecision(privacy_class, False, "privacy_policy_confidential_requires_smoke_test")
        return PrivacyDecision(privacy_class, True, None)

    def decide_for_smoke_console(
        self,
        text: str,
        *,
        policy_mode: AIPolicyMode | str = AIPolicyMode.STRICT_IP,
    ) -> PrivacyDecision:
        mode = self._policy_mode(policy_mode)
        normalized = " ".join(text.strip().lower().split())
        if mode == AIPolicyMode.DISABLED:
            return PrivacyDecision("unknown", False, "ai_policy_disabled")
        if self._contains_fast_dev_structural_secret(text):
            return PrivacyDecision("secret", False, "privacy_policy_secret_blocked")
        if any(marker in normalized for marker in self.smoke_console_bypass_markers):
            return PrivacyDecision("unknown", False, "privacy_policy_risky_prompt_blocked")
        if mode == AIPolicyMode.FAST_DEV:
            return PrivacyDecision(self._classify_fast_dev_allowed_text(text), True, None)

        if any(marker in normalized for marker in self.smoke_console_secret_markers):
            return PrivacyDecision("secret", False, "privacy_policy_secret_blocked")
        if any(marker in normalized for marker in self.smoke_console_sensitive_markers):
            return PrivacyDecision("sensitive_ip", False, "privacy_policy_sensitive_ip_blocked")
        if any(marker in normalized for marker in self.smoke_console_confidential_markers):
            return PrivacyDecision("confidential", False, "privacy_policy_confidential_blocked")
        if any(marker in normalized for marker in self.smoke_console_bypass_markers):
            return PrivacyDecision("unknown", False, "privacy_policy_risky_prompt_blocked")

        if self._is_allowed_smoke_console_greeting(normalized):
            return PrivacyDecision("public", True, None)

        base_class = self.classify(text)
        if base_class in {"secret", "sensitive_ip", "confidential", "unknown"}:
            return PrivacyDecision(base_class, False, f"privacy_policy_{base_class}_blocked")
        return PrivacyDecision(base_class, False, "privacy_policy_smoke_console_allowlist_blocked")

    def _is_allowed_smoke_console_greeting(self, normalized_text: str) -> bool:
        if normalized_text in self.smoke_console_allowed_exact:
            return True
        return len(normalized_text) <= 120 and any(
            marker in normalized_text for marker in self.smoke_console_allowed_greeting_markers
        )

    def _policy_mode(self, value: AIPolicyMode | str) -> AIPolicyMode:
        try:
            return AIPolicyMode(str(value))
        except ValueError:
            return AIPolicyMode.STRICT_IP

    def _contains_fast_dev_structural_secret(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        if any(marker in normalized for marker in self.fast_dev_secret_markers):
            return True
        return any(pattern.search(text) for pattern in self.fast_dev_secret_patterns)

    def _classify_fast_dev_allowed_text(self, text: str) -> str:
        normalized = " ".join(text.strip().lower().split())
        if any(marker in normalized for marker in self.fast_dev_public_markers):
            return "public"
        if any(marker in normalized for marker in ["internal note", "engineering note", "rough sizing"]):
            return "internal"
        return "internal"
