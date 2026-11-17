"""Offline model fixture for the Literature browser walk; never used by the product."""

from __future__ import annotations

import json
import os

from app.modules.ai.contracts import AIRequest, AIResponse, AIUsage
from app.modules.memory import literature_ingest
import uvicorn


class _WalkAdapter:
    provider_id = "fake"

    def complete(self, request: AIRequest) -> AIResponse:
        values = []
        if "The solubility of CO2 = 0.8 g/L at 25 K." in request.prompt:
            values = [
                {"quantity": "solubility of CO2", "value_text": "0.8", "unit": "g/L",
                 "conditions": [{"name": "temperature", "value_text": "25", "unit": "K"}],
                 "page": 2, "quote": "The solubility of CO2 = 0.8 g/L at 25 K."},
                {"quantity": "solubility", "value_text": "3.4", "unit": "g/L", "conditions": [],
                 "page": 2, "quote": "Table 2: run A 1.2 3.4 5.6"},
            ]
        body = json.dumps({"values": values})
        return AIResponse(provider_id="fake", model_id="walk-fixture", request_id=request.request_id,
                          text=body, content=body,
                          usage=AIUsage(provider_id="fake", model_id="walk-fixture", input_tokens=1, output_tokens=1),
                          finish_reason="stop", safety_status="allowed")


literature_ingest.INGEST_ROUTE_CLASS = "local:fake"
literature_ingest._ADAPTERS = {"fake": _WalkAdapter()}

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ["JARVISOS_PROOF_PORT"]), log_level="info")
