import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.system import router as system_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.packs import load_packs
from app.core.spa_static import SpaStaticFiles, derive_reserved_roots
from app.modules.agents.hermes.routes import router as hermes_router
from app.modules.agents.hermes.session_pool import HermesSessionPool, configured_supervisor
from app.modules.ai.routes import router as ai_router
from app.modules.ai.sensitivity_routes import router as sensitivity_router
from app.modules.bio_models.routes import router as bio_models_router
from app.modules.bluecad.routes import router as bluecad_router
from app.modules.coding.runtime_routes import router as coding_runtime_router
from app.modules.coding.runtime_truth import (
    capture_runtime_snapshot,
    startup_snapshot_unavailable,
)
from app.modules.development.routes import router as development_router
from app.modules.engineering.operator_routes import router as engineering_operator_router
from app.modules.environment.routes import router as environment_router
from app.modules.flowsheet.routes import router as flowsheet_router
from app.modules.local_ai.runtime.lifecycle import create_local_ai_runtime_lifecycle_from_env
from app.modules.local_ai.runtime.llama_cpp import get_llama_cpp_runtime_owner, llama_cpp_runtime_config
from app.modules.local_ai.runtime.routes import router as local_ai_runtime_router
from app.modules.memory.literature_routes import router as literature_router
from app.modules.memory.routes import router as memory_router
from app.modules.modeling.routes import router as modeling_router
from app.modules.process_stack.draft_routes import router as process_draft_router
from app.modules.process_stack.dynamic_routes import router as process_dynamic_router
from app.modules.process_stack.editor_routes import router as dwsim_editor_router
from app.modules.project_knowledge.routes import router as project_knowledge_router
from app.modules.project_search.routes import router as project_search_router
from app.modules.relay_gateway.routes import router as relay_gateway_router
from app.modules.runner.local_python import execution_ownership_state
from app.modules.runner.recovery import (
    live_stranded_runner_working_dirs,
    reconcile_stranded_runner_jobs,
)
from app.modules.runner.routes import router as runner_router
from app.modules.secrets.routes import router as secrets_router
from app.modules.studio.routes import router as studio_router
from app.modules.studio_runtime.routes import router as studio_runtime_router
from app.modules.workspace_actions.routes import router as workspace_actions_router
from app.modules.workspaces.design_components import router as design_components_router
from app.modules.workspaces.design_versions import router as design_versions_router
from app.modules.workspaces.routes import router as workspaces_router

RUNNER_RECOVERY_RECHECK_SECONDS = 0.25
_SPA_RESERVED_ROOT_CLIENT_ROUTES = frozenset(
    {
        "/memory/literature",
        "/memory/models",
        "/memory/project-basis",
        "/settings/ai",
        "/coding/repository",
        "/coding/runtime",
        "/development/roadmap/timeline",
        "/development/roadmap/calendar",
    }
)


async def _reconcile_after_live_owners_exit(
    working_dirs: tuple[Path, ...], *, poll_seconds: float = RUNNER_RECOVERY_RECHECK_SECONDS
) -> None:
    """Recheck only startup-observed live owners until each becomes non-live."""

    pending = set(working_dirs)
    while pending:
        await asyncio.sleep(poll_seconds)
        gone_paths: set[Path] = set()
        for working_dir in tuple(pending):
            state = execution_ownership_state(working_dir)
            if state == "live":
                continue
            if state == "gone":
                gone_paths.add(working_dir)
            else:
                pending.remove(working_dir)
        if not gone_paths:
            continue
        try:
            reconcile_stranded_runner_jobs()
        except sqlite3.OperationalError:
            # A transient SQLite lock must not discard the only follow-up for a
            # now-abandoned owner. Keep the gone paths pending and retry.
            continue
        pending.difference_update(gone_paths)


def _warm_pbr_imports() -> None:
    """Pay the Photobioreactor solver's cold imports at startup instead of inside a Run (spec 170)."""
    try:
        from app.modules.process_stack import pbr_unit

        pbr_unit.warm_imports()
    except Exception:  # noqa: BLE001 - warming is an optimisation and must never block startup
        logging.getLogger(__name__).warning("Photobioreactor import warm-up failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    lifecycle = create_local_ai_runtime_lifecycle_from_env()
    app.state.local_ai_runtime_lifecycle = lifecycle
    recovery_task: asyncio.Task[None] | None = None
    try:
        try:
            app.state.runtime_startup_snapshot = await asyncio.to_thread(
                capture_runtime_snapshot,
                provenance="process_start_observation",
            )
        except Exception:
            # Runtime-truth observation must never make JarvisOS fail to start.
            app.state.runtime_startup_snapshot = startup_snapshot_unavailable()
        await lifecycle.startup()
        from app.modules.bluecad.cfd_pipe.case_runs import recover_interrupted as recover_cfd_runs
        from app.modules.process_stack.dynamic_jobs import recover_interrupted
        from app.modules.process_stack.solve_jobs import recover_interrupted as recover_process_solves

        await asyncio.to_thread(recover_interrupted)
        await asyncio.to_thread(recover_process_solves)
        await asyncio.to_thread(recover_cfd_runs)
        await asyncio.to_thread(_warm_pbr_imports)
        llama_owner = get_llama_cpp_runtime_owner()
        if llama_cpp_runtime_config().manage:
            await asyncio.to_thread(llama_owner.start)
        live_working_dirs = live_stranded_runner_working_dirs()
        reconcile_stranded_runner_jobs()
        if live_working_dirs:
            recovery_task = asyncio.create_task(
                _reconcile_after_live_owners_exit(live_working_dirs)
            )
        yield
    finally:
        if recovery_task is not None:
            recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await recovery_task
        await lifecycle.shutdown()
        await asyncio.to_thread(get_llama_cpp_runtime_owner().stop)


def _frontend_dist_path() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()
    for pack in load_packs():  # an invalid pack raises PackError here, at startup
        logging.getLogger(__name__).info("Loaded pack %s from %s", pack.id, pack.root)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Local-first architecture spine for JarvisOS.",
        lifespan=lifespan,
    )
    app.state.hermes_supervisor = HermesSessionPool(configured_supervisor)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(system_router)
    app.include_router(ai_router)
    app.include_router(hermes_router)
    app.include_router(sensitivity_router)
    app.include_router(bluecad_router)
    app.include_router(bio_models_router)
    app.include_router(secrets_router)
    app.include_router(workspaces_router)
    app.include_router(design_versions_router)
    app.include_router(design_components_router)
    app.include_router(environment_router)
    app.include_router(modeling_router)
    app.include_router(memory_router)
    app.include_router(literature_router)
    app.include_router(project_search_router)
    app.include_router(runner_router)
    app.include_router(studio_runtime_router)
    app.include_router(studio_router)
    app.include_router(flowsheet_router)
    app.include_router(engineering_operator_router)
    app.include_router(dwsim_editor_router)
    app.include_router(process_draft_router)
    app.include_router(process_dynamic_router)
    app.include_router(workspace_actions_router)
    app.include_router(project_knowledge_router)
    app.include_router(coding_runtime_router)
    app.include_router(local_ai_runtime_router)
    app.include_router(development_router)
    app.include_router(relay_gateway_router)

    frontend_dist = _frontend_dist_path()
    if frontend_dist.is_dir():
        reserved_roots = derive_reserved_roots(app.routes)
        app.mount(
            "/",
            SpaStaticFiles(
                directory=frontend_dist,
                reserved_roots=reserved_roots,
                reserved_root_client_routes=_SPA_RESERVED_ROOT_CLIENT_ROUTES,
            ),
            name="frontend",
        )

    return app


app = create_app()
