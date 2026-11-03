from app.modules.local_ai_eval.routing_eval import (
    RoutingEvalPrediction,
    compute_routing_agreement,
    load_routing_eval_cases,
    render_local_route_smoke_markdown,
    render_routing_eval_markdown,
)


def test_routing_eval_fixture_covers_required_labels() -> None:
    cases = load_routing_eval_cases()

    assert len(cases) >= 30
    assert {case.expected_capability for case in cases} == {
        "simple",
        "general_reasoning",
        "coding",
        "heavy_coding",
        "deep_reasoning",
    }
    assert {case.expected_context_level for case in cases} == {"none", "light", "standard", "deep"}
    assert sum(1 for case in cases if case.expected_capability == "deep_reasoning") >= 3


def test_compute_routing_agreement_reports_accuracy_and_mismatches() -> None:
    predictions = [
        RoutingEvalPrediction(
            id="ok",
            prompt="p",
            expected_capability="simple",
            actual_capability="simple",
            expected_context_level="none",
            actual_context_level="none",
        ),
        RoutingEvalPrediction(
            id="bad",
            prompt="p",
            expected_capability="coding",
            actual_capability="simple",
            expected_context_level="standard",
            actual_context_level="light",
        ),
    ]

    agreement = compute_routing_agreement(predictions)

    assert agreement.total == 2
    assert agreement.capability.correct == 1
    assert agreement.context_level.correct == 1
    assert agreement.exact_match.correct == 1
    assert [item.id for item in agreement.mismatches] == ["bad"]
    assert agreement.confusion["capability"]["coding"]["simple"] == 1


def test_routing_eval_markdown_includes_summary_and_mismatches() -> None:
    report = {
        "generated_at": "2026-07-02T00:00:00+00:00",
        "agreement": {
            "total": 1,
            "capability": {"correct": 0, "total": 1, "accuracy": 0.0},
            "context_level": {"correct": 1, "total": 1, "accuracy": 1.0},
            "exact_match": {"correct": 0, "total": 1, "accuracy": 0.0},
            "mismatches": [
                {
                    "id": "case-1",
                    "expected_capability": "coding",
                    "actual_capability": "simple",
                    "expected_context_level": "none",
                    "actual_context_level": "none",
                }
            ],
        },
    }

    markdown = render_routing_eval_markdown(report)

    assert "| capability | 0 | 1 | 0.00% |" in markdown
    assert "`case-1` capability coding -> simple" in markdown


def test_local_route_smoke_markdown_includes_ledgers_and_swap_sequence() -> None:
    report = {
        "generated_at": "2026-07-02T00:00:00+00:00",
        "routes": [
            {
                "route_class": "local:fast",
                "model_id": "qwen3:8b",
                "calls": [{"status": "success", "wall_ms": 100, "ledger_id": "job-1"}],
            }
        ],
        "swap_sequence": [
            {"route_class": "local:fast", "model_id": "qwen3:8b", "status": "success", "wall_ms": 80, "ledger_id": "job-2"}
        ],
    }

    markdown = render_local_route_smoke_markdown(report)

    assert "| local:fast | qwen3:8b | 1 | 1 | 100 | job-1 |" in markdown
    assert "| 1 | local:fast | qwen3:8b | success | 80 | job-2 |" in markdown
