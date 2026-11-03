from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual Auto classifier + capability matrix routing eval.")
    parser.add_argument("--fixture", type=Path, default=None)
    args = parser.parse_args(argv)

    from app.core.database import initialize_database
    from app.modules.ai.models import AITaskRunRequest
    from app.modules.ai.routing.bridge import build_auto_decision_bundle
    from app.modules.local_ai_eval.routing_eval import (
        RoutingEvalPrediction,
        agreement_to_dict,
        compute_routing_agreement,
        load_routing_eval_cases,
        render_routing_eval_markdown,
    )

    # The decision bundle requests project context (include_project_context=True),
    # which reads workspace records; ensure the DB exists like the smoke script does.
    initialize_database()

    cases = load_routing_eval_cases(args.fixture) if args.fixture else load_routing_eval_cases()
    predictions: list[RoutingEvalPrediction] = []
    for case in cases:
        bundle = build_auto_decision_bundle(AITaskRunRequest(prompt=case.prompt, route_class="auto", include_project_context=True))
        predictions.append(
            RoutingEvalPrediction(
                id=case.id,
                prompt=case.prompt,
                expected_capability=case.expected_capability,
                actual_capability=bundle.capability,
                expected_context_level=case.expected_context_level,
                actual_context_level=str(bundle.context_decision["context_level"]),
                route_class=bundle.local_route_class,
                classification_source=bundle.classification_result.source.value,
                classification_confidence=bundle.classification_result.classification.confidence,
            )
        )

    generated_at = datetime.now(UTC).isoformat()
    report = {
        "generated_at": generated_at,
        "fixture": str(args.fixture) if args.fixture else "backend/app/modules/local_ai_eval/routing_eval_set.json",
        "agreement": agreement_to_dict(compute_routing_agreement(predictions)),
        "predictions": [item.model_dump() for item in predictions],
    }
    out_dir = REPO_ROOT / "local_eval_reports" / "routing_eval" / generated_at[:10]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "local_routing_eval.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / "local_routing_eval.md").write_text(render_routing_eval_markdown(report), encoding="utf-8")
    print(f"Wrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
