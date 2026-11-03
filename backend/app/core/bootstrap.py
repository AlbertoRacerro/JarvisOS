from app.core.database import DatabaseInfo, initialize_database
from app.modules.ai.settings import ensure_ai_settings
from app.modules.workspaces.service import seed_default_workspace


def initialize_storage(seed_default: bool = True) -> DatabaseInfo:
    info = initialize_database()
    ensure_ai_settings()
    if seed_default:
        seed_default_workspace()
    return info


if __name__ == "__main__":
    initialized = initialize_storage(seed_default=True)
    print(f"Initialized {initialized.engine} database at {initialized.database_file}")
