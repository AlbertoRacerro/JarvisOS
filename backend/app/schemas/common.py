from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceStatus:
    status: str
    message: str
