from dataclasses import dataclass

from fastapi import HTTPException

WORKSPACE_NOT_FOUND_CODE = "workspace_not_found"
WORKSPACE_NOT_FOUND_MESSAGE = "Workspace not found."


@dataclass(frozen=True)
class AppError(Exception):
    code: str
    message: str


def workspace_not_found_http_error() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": WORKSPACE_NOT_FOUND_CODE,
            "message": WORKSPACE_NOT_FOUND_MESSAGE,
        },
    )
