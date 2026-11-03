from dataclasses import dataclass, field

from app.modules.agents.base import Agent


@dataclass
class AgentRegistry:
    agents: dict[str, Agent] = field(default_factory=dict)

    def register(self, agent: Agent) -> None:
        self.agents[agent.name] = agent

    def names(self) -> list[str]:
        return sorted(self.agents.keys())
