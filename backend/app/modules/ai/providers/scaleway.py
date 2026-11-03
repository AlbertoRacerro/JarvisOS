import os
from dataclasses import dataclass

import httpx

from app.modules.secrets.storage import get_effective_scaleway_api_key


class ScalewayNotConfiguredError(RuntimeError):
    """Raised before any transport handoff when the Scaleway secret is absent."""


@dataclass(frozen=True)
class ScalewayProviderStatus:
    provider: str
    configured: bool
    base_url: str
    model: str
    implementation: str


@dataclass(frozen=True)
class ScalewayChatResult:
    provider_name: str
    model: str
    mode: str
    external_call_attempted: bool
    external_call_succeeded: bool
    response_text: str
    reported_input_tokens: int | None
    reported_output_tokens: int | None
    reported_total_tokens: int | None
    sanitized_metadata: dict[str, object]


class ScalewayProvider:
    name = "scaleway"
    default_base_url = "https://api.scaleway.ai/v1"
    default_model = "llama-3.1-8b-instruct"

    def status(self) -> ScalewayProviderStatus:
        key = get_effective_scaleway_api_key()
        return ScalewayProviderStatus(
            provider=self.name,
            configured=key.key_present,
            base_url=self.base_url(),
            model=self.model(),
            implementation="stub_no_external_calls",
        )

    def classify_smoke_case(self, text: str) -> str:
        raise RuntimeError(
            "Scaleway direct classification is not supported; "
            "use the guarded live smoke completion path."
        )

    def base_url(self) -> str:
        return os.getenv("SCALEWAY_BASE_URL", self.default_base_url).rstrip("/")

    def model(self) -> str:
        """Return the legacy direct-smoke model control.

        Normal routed work receives its model from the validated provider binding
        and does not consult SCALEWAY_MODEL.
        """
        return os.getenv("SCALEWAY_MODEL", self.default_model)

    def chat_completions_url(self) -> str:
        base_url = self.base_url()
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    def create_live_smoke_completion(
        self, *, prompt: str, estimated_output_tokens: int
    ) -> ScalewayChatResult:
        return self._create_chat_completion(
            prompt=prompt,
            estimated_output_tokens=estimated_output_tokens,
            system_prompt=(
                "Classify the harmless synthetic text as public, internal, "
                "confidential, sensitive_ip, secret, or unknown. "
                "Reply with one short sentence."
            ),
            mode="live",
            model_override=None,
        )

    def create_live_console_completion(
        self, *, prompt: str, estimated_output_tokens: int
    ) -> ScalewayChatResult:
        return self._create_chat_completion(
            prompt=prompt,
            estimated_output_tokens=estimated_output_tokens,
            system_prompt=(
                "Reply to this harmless provider smoke-test prompt in one short "
                "sentence. Do not request or process secrets, code, files, "
                "proprietary material, or private strategy."
            ),
            mode="live_smoke_console",
            model_override=None,
        )

    def create_work_completion(
        self,
        *,
        prompt: str,
        estimated_output_tokens: int,
        model: str,
    ) -> ScalewayChatResult:
        if not model.strip():
            raise ValueError("scaleway routed model must be non-empty")
        return self._create_chat_completion(
            prompt=prompt,
            estimated_output_tokens=estimated_output_tokens,
            system_prompt=(
                "You are a precise engineering assistant. "
                "Answer the user's request concisely and usefully."
            ),
            mode="work",
            model_override=model.strip(),
        )

    def _create_chat_completion(
        self,
        *,
        prompt: str,
        estimated_output_tokens: int,
        system_prompt: str,
        mode: str,
        model_override: str | None,
    ) -> ScalewayChatResult:
        secret = get_effective_scaleway_api_key()
        if not secret.value:
            raise ScalewayNotConfiguredError(
                "Scaleway credentials are unavailable before transport handoff."
            )

        model = model_override or self.model()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": estimated_output_tokens,
            "stream": False,
        }

        response = httpx.post(
            self.chat_completions_url(),
            headers={
                "Authorization": f"Bearer {secret.value}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage") or {}

        choices = data.get("choices") or []
        first_choice = choices[0] if choices else {}
        message = first_choice.get("message") or {}
        response_text = str(message.get("content") or "").strip()
        if not response_text:
            raise ValueError("scaleway_empty_completion")

        prompt_tokens = _optional_int(usage.get("prompt_tokens"))
        completion_tokens = _optional_int(usage.get("completion_tokens"))
        total_tokens = _optional_int(usage.get("total_tokens"))

        return ScalewayChatResult(
            provider_name=self.name,
            model=model,
            mode=mode,
            external_call_attempted=True,
            external_call_succeeded=True,
            response_text=response_text,
            reported_input_tokens=prompt_tokens,
            reported_output_tokens=completion_tokens,
            reported_total_tokens=total_tokens,
            sanitized_metadata={
                "implementation": "live_chat_completions",
                "base_url_configured": bool(os.getenv("SCALEWAY_BASE_URL")),
                "usage_returned": bool(usage),
                "finish_reason": first_choice.get("finish_reason"),
            },
        )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
