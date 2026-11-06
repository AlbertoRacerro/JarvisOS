from fastapi import APIRouter, HTTPException

from app.modules.local_ai.runtime.llama_cpp import get_llama_cpp_runtime_owner
from app.modules.local_ai.runtime.model_registry import activate_model, list_models, restore_default

router = APIRouter(prefix="/local-ai/runtime/llama-cpp", tags=["local-ai-runtime"])
models_router = APIRouter(prefix="/local-ai/models", tags=["local-ai-runtime"])


@models_router.get("")
def read_models() -> dict[str, object]:
    return list_models()


@models_router.post("/restore-default")
def restore_model() -> dict[str, object]:
    return restore_default()


@models_router.post("/{model_id}/activate")
def activate_registered_model(model_id: str) -> dict[str, object]:
    return activate_model(model_id)


@router.get("")
def read_llama_cpp_status() -> dict[str, object]:
    return get_llama_cpp_runtime_owner().status()


@router.post("/{action}")
def control_llama_cpp(action: str) -> dict[str, object]:
    owner = get_llama_cpp_runtime_owner()
    if action == "start":
        return owner.start()
    if action == "stop":
        return owner.stop()
    if action == "restart":
        return owner.restart()
    if action == "verify-digest":
        return owner.verify_digest()
    raise HTTPException(status_code=404, detail="Unknown llama.cpp runtime action.")
