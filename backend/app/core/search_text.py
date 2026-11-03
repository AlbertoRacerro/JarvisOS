"""Deterministic human text matching; ranking is never source authority."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache


@lru_cache(maxsize=512)
def tokens(text: str) -> tuple[str, ...]:
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return tuple(re.findall(r"[^\W_]+", folded))


def match_rank(query: str, value: str | None, *, literal: bool = False) -> int:
    if not value:
        return 99
    needle, haystack = query.casefold(), value.casefold()
    if needle == haystack:
        return 0
    if haystack.startswith(needle):
        return 1
    if needle in haystack:
        return 2
    if literal:
        return 99
    wanted, found = tokens(query), tokens(value)
    if not wanted:
        return 99
    if " ".join(wanted) in " ".join(found):
        return 3
    if set(wanted).issubset(found):
        return 4
    return 99


def register_search(connection, query: str, *, literal: bool = False) -> None:
    connection.create_function(
        "JARVIS_MATCH", 1,
        lambda value: match_rank(query, str(value) if value is not None else None, literal=literal),
        deterministic=True,
    )
