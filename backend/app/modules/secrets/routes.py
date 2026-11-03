from fastapi import APIRouter, HTTPException, Request

from app.modules.secrets.models import ScalewaySecretStatus
from app.modules.secrets.service import (
    SecretEnvironmentOverrideError,
    SecretStorageError,
    SecretStorageUnavailableError,
    delete_scaleway_api_key,
    read_scaleway_secret_status,
    save_scaleway_api_key,
)

router = APIRouter(prefix="/secrets", tags=["secrets"])


@router.get("/scaleway/status", response_model=ScalewaySecretStatus)
def get_scaleway_status() -> ScalewaySecretStatus:
    return read_scaleway_secret_status(log_status_check=True)


@router.post("/scaleway/api-key", response_model=ScalewaySecretStatus)
async def set_scaleway_api_key(request: Request) -> ScalewaySecretStatus:
    try:
        payload = await request.json()
    except ValueError:
        payload = None
    api_key = payload.get("api_key") if isinstance(payload, dict) else None
    try:
        return save_scaleway_api_key(api_key if isinstance(api_key, str) else None)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "scaleway_api_key_invalid",
                "message": "Scaleway API key must be a non-empty single token.",
            },
        ) from None
    except SecretEnvironmentOverrideError:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "scaleway_api_key_environment_override",
                "message": "The environment-provided Scaleway credential is authoritative.",
            },
        ) from None
    except SecretStorageUnavailableError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "scaleway_api_key_persistence_unavailable",
                "message": "Secure credential persistence is unavailable.",
            },
        ) from None
    except SecretStorageError:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "scaleway_api_key_persistence_failed",
                "message": "The credential could not be persisted safely.",
            },
        ) from None


@router.delete("/scaleway/api-key", response_model=ScalewaySecretStatus)
def remove_scaleway_api_key() -> ScalewaySecretStatus:
    try:
        return delete_scaleway_api_key()
    except SecretStorageUnavailableError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "scaleway_api_key_persistence_unavailable",
                "message": "Secure credential persistence is unavailable.",
            },
        ) from None
    except SecretStorageError:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "scaleway_api_key_delete_failed",
                "message": "The persisted credential could not be deleted safely.",
            },
        ) from None
