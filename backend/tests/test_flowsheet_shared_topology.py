from app.core.topology import TopologyError, deterministic_topological_order
from app.modules.flowsheet.models import FlowsheetEdgeRead, FlowsheetNodeRead
from app.modules.flowsheet.service import _topological_projection


def _node(ref: str) -> FlowsheetNodeRead:
    _, record_id = ref.split(":", 1)
    return FlowsheetNodeRead(
        ref=ref,
        kind="parameter",
        id=record_id,
        label=record_id,
    )


def _edge(
    upstream: str,
    downstream: str,
    *,
    edge_class: str = "dependency",
) -> FlowsheetEdgeRead:
    return FlowsheetEdgeRead(
        id=f"{upstream}->{downstream}:{edge_class}",
        upstream_ref=upstream,
        downstream_ref=downstream,
        relation="test",
        edge_class=edge_class,
        authorities=["test"],
        source_fields=["test"],
    )


def test_flowsheet_projection_reuses_stable_lexicographic_order() -> None:
    refs = ("parameter:c", "parameter:a", "parameter:b", "parameter:d")
    nodes = {ref: _node(ref) for ref in refs}
    edges = [
        _edge("parameter:a", "parameter:c"),
        _edge("parameter:b", "parameter:c"),
        _edge("parameter:c", "parameter:d"),
        _edge("parameter:d", "parameter:a", edge_class="provenance"),
    ]

    is_acyclic, order, cycles = _topological_projection(nodes, edges)

    assert is_acyclic is True
    assert order == ["parameter:a", "parameter:b", "parameter:c", "parameter:d"]
    assert cycles == []
    assert tuple(order) == deterministic_topological_order(
        nodes,
        [
            ("parameter:a", "parameter:c"),
            ("parameter:b", "parameter:c"),
            ("parameter:c", "parameter:d"),
        ],
        max_nodes=1000,
        max_edges=3000,
    )


def test_cycle_diagnostics_preserve_cycle_and_ignore_downstream_tail() -> None:
    refs = ("parameter:a", "parameter:b", "parameter:c")
    nodes = {ref: _node(ref) for ref in refs}
    edges = [
        _edge("parameter:a", "parameter:b"),
        _edge("parameter:b", "parameter:a"),
        _edge("parameter:b", "parameter:c"),
    ]

    is_acyclic, order, cycles = _topological_projection(nodes, edges)

    assert is_acyclic is False
    assert order is None
    assert cycles == [["parameter:a", "parameter:b", "parameter:a"]]

    try:
        deterministic_topological_order(
            nodes,
            [
                ("parameter:a", "parameter:b"),
                ("parameter:b", "parameter:a"),
                ("parameter:b", "parameter:c"),
            ],
            max_nodes=1000,
            max_edges=3000,
        )
    except TopologyError as exc:
        assert exc.code == "topology_cycle"
        assert exc.residual_nodes == ("parameter:a", "parameter:b", "parameter:c")
    else:
        raise AssertionError("Cycle must fail closed.")


def test_self_cycle_remains_canonical() -> None:
    ref = "parameter:self"
    nodes = {ref: _node(ref)}

    is_acyclic, order, cycles = _topological_projection(nodes, [_edge(ref, ref)])

    assert is_acyclic is False
    assert order is None
    assert cycles == [[ref, ref]]
