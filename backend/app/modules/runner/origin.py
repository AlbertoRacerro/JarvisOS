from typing import Annotated

from fastapi import Header, HTTPException

from app.core.config import get_settings


def require_runner_origin(
    origin: Annotated[str | None, Header(alias="Origin")] = None,
) -> None:
    """Reject browser-originated mutations outside the configured local allowlist.

    Missing Origin remains valid for local non-browser clients. This is a
    browser/CSRF guard, not authentication.
    """

    if origin is None:
        return
    if origin not in get_settings().cors_origins:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "runner_origin_forbidden",
                "message": "Origin is not allowed for runner mutation routes.",
            },
        )
