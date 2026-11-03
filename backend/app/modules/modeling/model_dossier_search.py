from __future__ import annotations

from app.core.database import open_sqlite_connection
from app.core.search_text import register_search
from app.modules.modeling.dossier_models import ModelDossierIndexItem, ModelDossierVersionIdentity

_MODEL_SEARCH_MATCH_LIMIT = 101


def _escape_like_literal(value: str) -> str:
    return value.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_model_dossier_index(workspace_id: str, query: str) -> list[ModelDossierIndexItem]:
    """Return bounded literal model/version matches from canonical dossier tables."""
    with open_sqlite_connection() as connection:
        register_search(connection, query)
        workspace = connection.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if workspace is None:
            raise ValueError("Workspace not found.")
        rows = connection.execute(
            """
            SELECT
                ms.id AS model_spec_id,
                ms.title,
                ms.engineering_question,
                ms.scope,
                ms.created_at AS model_created_at,
                mv.id AS model_version_id,
                mv.version_label,
                mv.implementation_kind,
                mv.status AS version_status,
                mv.created_at AS version_created_at,
                mv.input_contract_sha256
            FROM model_specs AS ms
            LEFT JOIN model_versions AS mv
              ON mv.model_spec_id = ms.id
             AND mv.workspace_id = ms.workspace_id
            WHERE ms.workspace_id = ?
              AND (
                JARVIS_MATCH(ms.title) < 99
                OR JARVIS_MATCH(ms.engineering_question) < 99
                OR JARVIS_MATCH(ms.scope) < 99
                OR JARVIS_MATCH(mv.version_label) < 99
                OR JARVIS_MATCH(mv.implementation_kind) < 99
              )
            ORDER BY MIN(JARVIS_MATCH(ms.title),JARVIS_MATCH(ms.engineering_question),JARVIS_MATCH(ms.scope),JARVIS_MATCH(mv.version_label),JARVIS_MATCH(mv.implementation_kind)), ms.created_at DESC, ms.id ASC, mv.created_at DESC, mv.id ASC
            LIMIT ?
            """,
            (workspace_id, _MODEL_SEARCH_MATCH_LIMIT),
        ).fetchall()

    grouped: dict[str, ModelDossierIndexItem] = {}
    for row in rows:
        model_spec_id = str(row["model_spec_id"])
        item = grouped.get(model_spec_id)
        if item is None:
            item = ModelDossierIndexItem(
                model_spec_id=model_spec_id,
                title=str(row["title"]),
                engineering_question=str(row["engineering_question"]),
                scope=row["scope"],
                versions=[],
            )
            grouped[model_spec_id] = item
        if row["model_version_id"] is not None:
            item.versions.append(
                ModelDossierVersionIdentity(
                    model_spec_id=model_spec_id,
                    model_version_id=str(row["model_version_id"]),
                    version_label=row["version_label"],
                    implementation_kind=row["implementation_kind"],
                    status=row["version_status"],
                    created_at=row["version_created_at"],
                    input_contract_digest=row["input_contract_sha256"],
                )
            )
    return list(grouped.values())
