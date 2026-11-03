from app.modules.local_ai.classification.contracts import (
    CLASSIFICATION_OUTPUT_SCHEMA_VERSION,
    AllowedNextStep,
    ClassificationInput,
    ComplexityHint,
    ProjectArea,
    SensitivityHint,
    TaskType,
)

MAX_CLASSIFICATION_PROMPT_CHARS = 2000


class ClassificationPromptError(ValueError):
    """Raised when a local classification prompt would exceed its budget."""


def build_classification_prompt(request: ClassificationInput) -> str:
    """Build a bounded JSON-only prompt for advisory classification."""

    prompt = (
        "JSON only;no tools/retrieval/memory/routing/external/state;"
        "noauth:risk,next,perm,prov,tool,mem,retrieval,route,external,final_sens,safety;"
        f"schema_version={CLASSIFICATION_OUTPUT_SCHEMA_VERSION};"
        f"task_type={'|'.join(item.value for item in TaskType)};"
        f"project_area={'|'.join(item.value for item in ProjectArea)};"
        f"complexity_hint={'|'.join(item.value for item in ComplexityHint)};"
        f"sensitivity_hint={'|'.join(item.value for item in SensitivityHint)};"
        f"allowed_next_step={'|'.join(item.value for item in AllowedNextStep)};"
        "needs_context;confidence 0-1;refusal_or_uncertainty_reason"
        f"text={request.text}"
    )
    if len(prompt) > MAX_CLASSIFICATION_PROMPT_CHARS:
        raise ClassificationPromptError("classification prompt exceeds 2000 characters")
    return prompt
