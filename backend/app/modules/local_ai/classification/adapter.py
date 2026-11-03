from time import perf_counter
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.modules.local_ai.classification.contracts import (
    DEFAULT_CLASSIFICATION_ENDPOINT_URL,
    DEFAULT_CLASSIFICATION_MAX_OUTPUT_TOKENS,
    DEFAULT_CLASSIFICATION_MODEL_NAME,
    DEFAULT_CLASSIFICATION_TEMPERATURE,
    DEFAULT_CLASSIFICATION_TIMEOUT_SECONDS,
    ClassificationAttemptDiagnostics,
    ClassificationFailureCode,
)
from app.modules.local_ai.classification.parser import (
    extract_response_text,
    response_indicates_thinking_budget_exhausted,
)
from app.modules.local_ai.classification.prompts import MAX_CLASSIFICATION_PROMPT_CHARS

DEFAULT_CLASSIFICATION_MODEL = DEFAULT_CLASSIFICATION_MODEL_NAME
DEFAULT_CLASSIFICATION_ENDPOINT = DEFAULT_CLASSIFICATION_ENDPOINT_URL
LOCAL_ENDPOINT_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ClassificationAdapterConfigurationError(ValueError):
    """Raised when a local classification adapter endpoint is unsafe."""


class ClassificationAdapterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_url: str = DEFAULT_CLASSIFICATION_ENDPOINT
    model_name: str = DEFAULT_CLASSIFICATION_MODEL
    timeout_seconds: float = Field(default=DEFAULT_CLASSIFICATION_TIMEOUT_SECONDS, ge=0.1, le=300)
    max_output_tokens: int = Field(default=DEFAULT_CLASSIFICATION_MAX_OUTPUT_TOKENS, ge=1, le=512)
    temperature: float = Field(default=DEFAULT_CLASSIFICATION_TEMPERATURE, ge=0, le=0)

    @field_validator("endpoint_url")
    @classmethod
    def endpoint_must_be_local(cls, value: str) -> str:
        return validate_classification_endpoint_url(value)


class ClassificationAdapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    model_name: str
    runtime_endpoint: str
    diagnostics: ClassificationAttemptDiagnostics
    response_text: str | None = None
    failure_code: ClassificationFailureCode | None = None
    failure_message: str | None = None


class LocalGemmaClassificationAdapter:
    """Local-only adapter boundary for future Gemma classification calls."""

    def __init__(self, config: ClassificationAdapterConfig | None = None, *, client: httpx.Client | None = None) -> None:
        self.config = config or ClassificationAdapterConfig()
        self._client = client

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        started = perf_counter()
        try:
            payload = self._payload(prompt)
        except (ClassificationAdapterConfigurationError, ValidationError) as exc:
            return self._failure(ClassificationFailureCode.invalid_endpoint, str(exc), prompt, input_chars, started)
        except ValueError as exc:
            return self._failure(ClassificationFailureCode.over_budget_prompt, str(exc), prompt, input_chars, started)

        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self.config.timeout_seconds)
        try:
            response = client.post(self.config.endpoint_url, json=payload, timeout=self.config.timeout_seconds)
            response.raise_for_status()
            response_json = response.json()
            response_text = extract_response_text(response_json)
            diagnostics = self._diagnostics(
                prompt=prompt,
                input_chars=input_chars,
                started=started,
                response_json=response_json,
                response_text=response_text,
            )
            if response_indicates_thinking_budget_exhausted(response_json):
                return self._failure(
                    ClassificationFailureCode.thinking_budget_exhausted,
                    "local model exhausted output budget before final content",
                    prompt,
                    input_chars,
                    started,
                    response_json=response_json,
                    response_text=response_text,
                )
            if response_text.strip() == "" and response_json.get("done_reason") == "length":
                return self._failure(
                    ClassificationFailureCode.done_reason_length,
                    "local model reached output budget before final content",
                    prompt,
                    input_chars,
                    started,
                    response_json=response_json,
                    response_text=response_text,
                )
            return ClassificationAdapterResult(
                success=True,
                model_name=self.config.model_name,
                runtime_endpoint=self.config.endpoint_url,
                diagnostics=diagnostics,
                response_text=response_text,
            )
        except httpx.TimeoutException as exc:
            return self._failure(
                ClassificationFailureCode.timeout,
                f"local classification timed out: {type(exc).__name__}",
                prompt,
                input_chars,
                started,
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            return self._failure(
                ClassificationFailureCode.http_error,
                f"unexpected local classification response: {type(exc).__name__}",
                prompt,
                input_chars,
                started,
            )
        finally:
            if owns_client:
                client.close()

    def _payload(self, prompt: str) -> dict[str, object]:
        ClassificationAdapterConfig.model_validate(self.config.model_dump())
        if len(prompt) > MAX_CLASSIFICATION_PROMPT_CHARS:
            raise ValueError("classification prompt exceeds bounded prompt budget")
        parsed = urlparse(self.config.endpoint_url)
        if parsed.path == "/api/chat":
            return {
                "model": self.config.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "options": {"temperature": self.config.temperature, "num_predict": self.config.max_output_tokens},
            }
        return {
            "model": self.config.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }

    def _failure(
        self,
        code: ClassificationFailureCode,
        message: str,
        prompt: str,
        input_chars: int,
        started: float,
        *,
        response_json: dict[str, object] | None = None,
        response_text: str | None = None,
    ) -> ClassificationAdapterResult:
        return ClassificationAdapterResult(
            success=False,
            model_name=self.config.model_name,
            runtime_endpoint=self.config.endpoint_url,
            diagnostics=self._diagnostics(
                prompt=prompt,
                input_chars=input_chars,
                started=started,
                response_json=response_json,
                response_text=response_text,
                fallback_used=True,
                fallback_reason=code,
            ),
            failure_code=code,
            failure_message=message,
        )

    def _diagnostics(
        self,
        *,
        prompt: str,
        input_chars: int,
        started: float,
        response_json: dict[str, object] | None = None,
        response_text: str | None = None,
        schema_valid: bool = False,
        fallback_used: bool = False,
        fallback_reason: ClassificationFailureCode | None = None,
    ) -> ClassificationAttemptDiagnostics:
        message = response_json.get("message") if response_json else None
        thinking_present: bool | None = None
        if isinstance(message, dict) and "thinking" in message:
            thinking_present = bool(message.get("thinking"))
        done_reason = response_json.get("done_reason") if response_json else None
        return ClassificationAttemptDiagnostics(
            model_name=self.config.model_name,
            endpoint=self._diagnostic_endpoint(),
            prompt_chars=len(prompt),
            input_chars=input_chars,
            max_output_tokens=self.config.max_output_tokens,
            temperature=self.config.temperature,
            timeout_seconds=self.config.timeout_seconds,
            latency_ms=max(0, int((perf_counter() - started) * 1000)),
            raw_content_empty=response_text is None or response_text.strip() == "",
            thinking_present=thinking_present,
            done_reason=done_reason if isinstance(done_reason, str) else None,
            schema_valid=schema_valid,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )

    def _diagnostic_endpoint(self) -> str:
        parsed = urlparse(self.config.endpoint_url)
        if not parsed.username and not parsed.password:
            return self.config.endpoint_url
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return parsed._replace(netloc=host).geturl()


def validate_classification_endpoint_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise ClassificationAdapterConfigurationError("classification endpoint must use http:// localhost only")
    if not parsed.hostname or parsed.hostname.lower() not in LOCAL_ENDPOINT_HOSTS:
        raise ClassificationAdapterConfigurationError("classification endpoint host must be localhost, 127.0.0.1, or ::1")
    if parsed.username or parsed.password:
        raise ClassificationAdapterConfigurationError("classification endpoint must not include credentials")
    if not parsed.path:
        raise ClassificationAdapterConfigurationError("classification endpoint must include an HTTP path")
    return url
