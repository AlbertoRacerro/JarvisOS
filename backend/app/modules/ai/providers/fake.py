from app.core.packs import sensitivity_terms
from app.modules.ai.models import ModelingDraft
from app.modules.ai.providers.base import AIProvider, AIRequest, AIResponse


class FakeModelingProvider(AIProvider):
    name = "fake"
    model = "fake-modeling-draft-v1"

    def generate(self, request: AIRequest) -> AIResponse:
        idea = request.draft_request.informal_model_idea.strip()
        title_seed = idea.split(".")[0][:72].strip() or "Engineering model draft"

        draft = ModelingDraft(
            engineering_question=f"What simplified engineering model would test: {title_seed}?",
            model_title_suggestion=f"Draft model: {title_seed}",
            model_scope="Scratch co-engineering draft for early review; not validated engineering work.",
            proposed_assumptions=[
                "Use first-order relationships before adding detailed physics.",
                "Treat uncertain values as draft assumptions with explicit sources later.",
                "Keep units visible for every parameter before running simulations.",
            ],
            proposed_parameters=[
                "primary_length_scale",
                "operating_environment",
                "material_or_system_capacity",
                "safety_margin",
            ],
            expected_inputs=[
                "geometry or system size",
                "environmental conditions",
                "candidate parameter values",
            ],
            expected_outputs=[
                "feasibility indicator",
                "sensitivity drivers",
                "draft margin or risk estimate",
            ],
            missing_information=[
                "target operating envelope",
                "validated parameter sources",
                "acceptance criteria for a useful result",
            ],
            model_weaknesses=[
                "Fake-provider draft is deterministic and not a substitute for expert review.",
                "No external literature or project context was retrieved.",
                "No simulation or numerical validation was performed.",
            ],
            suggested_next_step="Save reviewed assumptions and parameters manually before any future run workflow.",
        )
        return AIResponse(draft=draft, provider=self.name, model=self.model)

    def classify_smoke_case(self, text: str) -> str:
        lowered = text.lower()
        if any(marker in lowered for marker in ["api key", "password", ".env", "secret_key", "token="]):
            return "secret"
        if any(marker in lowered for marker in ["quick coupler", "proprietary geometry", "patent-like", "patent"]):
            return "sensitive_ip"
        if any(marker in lowered for marker in [*sensitivity_terms(), "brainstorming"]):
            return "confidential"
        if "engineering note" in lowered:
            return "internal"
        return "public"
