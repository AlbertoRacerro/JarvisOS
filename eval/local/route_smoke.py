from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

ROUTES = ("local:fast", "local:general", "local:coder", "local:coder_heavy")
PROMPTS = (
    "Reply with exactly one short sentence explaining caching.",
    "List two generic risks in a database migration.",
    "Write a tiny Python function that returns the larger of two numbers.",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual local route smoke through run_ai_task; requires Ollama.")
    parser.add_argument("--confirm-live-local", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=96)
    args = parser.parse_args(argv)
    if not args.confirm_live_local:
        print("Refusing to call local models without --confirm-live-local.", file=sys.stderr)
        return 2

    from app.core.database import initialize_database
    from app.modules.ai.execution import _default_bindings, run_ai_task
    from app.modules.local_ai_eval.routing_eval import render_local_route_smoke_markdown

    initialize_database()
    bindings = _default_bindings()
    generated_at = datetime.now(UTC).isoformat()
    report = {
        "generated_at": generated_at,
        "routes_resolved": {route: bindings[route].model_id for route in ROUTES},
        "routes": [],
        "swap_sequence": [],
    }
    try:
        for route in ROUTES:
            calls = [_call(run_ai_task, route, prompt, args.max_tokens) for prompt in PROMPTS]
            report["routes"].append({"route_class": route, "model_id": bindings[route].model_id, "calls": calls})
        for route in ("local:fast", "local:general", "local:fast"):
            report["swap_sequence"].append(_call(run_ai_task, route, "Reply with the word ready.", args.max_tokens))
    except Exception as exc:
        print(f"Local route smoke failed before report write: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    failed = [call for route in report["routes"] for call in route["calls"] if call["status"] != "success"]
    failed.extend(call for call in report["swap_sequence"] if call["status"] != "success")
    if failed:
        first = failed[0]
        print(
            f"Local route smoke failed for {first['route_class']}: {first['status']} {first.get('error_type') or ''}. "
            "Check that Ollama is running and required models are present; this script never pulls models.",
            file=sys.stderr,
        )
        return 1

    out_dir = REPO_ROOT / "local_eval_reports" / "route_smoke" / generated_at[:10]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "local_route_smoke.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / "local_route_smoke.md").write_text(render_local_route_smoke_markdown(report), encoding="utf-8")
    print(f"Wrote {out_dir}")
    return 0


def _call(run_ai_task, route: str, prompt: str, max_tokens: int) -> dict:
    started = time.perf_counter()
    outcome = run_ai_task(user_prompt=prompt, route_class=route, max_output_tokens=max_tokens)
    wall_ms = round((time.perf_counter() - started) * 1000)
    usage = outcome.response.usage.model_dump() if outcome.response is not None else None
    return {
        "route_class": route,
        "prompt": prompt,
        "status": outcome.status,
        "ledger_id": outcome.ledger_id,
        "wall_ms": wall_ms,
        "model_id": outcome.response.model_id if outcome.response is not None else None,
        "provider_id": outcome.response.provider_id if outcome.response is not None else None,
        "usage": usage,
        "output_sane": bool(outcome.response and outcome.response.text and outcome.response.text.strip()),
        "error_type": outcome.error_type,
        "blocked_reason": outcome.decision.blocked_reason,
    }


if __name__ == "__main__":
    raise SystemExit(main())
