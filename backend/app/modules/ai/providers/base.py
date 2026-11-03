from typing import Protocol

from app.modules.ai.models import ModelingDraft, ModelingDraftRequest


class AIRequest:
    def __init__(self, *, task_type: str, quality_level: str, draft_request: ModelingDraftRequest) -> None:
        self.task_type = task_type
        self.quality_level = quality_level
        self.draft_request = draft_request


class AIResponse:
    def __init__(self, *, draft: ModelingDraft, provider: str, model: str) -> None:
        self.draft = draft
        self.provider = provider
        self.model = model


class AIProvider(Protocol):
    name: str

    def generate(self, request: AIRequest) -> AIResponse:
        """Generate a response through a concrete provider implementation."""
