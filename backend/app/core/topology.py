from __future__ import annotations

import heapq
from collections.abc import Iterable


class TopologyError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        residual_nodes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.residual_nodes = residual_nodes


def deterministic_topological_order(
    node_ids: Iterable[str],
    edges: Iterable[tuple[str, str]],
    *,
    max_nodes: int = 128,
    max_edges: int = 512,
) -> tuple[str, ...]:
    """Return a stable topological order or fail closed on an invalid graph."""

    nodes = tuple(node_ids)
    if len(nodes) > max_nodes:
        raise TopologyError("topology_node_limit_exceeded", "Graph exceeds the node limit.")
    if any(not isinstance(node, str) or not node for node in nodes):
        raise TopologyError("topology_node_invalid", "Node ids must be non-empty strings.")
    if len(nodes) != len(set(nodes)):
        raise TopologyError("topology_node_duplicate", "Node ids must be unique.")

    edge_items = tuple(edges)
    if len(edge_items) > max_edges:
        raise TopologyError("topology_edge_limit_exceeded", "Graph exceeds the edge limit.")

    node_set = set(nodes)
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    indegree: dict[str, int] = dict.fromkeys(nodes, 0)
    seen_edges: set[tuple[str, str]] = set()
    for edge in edge_items:
        if not isinstance(edge, tuple) or len(edge) != 2:
            raise TopologyError("topology_edge_invalid", "Edges must be (source, target) pairs.")
        source, target = edge
        if source not in node_set or target not in node_set:
            raise TopologyError("topology_edge_unknown_node", "Edge references an unknown node.")
        if source == target:
            raise TopologyError(
                "topology_cycle",
                "Self-cycles are not allowed.",
                residual_nodes=(source,),
            )
        if edge in seen_edges:
            continue
        seen_edges.add(edge)
        adjacency[source].add(target)
        indegree[target] += 1

    ready = [node for node, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        source = heapq.heappop(ready)
        ordered.append(source)
        for target in sorted(adjacency[source]):
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)

    if len(ordered) != len(nodes):
        residual_nodes = tuple(sorted(node for node, degree in indegree.items() if degree > 0))
        raise TopologyError(
            "topology_cycle",
            "Graph contains a directed cycle.",
            residual_nodes=residual_nodes,
        )
    return tuple(ordered)
