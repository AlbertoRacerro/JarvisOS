import sqlite3
from collections.abc import Iterable
from typing import TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def row_to_model(row: sqlite3.Row, model_type: type[ModelT]) -> ModelT:
    return model_type(**dict(row))


def optional_row_to_model(row: sqlite3.Row | None, model_type: type[ModelT]) -> ModelT | None:
    if row is None:
        return None
    return row_to_model(row, model_type)


def rows_to_models(rows: Iterable[sqlite3.Row], model_type: type[ModelT]) -> list[ModelT]:
    return [row_to_model(row, model_type) for row in rows]
