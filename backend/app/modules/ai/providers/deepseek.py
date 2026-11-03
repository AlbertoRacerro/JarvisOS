import os
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class DeepSeekProviderStatus:
    provider: str
    configured: bool
    base_url: str
    model: str
    implementation: str


@dataclass(frozen=True)
class DeepSeekChatResult:
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


class DeepSeekProvider:
    name = "deepseek"
    default_base_url = "https://api.deepseek.com/v1"
    default_model = "deepseek-chat"

    def status(self) -> DeepSeekProviderStatus:
        return DeepSeekProviderStatus(
            provider=self.name,
            configured=bool(self.api_key()),
            base_url=self.base_url(),
            model=self.model(),
            implementation="openai_compatible_chat_completions",
        )

    def api_key(self) -> str | None:
        value = os.getenv("DEEPSEEK_API_KEY")
        return value.strip() if value and value.strip() else None

    def base_url(self) -> str:
        return os.getenv("DEEPSEEK_BASE_URL", self.default_base_url).rstrip("/")

    def model(self) -> str:
        return os.getenv("DEEPSEEK_MODEL", self.default_model)

    def chat_completions_url(self) -> str:
        base_url = self.base_url()
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    def create_live_console_completion(self, *, prompt: str, estimated_output_tokens: int) -> DeepSeekChatResult:
        return self._create_chat_completion(
            prompt=prompt,
            estimated_output_tokens=estimated_output_tokens,
            system_prompt=(
                "Reply to this public/internal JarvisOS provider smoke-test prompt clearly and concisely. "
                "Do not request or process API keys, Authorization headers, .env files, private keys, or secrets."
            ),
            mode="strong_provider_smoke",
        )

    def _create_chat_completion(
        self,
        *,
        prompt: str,
        estimated_output_tokens: int,
        system_prompt: str,
        mode: str,
    ) -> DeepSeekChatResult:
        key = self.api_key()
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY is required for DeepSeek smoke calls.")

        model = self.model()
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
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        first_choice = choices[0] if choices else {}
        message = first_choice.get("message") or {}
        response_text = str(message.get("content") or "").strip()

        prompt_tokens = _optional_int(usage.get("prompt_tokens"))
        completion_tokens = _optional_int(usage.get("completion_tokens"))
        total_tokens = _optional_int(usage.get("total_tokens"))

        return DeepSeekChatResult(
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
                "implementation": "openai_compatible_chat_completions",
                "base_url_configured": bool(os.getenv("DEEPSEEK_BASE_URL")),
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
