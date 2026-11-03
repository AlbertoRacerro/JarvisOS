from __future__ import annotations

from typing import Any


class ProcessKernelError(ValueError):
    def __init__(self, code: str, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context or {}
