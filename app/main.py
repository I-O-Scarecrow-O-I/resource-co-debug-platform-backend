from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import get_log_service, get_settings
from app.api.router import api_router
from app.core.errors import AppError
from app.schemas.common import ApiResponse


def create_app() -> FastAPI:
    settings = get_settings()
    settings.storage_root.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="B/S backend foundation for resource-coordinated debugging.",
    )
    app.include_router(api_router, prefix="/api")

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

    @app.websocket("/ws/tasks/{task_id}/logs")
    async def stream_task_logs(websocket: WebSocket, task_id: str) -> None:
        log_service = get_log_service()
        await websocket.accept()

        for event in log_service.history(task_id):
            await websocket.send_json(event.model_dump(mode="json"))

        queue = log_service.subscribe(task_id)
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event.model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        finally:
            log_service.unsubscribe(task_id, queue)

    return app


app = create_app()
