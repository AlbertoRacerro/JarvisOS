import os
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class JarvisPaths:
    data_root: Path
    database_file: Path
    workspaces_dir: Path
    artifacts_dir: Path
    logs_dir: Path

    def environment_workspace_dir(self, workspace_id: str) -> Path:
        return self.data_root / "workspaces" / workspace_id / "environment"

    def environment_site_file(self, workspace_id: str) -> Path:
        return self.environment_workspace_dir(workspace_id) / "site.json"

    def environment_profiles_dir(self, workspace_id: str) -> Path:
        return self.environment_workspace_dir(workspace_id) / "profiles"

    def environment_staging_dir(self, workspace_id: str) -> Path:
        return self.environment_workspace_dir(workspace_id) / "staging"

    @property
    def retrieval_index_file(self) -> Path:
        return self.data_root / "retrieval-index.sqlite3"

    @property
    def secrets_dir(self) -> Path:
        return self.data_root / "secrets"

    def bio_models_workspace_dir(self, workspace_id: str) -> Path:
        """Workspace-scoped biological model sets and cards (spec 169)."""
        return self.workspaces_dir / workspace_id / "bio_models"

    @property
    def scaleway_secret_file(self) -> Path:
        return self.secrets_dir / "scaleway-api-key.v1.json"

    @property
    def data_root_exists(self) -> bool:
        return self.data_root.exists()

    def as_strings(self) -> dict[str, str]:
        return {
            "data_root": str(self.data_root),
            "database_file": str(self.database_file),
            "workspaces_dir": str(self.workspaces_dir),
            "artifacts_dir": str(self.artifacts_dir),
            "logs_dir": str(self.logs_dir),
        }


def build_paths(settings: Settings | None = None) -> JarvisPaths:
    resolved = settings or get_settings()
    return JarvisPaths(
        data_root=resolved.data_root,
        database_file=resolved.database_path,
        workspaces_dir=resolved.data_root / "workspaces",
        artifacts_dir=resolved.data_root / "artifacts",
        logs_dir=resolved.data_root / "logs",
    )


def ensure_data_directories(paths: JarvisPaths | None = None) -> JarvisPaths:
    resolved = paths or build_paths()
    for directory in [
        resolved.data_root,
        resolved.workspaces_dir,
        resolved.artifacts_dir,
        resolved.logs_dir,
        resolved.secrets_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    return resolved


def resolve_paths(settings: Settings | None = None) -> JarvisPaths:
    """Compatibility alias for callers that need the resolved data-root paths."""
    return build_paths(settings)


def relay_root() -> Path:
    """Relay workspaces, agent homes and sandbox manifests (spec 157).

    Kept on the WSL filesystem, apart from the data root: these hold only cloud-safe code
    and context, and the sandbox must never be handed a path inside the data root.
    """
    return Path(os.getenv("JARVISOS_RELAY_ROOT", str(Path.home() / ".local" / "share" / "jarvisos" / "relay")))
