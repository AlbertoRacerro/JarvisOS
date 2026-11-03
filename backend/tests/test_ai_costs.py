from app.modules.ai.costs import estimate_route_cost


def test_estimate_route_cost_for_known_prompt_size() -> None:
    estimate = estimate_route_cost("external:reasoning", "abcdefgh", 10)

    assert estimate["label"] == "estimate"
    assert estimate["input_tokens"] == 2
    assert estimate["max_output_tokens"] == 10
    assert estimate["estimated_cost_usd"] == 0.0000132
