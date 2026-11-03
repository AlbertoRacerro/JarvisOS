"""Native and synthetic CalculiX reaction-force parsing."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

_NATIVE_REACTION_HEADER = re.compile(
    r"^\s*forces\s*\(fx,\s*fy,\s*fz\)\s+for\s+set\b",
    re.IGNORECASE,
)

def _parse_reactions(dat_path: Path) -> list[dict[str, Any]]:
    lines = dat_path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines()
    reactions: list[dict[str, Any]] = []
    for raw in lines:
        parts = raw.split()
        if parts and parts[0].upper() == "REACTION" and len(parts) == 5:
            reactions.append(
                {
                    "node_id": int(parts[1]),
                    "force": [_reaction_float(value) for value in parts[2:5]],
                }
            )
    if reactions:
        return reactions

    native_blocks: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(lines):
        if not _NATIVE_REACTION_HEADER.match(lines[index]):
            index += 1
            continue
        index += 1
        block: list[dict[str, Any]] = []
        seen_nodes: set[int] = set()
        while index < len(lines):
            parts = lines[index].split()
            if not parts:
                if block:
                    break
                index += 1
                continue
            if _NATIVE_REACTION_HEADER.match(lines[index]):
                break
            if len(parts) < 4:
                raise ValueError(
                    f"invalid native reaction record: {lines[index]!r}"
                )
            try:
                node_id = int(parts[0])
                force = [_reaction_float(value) for value in parts[1:4]]
            except ValueError as exc:
                raise ValueError(
                    f"invalid native reaction record: {lines[index]!r}"
                ) from exc
            if node_id in seen_nodes:
                raise ValueError(
                    f"duplicate native reaction node: {node_id}"
                )
            seen_nodes.add(node_id)
            block.append({"node_id": node_id, "force": force})
            index += 1
        if not block:
            raise ValueError("native reaction block has no parseable records")
        native_blocks.append(block)
    return native_blocks[-1] if native_blocks else []


def _reaction_float(value: str) -> float:
    parsed = float(value.replace("D", "E").replace("d", "e"))
    if not math.isfinite(parsed):
        raise ValueError("non-finite native reaction value")
    return parsed


