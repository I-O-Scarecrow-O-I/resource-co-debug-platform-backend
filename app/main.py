from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.errors import AppError
from app.platform.api.deps import (
    clear_task_service_cache,
    get_log_service,
    get_settings,
    get_task_service,
)
from app.platform.api.router import api_router
from app.platform.schemas.common import ApiResponse
from app.platform.services.task_service import TaskService


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        task_service: TaskService = get_task_service()
        await task_service.startup()
        try:
            yield
        finally:
            await task_service.shutdown()
    finally:
        clear_task_service_cache()


def create_app() -> FastAPI:
    settings = get_settings()
    settings.storage_root.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="B/S backend foundation for resource-coordinated debugging.",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix="/api/v1")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def handle_app_error(_, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(ApiResponse.failed(exc.message)),
        )

    @app.websocket("/ws/v1/tasks/{task_id}/logs")
    async def stream_task_logs(websocket: WebSocket, task_id: str) -> None:
        log_service = get_log_service()
        await websocket.accept()

        history, queue, cutover_sequence = log_service.subscribe_with_history(task_id)
        try:
            for event in history:
                await websocket.send_json(event.model_dump(mode="json"))

            while True:
                event = await queue.get()
                if event.sequence > cutover_sequence:
                    await websocket.send_json(event.model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        finally:
            log_service.unsubscribe(task_id, queue)

    return app


app = create_app()

