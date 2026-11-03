from app.core.packs import mentions_pack_term
from app.modules.local_ai.classification.adapter import LocalGemmaClassificationAdapter
from app.modules.local_ai.classification.contracts import (
    LOW_CONFIDENCE_THRESHOLD,
    AllowedNextStep,
    ClassificationAdvisoryHints,
    ClassificationAttemptDiagnostics,
    ClassificationFailureCode,
    ClassificationInput,
    ClassificationOutput,
    ClassificationResultSource,
    ClassificationServiceResult,
    ComplexityHint,
    ProjectArea,
    SensitivityHint,
    TaskType,
    make_advisory_hints,
    make_output,
)
from app.modules.local_ai.classification.parser import ClassificationParseError, parse_classification_output
from app.modules.local_ai.classification.prompts import ClassificationPromptError, build_classification_prompt

SENSITIVITY_RANK = {
    SensitivityHint.public: 0,
    SensitivityHint.internal: 1,
    SensitivityHint.confidential: 2,
    SensitivityHint.sensitive_ip: 3,
    SensitivityHint.secret: 4,
    SensitivityHint.unknown: 2,
}
HARD_DETERMINISTIC_TASKS = {TaskType.unsafe_tool_request, TaskType.overbroad_orchestration_request}
HARD_DETERMINISTIC_SENSITIVITY = {SensitivityHint.secret, SensitivityHint.sensitive_ip}


def classify_text(
    request: ClassificationInput,
    *,
    adapter: LocalGemmaClassificationAdapter | None = None,
) -> ClassificationServiceResult:
    deterministic, deterministic_reasons = deterministic_classify(request)
    if _is_hard_deterministic(deterministic):
        return ClassificationServiceResult(
            classification=deterministic,
            advisory_hints=make_advisory_hints(deterministic),
            source=ClassificationResultSource.deterministic,
            model_output_accepted=False,
            deterministic_reasons=deterministic_reasons,
        )
    if adapter is None:
        return ClassificationServiceResult(
            classification=deterministic,
            advisory_hints=make_advisory_hints(deterministic),
            source=ClassificationResultSource.deterministic,
            model_output_accepted=False,
            deterministic_reasons=deterministic_reasons,
        )

    try:
        prompt = build_classification_prompt(request)
    except ClassificationPromptError:
        return _fallback(
            deterministic,
            ClassificationFailureCode.over_budget_prompt,
            deterministic_reasons,
            diagnostics=_diagnostics_without_adapter_response(
                request=request,
                adapter=adapter,
                fallback_reason=ClassificationFailureCode.over_budget_prompt,
            ),
        )

    adapter_result = adapter.complete(prompt, input_chars=len(request.text))
    if not adapter_result.success or adapter_result.response_text is None:
        return _fallback(
            deterministic,
            adapter_result.failure_code or ClassificationFailureCode.unknown,
            deterministic_reasons,
            diagnostics=adapter_result.diagnostics,
        )
    try:
        model_output = parse_classification_output(adapter_result.response_text)
    except ClassificationParseError as exc:
        return _fallback(
            deterministic,
            exc.code,
            deterministic_reasons,
            diagnostics=_with_diagnostics_outcome(adapter_result.diagnostics, schema_valid=False, fallback_reason=exc.code),
        )
    if model_output.confidence < LOW_CONFIDENCE_THRESHOLD:
        return _fallback(
            deterministic,
            ClassificationFailureCode.low_confidence,
            deterministic_reasons,
            diagnostics=_with_diagnostics_outcome(
                adapter_result.diagnostics,
                schema_valid=True,
                fallback_reason=ClassificationFailureCode.low_confidence,
            ),
            advisory_hints=make_advisory_hints(model_output),
        )

    merged, override_reasons = apply_deterministic_overrides(model_output, deterministic)
    source = (
        ClassificationResultSource.model_with_deterministic_override
        if override_reasons
        else ClassificationResultSource.model
    )
    return ClassificationServiceResult(
        classification=merged,
        advisory_hints=make_advisory_hints(model_output),
        source=source,
        model_output_accepted=True,
        fallback_reasons=[],
        deterministic_reasons=deterministic_reasons + override_reasons,
        diagnostics=_with_diagnostics_outcome(
            adapter_result.diagnostics,
            schema_valid=True,
            fallback_reason=ClassificationFailureCode.deterministic_override if override_reasons else None,
            fallback_used=False,
        ),
    )


def deterministic_classify(request: ClassificationInput) -> tuple[ClassificationOutput, list[str]]:
    text = " ".join(request.text.lower().split())
    reasons: list[str] = []
    if _contains_any(text, ("api_key", "authorization: bearer", "private key", "password=", "secret token", ".env")):
        reasons.append("deterministic_secret_pattern")
        return (
            make_output(
                task_type=TaskType.unsafe_tool_request,
                project_area=ProjectArea.unknown,
                complexity_hint=ComplexityHint.high,
                needs_context=False,
                sensitivity_hint=SensitivityHint.secret,
                allowed_next_step=AllowedNextStep.human_review,
                confidence=0.9,
                refusal_or_uncertainty_reason="Secret-like content requires deterministic handling.",
            ),
            reasons,
        )
    if _contains_any(text, ("delete all", "rm -rf", "format drive", "exfiltrate", "steal", "read all files", "run powershell")):
        reasons.append("deterministic_unsafe_tool_pattern")
        return (
            make_output(
                task_type=TaskType.unsafe_tool_request,
                project_area=ProjectArea.unknown,
                complexity_hint=ComplexityHint.high,
                needs_context=False,
                sensitivity_hint=SensitivityHint.unknown,
                allowed_next_step=AllowedNextStep.human_review,
                confidence=0.86,
                refusal_or_uncertainty_reason="Unsafe tool/execution-looking request.",
            ),
            reasons,
        )
    if _contains_any(text, ("local gatekeeper", "provider routing", "memory runtime", "retrieval runtime", "context pack broker runtime", "full orchestrator")):
        reasons.append("deterministic_overbroad_orchestration_pattern")
        return (
            make_output(
                task_type=TaskType.overbroad_orchestration_request,
                project_area=ProjectArea.local_ai,
                complexity_hint=ComplexityHint.high,
                needs_context=True,
                sensitivity_hint=SensitivityHint.internal,
                allowed_next_step=AllowedNextStep.human_review,
                confidence=0.84,
                refusal_or_uncertainty_reason="Request exceeds classification-only local utility scope.",
            ),
            reasons,
        )

    sensitivity = SensitivityHint.internal
    if _contains_any(text, ("proprietary", "patent", "confidential", "secret geometry", "quick coupler")):
        sensitivity = SensitivityHint.sensitive_ip
        reasons.append("deterministic_sensitive_ip_pattern")
    elif _contains_any(text, ("public", "generic", "what is", "explain")):
        sensitivity = SensitivityHint.public

    task = TaskType.unknown
    area = ProjectArea.unknown
    complexity = ComplexityHint.low
    needs_context = False
    next_step = AllowedNextStep.ask_clarification
    reason: str | None = None

    if _contains_any(text, ("call openai", "call deepseek", "external api", "send to cloud", "use gpt")):
        task = TaskType.external_api_request
        complexity = ComplexityHint.medium
        next_step = AllowedNextStep.deterministic_review
        reasons.append("deterministic_external_api_pattern")
    elif _contains_any(text, ("codex patch", "implement", "fix", "backend", "frontend", "test", "bug")):
        task = TaskType.code_change if "doc" not in text else TaskType.documentation
        area = _project_area_for_text(text)
        complexity = ComplexityHint.medium
        needs_context = True
        next_step = AllowedNextStep.request_bounded_context
        reasons.append("deterministic_code_or_patch_pattern")
    elif _contains_any(text, ("readme", "docs", "documentation", "adr", "runbook")):
        task = TaskType.documentation
        area = ProjectArea.documentation
        complexity = ComplexityHint.low
        needs_context = True
        next_step = AllowedNextStep.request_bounded_context
        reasons.append("deterministic_docs_pattern")
    elif mentions_pack_term(text):
        task = TaskType.engineering_question
        area = ProjectArea.project
        complexity = ComplexityHint.high if _contains_any(text, ("model", "simulation", "design")) else ComplexityHint.medium
        needs_context = True
        next_step = AllowedNextStep.human_review if sensitivity == SensitivityHint.sensitive_ip else AllowedNextStep.request_bounded_context
        reasons.append("deterministic_workspace_pattern")
    elif _contains_any(text, ("euler", "equation", "engineering", "physics", "simulation")):
        task = TaskType.engineering_question
        area = ProjectArea.general_engineering
        complexity = ComplexityHint.low
        sensitivity = SensitivityHint.public if sensitivity != SensitivityHint.sensitive_ip else sensitivity
        next_step = AllowedNextStep.answer_locally
        reasons.append("deterministic_general_engineering_pattern")
    elif _contains_any(text, ("note to self", "local-only", "local note")):
        task = TaskType.local_note
        area = ProjectArea.unknown
        next_step = AllowedNextStep.no_action
        reasons.append("deterministic_local_note_pattern")
    elif _contains_any(text, ("what should i", "personal", "my calendar", "my day")):
        task = TaskType.personal_question
        area = ProjectArea.personal
        sensitivity = SensitivityHint.internal
        next_step = AllowedNextStep.answer_locally
        reasons.append("deterministic_personal_pattern")
    elif len(text) < 12 or text in {"continue", "help", "next"}:
        task = TaskType.ambiguous
        complexity = ComplexityHint.unknown
        reason = "Input is too short or ambiguous."
        reasons.append("deterministic_ambiguous_pattern")

    if sensitivity in HARD_DETERMINISTIC_SENSITIVITY:
        next_step = AllowedNextStep.human_review
    if not reasons:
        reasons.append("deterministic_default_unknown")
        reason = "Deterministic classifier could not assign a specific task type."

    return (
        make_output(
            task_type=task,
            project_area=area,
            complexity_hint=complexity,
            needs_context=needs_context,
            sensitivity_hint=sensitivity,
            allowed_next_step=next_step,
            confidence=0.62 if task in {TaskType.unknown, TaskType.ambiguous} else 0.74,
            refusal_or_uncertainty_reason=reason,
        ),
        reasons,
    )


def apply_deterministic_overrides(
    model_output: ClassificationOutput,
    deterministic: ClassificationOutput,
) -> tuple[ClassificationOutput, list[str]]:
    reasons: list[str] = []
    output = model_output.model_copy(deep=True)
    if output.sensitivity_hint != deterministic.sensitivity_hint:
        output.sensitivity_hint = deterministic.sensitivity_hint
        reasons.append("jarvisos_final_sensitivity_policy")
    if output.allowed_next_step != deterministic.allowed_next_step:
        output.allowed_next_step = deterministic.allowed_next_step
        reasons.append("jarvisos_next_step_policy")
    if deterministic.task_type in HARD_DETERMINISTIC_TASKS:
        output.task_type = deterministic.task_type
        reasons.append("deterministic_task_override")
    return output, reasons


def _fallback(
    deterministic: ClassificationOutput,
    reason: ClassificationFailureCode,
    deterministic_reasons: list[str],
    *,
    diagnostics: ClassificationAttemptDiagnostics | None = None,
    advisory_hints: ClassificationAdvisoryHints | None = None,
) -> ClassificationServiceResult:
    classification = deterministic
    if reason == ClassificationFailureCode.low_confidence and deterministic.task_type in {TaskType.unknown, TaskType.ambiguous}:
        classification = deterministic.model_copy(
            update={
                "task_type": TaskType.ambiguous,
                "allowed_next_step": AllowedNextStep.ask_clarification,
                "refusal_or_uncertainty_reason": "Model confidence was too low for advisory classification.",
            }
        )
    return ClassificationServiceResult(
        classification=classification,
        advisory_hints=advisory_hints or make_advisory_hints(classification),
        source=ClassificationResultSource.fallback,
        model_output_accepted=False,
        fallback_reasons=[reason],
        deterministic_reasons=deterministic_reasons,
        diagnostics=diagnostics,
    )


def _with_diagnostics_outcome(
    diagnostics: ClassificationAttemptDiagnostics,
    *,
    schema_valid: bool,
    fallback_reason: ClassificationFailureCode | None,
    fallback_used: bool = True,
) -> ClassificationAttemptDiagnostics:
    return diagnostics.model_copy(
        update={
            "schema_valid": schema_valid,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
        }
    )


def _diagnostics_without_adapter_response(
    *,
    request: ClassificationInput,
    adapter: LocalGemmaClassificationAdapter,
    fallback_reason: ClassificationFailureCode,
) -> ClassificationAttemptDiagnostics:
    return ClassificationAttemptDiagnostics(
        model_name=adapter.config.model_name,
        endpoint=adapter.config.endpoint_url,
        prompt_chars=0,
        input_chars=len(request.text),
        max_output_tokens=adapter.config.max_output_tokens,
        temperature=adapter.config.temperature,
        timeout_seconds=adapter.config.timeout_seconds,
        latency_ms=None,
        raw_content_empty=True,
        thinking_present=None,
        done_reason=None,
        schema_valid=False,
        fallback_used=True,
        fallback_reason=fallback_reason,
    )


def _is_hard_deterministic(output: ClassificationOutput) -> bool:
    return output.task_type in HARD_DETERMINISTIC_TASKS or output.sensitivity_hint in HARD_DETERMINISTIC_SENSITIVITY


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _project_area_for_text(text: str) -> ProjectArea:
    if mentions_pack_term(text):
        return ProjectArea.project
    if "python runner" in text or "runner" in text:
        return ProjectArea.python_runner
    if "local ai" in text or "gemma" in text:
        return ProjectArea.local_ai
    if "doc" in text or "readme" in text:
        return ProjectArea.documentation
    return ProjectArea.jarvisos
