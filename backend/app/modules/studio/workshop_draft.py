"""Proposal-only Workshop drafts (spec 214): stateless, no Studio or model-spec writes.

The contract always comes from the operator's answers. The AI route may only propose Python source and an
explanation; its output is validated like any other Studio proposal and, when unavailable or refused, a
deterministic scaffold built from the answers is returned instead. Nothing here executes drafted code.
"""

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.modules.ai.execution import run_ai_task
from app.modules.studio import geometry, service
from app.modules.studio.models import StudioError
from app.modules.studio.unit_models import unit_scaffold

_SCHEMA: dict[str, object] = {
    "type": "object", "additionalProperties": False, "required": ["source", "explanation"],
    "properties": {"source": {"type": "string", "maxLength": 65536}, "explanation": {"type": "string", "maxLength": 4000}},
}
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class WorkshopDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["calculation", "process_unit", "geometry", "procedure"]
    step: int = Field(default=0, ge=0, le=6)
    name: str = Field(default="", max_length=160)
    engineering_question: str = Field(default="", max_length=2000)
    approach: str = Field(default="", max_length=4000)
    assumptions: str = Field(default="", max_length=4000)
    test_intent: str = Field(default="", max_length=4000)
    inputs: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    outputs: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    inlets: list[dict[str, Any]] = Field(default_factory=list, max_length=4)
    outlets: list[dict[str, Any]] = Field(default_factory=list, max_length=4)
    parameters: list[dict[str, Any]] = Field(default_factory=list, max_length=32)


def _clean(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if value is not None} for item in items]


def _contract(data: WorkshopDraftRequest) -> dict[str, Any]:
    if data.category == "calculation":
        return {"inputs": _clean(data.inputs), "outputs": _clean(data.outputs)}
    if data.category == "geometry":
        return {"inputs": _clean(data.inputs)}
    return {"inlets": _clean(data.inlets), "outlets": _clean(data.outlets), "parameters": _clean(data.parameters),
            "symbol": None, "culture_generation": []}


def _scaffold(category: str, contract: dict[str, Any]) -> str:
    if category == "geometry":
        return geometry.geometry_scaffold(contract)
    return unit_scaffold(contract) if category == "process_unit" else service.scaffold(contract)


def _explanation(data: WorkshopDraftRequest) -> str:
    parts = [f"Question: {data.engineering_question.strip()}" if data.engineering_question.strip() else "",
             f"Approach: {data.approach.strip()}" if data.approach.strip() else "",
             f"Assumptions: {data.assumptions.strip()}" if data.assumptions.strip() else "",
             f"Test intent: {data.test_intent.strip()}" if data.test_intent.strip() else ""]
    return "\n".join(part for part in parts if part) or "Answer the questions to grow this draft."


def _prompt(data: WorkshopDraftRequest, contract: dict[str, Any], scaffold: str) -> str:
    return ("Draft Python for a JarvisOS Studio model. Reply with one JSON object {\"source\": string, \"explanation\": "
            "string} and nothing else. Keep the function names, signatures and `main` from the scaffold; do not change "
            "the declared contract; use only the standard library (build123d for geometry); no I/O, no network.\n"
            f"Name: {data.name}\nQuestion: {data.engineering_question}\nApproach: {data.approach}\n"
            f"Assumptions: {data.assumptions}\nTest intent: {data.test_intent}\n"
            f"Contract: {json.dumps(contract, sort_keys=True)}\nScaffold:\n{scaffold}")


def _parse(text: str) -> tuple[str, str]:
    body = text.strip()
    fenced = _FENCE.match(body)
    value = json.loads(fenced.group(1) if fenced else body)
    if (not isinstance(value, dict) or not isinstance(value.get("source"), str)
            or not isinstance(value.get("explanation"), str)):
        raise ValueError("AI output does not match the draft schema.")
    return value["source"], value["explanation"]


def draft(workspace_id: str, data: WorkshopDraftRequest) -> dict[str, Any]:
    result: dict[str, Any] = {"step": data.step, "category": data.category, "origin": "deterministic", "reason": None,
                              "contract": None, "contract_ready": False, "source": "", "acceptable": False,
                              "explanation": _explanation(data), "ai_job_id": None}
    if data.category == "procedure":
        result["reason"] = "Procedures compile from typed steps and have no Python source."
        return result
    name, question = data.name.strip() or "Draft", data.engineering_question.strip() or "Draft question"
    try:
        contract = service.validate_proposed_source(data.category, name, question, _contract(data),
                                                    "def main(inputs):\n    return {}\n")
    except (StudioError, ValidationError, ValueError, TypeError) as exc:
        result["reason"] = "The typed contract is not complete yet: " + (
            exc.message if isinstance(exc, StudioError) else "inputs, outputs or units need attention") + "."
        return result
    scaffold = _scaffold(data.category, contract)
    result.update(contract=contract, contract_ready=True, source=scaffold, acceptable=True)
    if not (data.name.strip() and data.engineering_question.strip() and data.approach.strip()):
        result["reason"] = "AI drafting starts once the name, question and approach are answered."
        return result
    try:
        outcome = run_ai_task(user_prompt=_prompt(data, contract, scaffold), task_kind="general",
                              max_output_tokens=2048, structured_output_schema=_SCHEMA, workspace_id=workspace_id)
    except Exception as exc:  # noqa: BLE001 - any AI-route failure degrades to the deterministic draft.
        result["reason"] = f"AI route failed ({type(exc).__name__})."
        return result
    result["ai_job_id"] = outcome.ledger_id
    if outcome.status != "success" or outcome.response is None or not outcome.response.text:
        reason = outcome.decision.blocked_reason or outcome.error_type or outcome.status
        result["reason"] = f"AI route unavailable ({reason})."
        return result
    try:
        source, explanation = _parse(outcome.response.text)
        service.validate_proposed_source(data.category, name, question, _contract(data), source)
    except StudioError as exc:
        result["reason"] = f"AI output refused: {exc.message}"
        return result
    except (ValueError, ValidationError):
        result["reason"] = "AI output refused: it was not a valid draft."
        return result
    result.update(origin="ai", reason=None, source=source, explanation=explanation.strip() or result["explanation"])
    return result
