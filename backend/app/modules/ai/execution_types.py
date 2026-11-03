from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExecutionClass = Literal["synthetic", "local_compute", "external_provider"]


@dataclass(frozen=True)
class ProviderBinding:
    route_class: str
    provider_id: str
    model_id: str
    requires_network: bool
    max_output_tokens: int
    execution_class: ExecutionClass | None = None
    context_window_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.execution_class is not None and self.context_window_tokens is not None:
            return
        try:
            from app.modules.ai.provider_registry import load_default_provider_registry

            registry = load_default_provider_registry()
        except (ImportError, ValueError):
            return
        provider = registry.providers.get(self.provider_id)
        model = registry.models.get((self.provider_id, self.model_id))
        if provider is None or model is None or not provider.enabled:
            return
        if self.route_class not in model.route_classes:
            return
        if self.execution_class is None:
            object.__setattr__(self, "execution_class", provider.execution_class)
        if self.context_window_tokens is None:
            object.__setattr__(self, "context_window_tokens", model.context_window_tokens)
