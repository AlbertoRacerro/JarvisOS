import pytest

from app.modules.ai.sensitivity import deterministic_floor


@pytest.mark.parametrize(
    "content",
    [
        "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5,
        "api" + "_key=synthetic-test-value",
        "pass" + "word=synthetic-test-value",
        "Bear" + "er abcdefghijkl",
        "s" + "k-ABCDEFGHIJKLMNOP",
        "e" + "yJabcdefgh.ijklmnop.qrstuvwx",
    ],
)
def test_structural_secret_markers_keep_s4_floor(content: str) -> None:
    assert deterministic_floor(content) == "S4"


@pytest.mark.parametrize(
    "content",
    [
        "api key discussed without an assigned value",
        "Bearer short",
        "sk-short",
        "eyJ.not-a-token",
    ],
)
def test_near_miss_markers_do_not_create_false_s4_floor(content: str) -> None:
    assert deterministic_floor(content) is None
