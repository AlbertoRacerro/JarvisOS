from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ToolResult:
    status: str
    output: dict[str, object]


class Tool(Protocol):
    name: str

    def run(self, payload: dict[str, object]) -> ToolResult:
        """Run a future executable tool."""
