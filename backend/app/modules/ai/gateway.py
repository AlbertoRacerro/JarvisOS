import time

from app.core.database import open_sqlite_connection
from app.core.packs import default_workspace_slug
from app.modules.ai.budget import evaluate_ai_status
from app.modules.ai.execution import resolve_binding, resolve_effective_route_class
from app.modules.ai.models import (
    AIMetadata,
    AITaskRunRequest,
    AITaskRunResponse,
    ModelingDraftRequest,
    ModelingDraftResponse,
    ProviderSmokeRequest,
    ProviderSmokeResponse,
    SmokeConsoleRequest,
    SmokeConsoleResponse,
    SmokeTestRequest,
    SmokeTestResponse,
    SupervisorPublicTestRequest,
    SupervisorPublicTestResponse,
)
from app.modules.ai.providers.base import AIRequest
from app.modules.ai.providers.fake import FakeModelingProvider
from app.modules.ai.settings import get_ai_settings
from app.modules.events.service import log_event
from app.modules.workspaces.service import get_workspace

TASK_MODELING_DRAFT = "modeling_draft"


class AIGateway:
    """All AI-assisted modeling calls pass through this guarded gateway."""

    def __init__(self) -> None:
        self.fake_provider = FakeModelingProvider()

    def status(self, provider_mode: str | None = None):
        return evaluate_ai_status(get_ai_settings(), provider_mode)

    def run_smoke_tests(self, request: SmokeTestRequest) -> SmokeTestResponse:
        from app.modules.ai.smoke_tests import run_smoke_tests

        return run_smoke_tests(provider_mode=request.provider_mode, smoke_mode=request.smoke_mode)

    def run_smoke_console(self, request: SmokeConsoleRequest) -> SmokeConsoleResponse:
        from app.modules.ai.smoke_console import run_smoke_console

        return run_smoke_console(request)

    def run_provider_smoke(self, request: ProviderSmokeRequest) -> ProviderSmokeResponse:
        from app.modules.ai import deepseek_provider_smoke

        return deepseek_provider_smoke.run_provider_smoke(request)

    def run_supervisor_public_test(self, request: SupervisorPublicTestRequest) -> SupervisorPublicTestResponse:
        from app.modules.ai import supervisor_public_test

        return supervisor_public_test.run_supervisor_public_test(request)

    def run_task(self, request: AITaskRunRequest) -> AITaskRunResponse:
        import json as _json

        from app.modules.ai.context_builder import (
            DEFAULT_CONTEXT_BUDGET_CHARS,
            build_workspace_context_bundle,
        )
        from app.modules.ai.execution import run_ai_task

        selected_route_class = resolve_effective_route_class(
            task_kind=request.task_kind,
            route_class=request.route_class,
        )
        external_blocked_reason = None
        requested_workspace_id = (request.workspace_id or default_workspace_slug()) if request.include_project_context else None

        if selected_route_class == "auto":
            from app.modules.ai.routing.bridge import run_auto_task

            return run_auto_task(request)

        if selected_route_class.startswith("external:"):
            settings = get_ai_settings()
            binding, _decision = resolve_binding(selected_route_class)
            provider_mode = binding.provider_id if binding is not None else selected_route_class
            status = evaluate_ai_status(settings, provider_mode)
            if not status.external_calls_allowed:
                external_blocked_reason = status.blocking_reason or "external_calls_disabled"

        manual_blocks = list(request.context_blocks or [])
        context_blocks: list[dict] = manual_blocks
        context_build_error = None
        if request.include_project_context:
            manual_len = (
                len(_json.dumps(manual_blocks, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
                if manual_blocks
                else 0
            )
            # Budget-aware: build workspace context only within the budget left after
            # the manual blocks, so opt-in context degrades (drops blocks) instead of
            # making a previously-working task fail closed on the spine budget.
            remaining = max(0, DEFAULT_CONTEXT_BUDGET_CHARS - manual_len)
            try:
                bundle = build_workspace_context_bundle(requested_workspace_id or default_workspace_slug(), budget_chars=remaining)
            except Exception as exc:  # workspace missing / build failure: fail closed
                context_build_error = f"workspace_context_build_failed: {type(exc).__name__}"
            else:
                # manual/explicit context first, workspace context after.
                context_blocks = manual_blocks + bundle.blocks

        outcome = run_ai_task(
            user_prompt=request.prompt,
            task_kind=request.task_kind,
            route_class=selected_route_class,
            context_blocks=context_blocks,
            max_output_tokens=request.max_tokens,
            external_blocked_reason=external_blocked_reason,
            context_build_error=context_build_error,
            workspace_id=requested_workspace_id,
        )
        response = outcome.response
        return AITaskRunResponse(
            status=outcome.status,
            ledger_id=outcome.ledger_id,
            selected_route_class=outcome.selected_route_class,
            decision_reason=outcome.decision.decision_reason,
            blocked_reason=outcome.decision.blocked_reason,
            response_text=response.text if response is not None else None,
            provider_id=response.provider_id if response is not None else outcome.decision.provider_id,
            model_id=response.model_id if response is not None else outcome.decision.model_id,
            usage=response.usage if response is not None else None,
            error_type=outcome.error_type,
            include_project_context=request.include_project_context,
            workspace_id=requested_workspace_id,
            context_digest=outcome.context_digest,
            context_sources_count=outcome.context_sources_count,
            records_parse_error=outcome.records_parse_error,
            proposed_record_ids=outcome.proposed_record_ids or [],
        )

    def create_modeling_draft(self, request: ModelingDraftRequest) -> ModelingDraftResponse:
        workspace = get_workspace(request.workspace_id)
        if workspace is None:
            return self._blocked_response(
                request=request,
                provider="none",
                model="none",
                provider_mode=request.provider_mode or get_ai_settings().provider_mode,
                reason="workspace_not_found",
                blocked_by_budget=False,
                event_type="AIModelingDraftFailed",
            )

        settings = get_ai_settings()
        provider_mode = request.provider_mode or settings.provider_mode
        status = evaluate_ai_status(settings, provider_mode)

        self._log_attempt(
            request,
            event_type="AIModelingDraftRequested",
            provider=status.default_ai_provider,
            model=status.default_ai_model,
            provider_mode=provider_mode,
            blocked_by_budget=False,
        )

        if provider_mode != "fake":
            if provider_mode == "scaleway" and status.blocking_reason in {
                "monthly_budget_zero",
                "monthly_budget_exhausted",
                "paid_ai_disabled",
            } and settings.use_fake_provider_when_budget_zero:
                return self._run_fake_provider(request, provider_mode="fake")

            self._log_attempt(
                request,
                event_type="AIModelingDraftBlockedByBudget",
                provider=provider_mode,
                model=settings.default_ai_model,
                provider_mode=provider_mode,
                blocked_by_budget=status.blocking_reason in {"monthly_budget_zero", "monthly_budget_exhausted"},
                reason=status.blocking_reason,
            )
            return self._blocked_response(
                request=request,
                provider=provider_mode,
                model=settings.default_ai_model,
                provider_mode=provider_mode,
                reason=status.blocking_reason or "external_provider_blocked",
                blocked_by_budget=status.blocking_reason in {"monthly_budget_zero", "monthly_budget_exhausted"},
                event_type=None,
            )

        return self._run_fake_provider(request, provider_mode="fake")

    def _run_fake_provider(self, request: ModelingDraftRequest, provider_mode: str) -> ModelingDraftResponse:
        settings = get_ai_settings()
        started = time.perf_counter()
        ai_request = AIRequest(
            task_type=TASK_MODELING_DRAFT,
            quality_level=request.quality_level,
            draft_request=request,
        )
        response = self.fake_provider.generate(ai_request)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)

        self._log_attempt(
            request,
            event_type="AIModelingDraftCompleted",
            provider=response.provider,
            model=response.model,
            provider_mode=provider_mode,
            blocked_by_budget=False,
            extra={"latency_ms": latency_ms},
        )

        return ModelingDraftResponse(
            draft=response.draft,
            ai_metadata=AIMetadata(
                provider=response.provider,
                model=response.model,
                provider_mode=provider_mode,
                task_type=TASK_MODELING_DRAFT,
                quality_level=request.quality_level,
                paid_api_call_attempted=False,
                blocked_by_budget=False,
                estimated_cost_usd=0,
                monthly_budget_usd=settings.monthly_api_budget_usd,
                spend_month_to_date_usd=settings.api_spend_month_to_date_usd,
                success=True,
            ),
        )

    def _blocked_response(
        self,
        *,
        request: ModelingDraftRequest,
        provider: str,
        model: str,
        provider_mode: str,
        reason: str,
        blocked_by_budget: bool,
        event_type: str | None,
    ) -> ModelingDraftResponse:
        settings = get_ai_settings()
        if event_type:
            self._log_attempt(
                request,
                event_type=event_type,
                provider=provider,
                model=model,
                provider_mode=provider_mode,
                blocked_by_budget=blocked_by_budget,
                reason=reason,
            )
        return ModelingDraftResponse(
            draft=None,
            ai_metadata=AIMetadata(
                provider=provider,
                model=model,
                provider_mode=provider_mode,
                task_type=TASK_MODELING_DRAFT,
                quality_level=request.quality_level,
                paid_api_call_attempted=False,
                blocked_by_budget=blocked_by_budget,
                blocked_reason=reason,
                estimated_cost_usd=None,
                monthly_budget_usd=settings.monthly_api_budget_usd,
                spend_month_to_date_usd=settings.api_spend_month_to_date_usd,
                success=False,
            ),
        )

    def _log_attempt(
        self,
        request: ModelingDraftRequest,
        *,
        event_type: str,
        provider: str,
        model: str,
        provider_mode: str,
        blocked_by_budget: bool,
        reason: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        payload = {
            "provider_mode": provider_mode,
            "provider": provider,
            "model": model,
            "blocked_by_budget": blocked_by_budget,
            "workspace_id": request.workspace_id,
            "task_type": TASK_MODELING_DRAFT,
            "reason": reason,
            "estimated_cost_usd": 0 if provider == "fake" else None,
        }
        payload.update(extra or {})
        with open_sqlite_connection() as connection:
            log_event(
                connection,
                event_type=event_type,
                actor="local-user",
                target_type="AIModelingDraft",
                target_id=None,
                workspace_id=request.workspace_id,
                payload=payload,
            )
            connection.commit()
