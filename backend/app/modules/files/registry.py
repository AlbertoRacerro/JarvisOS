from dataclasses import dataclass, field

from app.modules.files.models import ArtifactRecord


@dataclass
class ArtifactRegistry:
    records: dict[str, ArtifactRecord] = field(default_factory=dict)

    def register(self, artifact: ArtifactRecord) -> ArtifactRecord:
        self.records[artifact.id] = artifact
        return artifact

    def list_records(self) -> list[ArtifactRecord]:
        return list(self.records.values())
