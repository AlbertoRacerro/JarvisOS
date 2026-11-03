import sqlite3


class ParameterReplacementError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_parameter_replacement_proposal(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    supersedes_parameter_id: str | None,
    replacement_parameter_id: str | None,
    unit: str,
    value: str | None,
) -> sqlite3.Row | None:
    if supersedes_parameter_id is None:
        return None
    supersedes_parameter_id = supersedes_parameter_id.strip()
    if not supersedes_parameter_id:
        raise ParameterReplacementError(
            "parameter_replacement_not_configured",
            "The superseded Parameter ID is empty.",
        )
    if replacement_parameter_id is not None and supersedes_parameter_id == replacement_parameter_id:
        raise ParameterReplacementError(
            "parameter_replacement_not_configured",
            "A Parameter cannot supersede itself.",
        )
    if replacement_parameter_id is not None:
        replacement = connection.execute(
            "SELECT workspace_id, lifecycle_state FROM parameters WHERE id = ?",
            (replacement_parameter_id,),
        ).fetchone()
        if replacement is not None:
            if str(replacement["workspace_id"]) != workspace_id:
                raise ParameterReplacementError(
                    "parameter_replacement_cross_workspace",
                    "The replacement Parameter is outside the replacement workspace.",
                )
            if str(replacement["lifecycle_state"]) != "active":
                raise ParameterReplacementError(
                    "parameter_replacement_target_not_active",
                    "Only a lifecycle-active replacement proposal can be promoted.",
                )
    row = connection.execute(
        "SELECT id, workspace_id, status, lifecycle_state, unit FROM parameters WHERE id = ?",
        (supersedes_parameter_id,),
    ).fetchone()
    if row is None:
        raise ParameterReplacementError(
            "parameter_replacement_not_found",
            "The superseded Parameter was not found.",
        )
    if str(row["workspace_id"]) != workspace_id:
        raise ParameterReplacementError(
            "parameter_replacement_cross_workspace",
            "The superseded Parameter is outside the replacement workspace.",
        )
    if str(row["status"]) != "accepted":
        raise ParameterReplacementError(
            "parameter_replacement_source_not_accepted",
            "The superseded Parameter is not accepted.",
        )
    if str(row["lifecycle_state"]) != "active":
        raise ParameterReplacementError(
            "parameter_replacement_source_not_active",
            "Only a lifecycle-active Parameter can be superseded.",
        )
    if str(row["unit"]) != unit:
        raise ParameterReplacementError(
            "parameter_replacement_unit_mismatch",
            "The replacement Parameter must use the exact superseded unit.",
        )
    if value is None or not str(value).strip():
        raise ParameterReplacementError(
            "parameter_replacement_value_missing",
            "The replacement Parameter must contain a value.",
        )
    return row
