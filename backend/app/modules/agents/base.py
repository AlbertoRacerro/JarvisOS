from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentCapability:
    name: str
    description: str


class Agent(Protocol):
    name: str
    capabilities: list[AgentCapability]
