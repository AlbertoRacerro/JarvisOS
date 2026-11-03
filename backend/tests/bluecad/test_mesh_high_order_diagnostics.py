from app.modules.bluecad.mesh_adapter import _high_order_invalid_error


def test_completely_inverted_elements_are_rejected() -> None:
    assert _high_order_invalid_error(["Warning: 17 elements are completely inverted"]) == {
        "code": "MESH_HIGH_ORDER_INVALID",
        "detail": {
            "requested_order": 2,
            "diagnostics": ["Warning: 17 elements are completely inverted"],
            "reported_invalid_elements": 17,
        },
    }


def test_single_completely_inverted_element_is_rejected() -> None:
    assert _high_order_invalid_error(["Warning: 1 element is completely inverted"]) == {
        "code": "MESH_HIGH_ORDER_INVALID",
        "detail": {
            "requested_order": 2,
            "diagnostics": ["Warning: 1 element is completely inverted"],
            "reported_invalid_elements": 1,
        },
    }


def test_zero_completely_inverted_elements_are_accepted() -> None:
    assert _high_order_invalid_error(["Info: 0 elements are completely inverted"]) is None
