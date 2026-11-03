from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.modules.ai.contracts import (
    AIModelCapability,
    AIPrivacyClass,
    AIProviderError,
    AIProviderErrorCode,
    AIProviderHealth,
    AIRequest,
    AIResponse,
    AIUsage,
    AIUsageSource,
    ModelRegistryEntry,
)
from app.modules.local_ai.runtime.llama_cpp import get_llama_cpp_runtime_owner, llama_cpp_runtime_config

logger = logging.getLogger(__name__)

LOCAL_LLAMACPP_PROVIDER_ID = "local_llamacpp"
_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def _loopback_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


class LocalLlamaCppAdapter:
    provider_id = LOCAL_LLAMACPP_PROVIDER_ID

    def __init__(self, *, client_factory: Any = httpx.Client) -> None:
        self._client_factory = client_factory

    def _base_url(self) -> str:
        url = llama_cpp_runtime_config().base_url
        if not _loopback_url(url):
            raise ValueError("llama-server endpoint must use HTTP loopback")
        return url.rstrip("/")

    def health(self) -> AIProviderHealth:
        try:
            response = self._client_factory().get(f"{self._base_url().removesuffix('/v1')}/health", timeout=1.5)
            return AIProviderHealth.healthy if response.is_success else AIProviderHealth.unavailable
        except Exception:
            return AIProviderHealth.unavailable

    def list_models(self) -> list[ModelRegistryEntry]:
        model_id = llama_cpp_runtime_config().model_id
        return [ModelRegistryEntry(
            model_id=model_id, provider_id=self.provider_id, provider_model_name=model_id,
            display_name=f"llama.cpp {model_id}", enabled=True,
            capabilities={AIModelCapability.chat_text, AIModelCapability.long_context},
            allowed_privacy_classes={AIPrivacyClass.public, AIPrivacyClass.internal},
            notes="Local llama-server over its loopback OpenAI-compatible API.",
        )]

    def complete(self, request: AIRequest) -> AIResponse:
        base_url = self._base_url()
        model_id = request.model_preference or llama_cpp_runtime_config().model_id
        # Same precedence as the Ollama route: the assembled prompt carries the Jarvis envelope.
        if request.prompt is not None:
            content: Any = request.prompt
            if request.images and getattr(llama_cpp_runtime_config(), "vision", False):
                # OpenAI-compatible multimodal parts; needs a llama-server started with --mmproj.
                content = [{"type": "text", "text": request.prompt},
                           *({"type": "image_url", "image_url": {"url": image}} for image in request.images)]
            messages = [{"role": "user", "content": content}]
        else:
            messages = [message.model_dump() for message in request.messages]
        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": 0,
            "max_tokens": request.max_output_tokens or 2048,
            "stream": False,
        }
        schema_enabled = os.getenv("JARVIS_LLAMA_JSON_SCHEMA", "true").lower() not in {"0", "false", "no", "off"}
        if request.structured_output_schema is not None and schema_enabled:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "jarvis_agent_response", "strict": True,
                "schema": request.structured_output_schema,
            }}
        config = llama_cpp_runtime_config()
        if config.thinking in {"on", "off"}:
            payload["chat_template_kwargs"] = {"enable_thinking": config.thinking == "on"}
        client = self._client_factory()
        schema_fallback = False
        try:
            request_url = f"{base_url}/chat/completions"
            headers = get_llama_cpp_runtime_owner().auth_headers()
            timeout = llama_cpp_runtime_config().request_timeout_s
            response = client.post(request_url, json=payload, headers=headers, timeout=timeout)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if not _is_schema_rejection(exc):
                    raise
                logger.warning("llama-server rejected JSON schema; retrying once without response_format")
                retry_payload = dict(payload)
                retry_payload.pop("response_format", None)
                response = client.post(request_url, json=retry_payload, headers=headers, timeout=timeout)
                response.raise_for_status()
                schema_fallback = True
            body_raw = response.json()
            if not isinstance(body_raw, dict):
                raise ValueError("llama-server response must be an object")
            body: dict[str, Any] = body_raw
        except Exception as exc:
            # Bounded cause only (HTTP status or exception class); never prompt or body text.
            cause = (f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError)
                     else type(exc).__name__)
            logger.warning("llama-server request failed: %s", cause)
            recovery_in_progress = False
            if isinstance(exc, httpx.RequestError):
                recovery = get_llama_cpp_runtime_owner().recover_if_crashed()
                recovery_in_progress = recovery.get("reason_code") == "LLAMACPP_RECOVERING"
            failure_message = f"Local llama-server request failed ({cause})."
            if recovery_in_progress:
                failure_message += " Automatic recovery is in progress."
            return AIResponse(
                provider_id=self.provider_id, model_id=model_id, request_id=request.request_id,
                correlation_id=request.correlation_id, finish_reason="error", safety_status="allowed",
                usage=AIUsage(provider_id=self.provider_id, model_id=model_id),
                error=AIProviderError(code=AIProviderErrorCode.provider_unavailable,
                                      message=failure_message, retryable=True),
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        choices = body.get("choices")
        choice: dict[str, Any] = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message_raw = choice.get("message")
        message: dict[str, Any] = message_raw if isinstance(message_raw, dict) else {}
        content_raw = message.get("content")
        reasoning_raw = message.get("reasoning_content")
        content = content_raw if isinstance(content_raw, str) else ""
        reasoning = reasoning_raw if isinstance(reasoning_raw, str) else ""
        think = "".join(_THINK_RE.findall(content))
        visible = _THINK_RE.sub("", content).strip()
        usage_value = body.get("usage")
        usage_raw: dict[str, Any] = usage_value if isinstance(usage_value, dict) else {}
        reasoning_tokens = _positive_int(usage_raw.get("reasoning_tokens"))
        prompt_tokens = _positive_int(usage_raw.get("prompt_tokens"))
        completion_tokens = _positive_int(usage_raw.get("completion_tokens"))
        timings_value = body.get("timings")
        timings: dict[str, Any] = timings_value if isinstance(timings_value, dict) else {}
        prompt_tokens = prompt_tokens or _positive_int(timings.get("prompt_n"))
        completion_tokens = completion_tokens or _positive_int(timings.get("predicted_n"))
        if completion_tokens and reasoning_tokens:
            completion_tokens = max(0, completion_tokens - reasoning_tokens)
        elif reasoning_tokens == 0:
            details = usage_raw.get("completion_tokens_details")
            reasoning_tokens = _positive_int(details.get("reasoning_tokens")) if isinstance(details, dict) else 0
            completion_tokens = max(0, completion_tokens - reasoning_tokens)
        finish = choice.get("finish_reason")
        finish = finish if finish in {"stop", "length"} else "error" if finish in {"error", "content_filter"} else None
        total = prompt_tokens + completion_tokens + reasoning_tokens
        return AIResponse(
            provider_id=self.provider_id, model_id=model_id, request_id=request.request_id,
            correlation_id=request.correlation_id, text=visible, content=visible,
            usage=AIUsage(provider_id=self.provider_id, model_id=model_id,
                          input_tokens=prompt_tokens, output_tokens=completion_tokens + reasoning_tokens,
                          total_tokens=total if total else None,
                          usage_source=AIUsageSource.actual if usage_raw or timings else AIUsageSource.estimated),
            finish_reason=finish, safety_status="allowed",
            raw_provider_metadata={"reasoning_char_count": len(reasoning or think),
                                   "reasoning_tokens": reasoning_tokens,
                                   "timings": timings,
                                   "json_schema_fallback": schema_fallback},
            error=AIProviderError(code=AIProviderErrorCode.provider_response_invalid,
                                  message="llama-server reported an inference error.", retryable=False)
            if finish == "error" else None,
        )

    def stream(self, request: AIRequest) -> object:
        raise NotImplementedError("Local llama.cpp adapter streaming is not implemented.")


def _positive_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _is_schema_rejection(exc: httpx.HTTPStatusError) -> bool:
    response = exc.response
    if response.status_code not in {400, 500}:
        return False
    try:
        body = response.text.lower()
    except Exception:
        return False
    return "response_format" in body or "grammar" in body or "schema" in body
