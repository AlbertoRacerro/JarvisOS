from fastapi import APIRouter, HTTPException

from app.modules.local_ai.runtime.llama_cpp import get_llama_cpp_runtime_owner

router = APIRouter(prefix="/local-ai/runtime/llama-cpp", tags=["local-ai-runtime"])


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
