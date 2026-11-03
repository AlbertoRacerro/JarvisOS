from dataclasses import dataclass, field

from app.modules.tools.base import Tool


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self.tools.keys())
