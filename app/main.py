from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.errors import AppError
from app.modules.code_generation.deps import (
    clear_code_generation_task_service_cache,
    get_code_generation_service,
    get_code_generation_task_service,
)
from app.modules.code_generation.task_service import CodeGenerationTaskService
from app.platform.api.deps import (
    clear_log_service_cache,
    clear_task_service_cache,
    clear_task_store_cache,
    get_log_service,
    get_settings,
    get_task_service,
)
from app.platform.api.router import api_router
from app.platform.api.upload_limit import ProjectUploadSizeLimitMiddleware
from app.platform.schemas.common import ApiResponse
from app.platform.services.log_service import LogStreamOverflow
from app.platform.services.task_service import TaskService
from app.platform.services.workspace_service import WorkspaceService

# 统一声明平台业务错误的响应结构，便于 OpenAPI 客户端生成一致的错误类型。
APP_ERROR_RESPONSES = {
    400: {
        "model": ApiResponse[None],
        "description": "Application error response.",
    },
    404: {
        "model": ApiResponse[None],
        "description": "Application error response.",
    },
}


# 生命周期统一管理任务平台和代码生成恢复器。
# 任一步启动失败仍走收尾，只处理已取得的服务实例。
@asynccontextmanager
async def lifespan(_: FastAPI):
    # 先记录局部所有权，启动中途失败时只关闭已经取得的依赖。
    task_service: TaskService | None = None
    code_generation_task_service: CodeGenerationTaskService | None = None
    # 两个启动步骤共用同一异常收尾路径；中途异常时也应释放先前创建的依赖。
    try:
        task_service = get_task_service()
        # 生成恢复器依赖同一个平台任务服务，避免重启时在另一份任务存储中查找未完成运行。
        code_generation_task_service = get_code_generation_task_service(
            task_service=task_service,
            naturalcc_service=get_code_generation_service(),
        )
        # 先恢复持久化的通用任务状态，再让代码生成模块接管自己的恢复工作。
        await task_service.startup()
        await code_generation_task_service.startup()
        # 进入 yield 后才对外提供服务；退出 yield 时停止应用工作。
        yield
    # 先关闭任务入口并通知执行者停止，再关闭代码生成模块的后台工作。
    # 各阶段的收尾互不短路，前一步异常也不能跳过后一步。
    finally:
        # 清理顺序保留资源依赖：先停平台执行入口，再结束模块恢复器，最后处理共享连接。
        try:
            if task_service is not None:
                await task_service.shutdown()
        finally:
            try:
                # 模块有独立恢复工作，通用任务停止后仍要结束其清理循环。
                if code_generation_task_service is not None:
                    await code_generation_task_service.shutdown()
            finally:
                # 通用存储和日志在最后一个后台任务退出后关闭。
                # 依赖缓存只撤掉实例引用，以免仍在运行的 worker 提前失去资源。
                try:
                    if task_service is not None:
                        # 任务服务自行判断立即关闭或等到空闲，不应由缓存清除操作替它提前关闭。
                        task_service.close_resources_when_idle()
                finally:
                    # 缓存失效不再关闭同一连接；连接关闭已由空闲回调或当前收尾负责。
                    clear_code_generation_task_service_cache()
                    clear_task_store_cache(close=False)
                    clear_log_service_cache(close=False)
                    clear_task_service_cache()


# 应用工厂使用同一份设置初始化存储根、路由和跨域规则。
def create_app() -> FastAPI:
    # 工厂只装配应用并确保存储根存在；任务恢复留给实际服务启动。
    settings = get_settings()
    # 工厂确保根路径能创建；数据库和任务恢复的错误仍由服务启动阶段报告。
    settings.storage_root.mkdir(parents=True, exist_ok=True)

    # 应用级声明为模块路由提供公共文档和生命周期，不替换模块各自的响应契约。
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="B/S backend foundation for resource-coordinated debugging.",
        lifespan=lifespan,
        responses=APP_ERROR_RESPONSES,
    )
    # 业务路由统一版本前缀；日志 WebSocket 另用自己的版本路径。
    app.include_router(api_router, prefix="/api/v1")

    # 上传体积限制位于业务路由之外，multipart 解析前即可拦截大请求。
    app.add_middleware(
        ProjectUploadSizeLimitMiddleware,
        max_body_bytes=WorkspaceService.MAX_PROJECT_UPLOAD_BODY_BYTES,
    )

    # 在外层添加 CORS，使上传中间件直接生成的错误也能返回跨域头。
    # 跨域来源从设置取得；凭据式跨域请求并未在此启用。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 业务异常沿用统一响应体及自身状态码；这里不拦截未知异常。
    @app.exception_handler(AppError)
    # HTTP 状态沿用业务异常本身，响应体则维持平台统一的失败结构。
    async def handle_app_error(_, exc: AppError) -> JSONResponse:
        # 异常中的公开消息直接进入失败响应，因此模块构造 AppError 时要区分用户信息与内部诊断。
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(ApiResponse.failed(exc.message)),
        )

    # 日志连接以任务 ID 订阅；无效或不存在的任务不会分配订阅队列。
    @app.websocket("/ws/v1/tasks/{task_id}/logs")
    async def stream_task_logs(websocket: WebSocket, task_id: str) -> None:
        # 接受连接前解析 ID 和查任务，拒绝的客户端不会进入日志订阅生命周期。
        try:
            get_task_service().require_task(UUID(task_id))
        # 无效任务标识和找不到任务都在接受连接之前按策略违规关闭，不建立日志订阅。
        except (AppError, ValueError):
            await websocket.close(code=1008)
            return

        # 握手后才创建订阅；其历史切换点由日志服务而非 WebSocket 推算。
        log_service = get_log_service()
        await websocket.accept()

        # 历史和实时队列同时取得，用切换序号过滤重叠段。
        # 先发送历史，再消费实时事件，客户端不必自行合并两路数据。
        history, queue, cutover_sequence = log_service.subscribe_with_history(task_id)
        try:
            # 历史发送期间新事件进入实时队列，不能在历史播完之后才订阅，否则会漏掉这段窗口。
            for event in history:
                await websocket.send_json(event.model_dump(mode="json"))

            # 订阅必须持续消费，任务终态不自动结束连接。
            # 慢消费者收到溢出关闭后可以重新读取保留历史。
            while True:
                # 实时取队列可以收到普通日志或溢出信号；后者没有可序列化成日志的业务载荷。
                event = await queue.get()
                # 订阅队列落后时显式断开，让客户端决定重连，而不是静默跳过事件。
                if isinstance(event, LogStreamOverflow):
                    await websocket.close(code=1013)
                    return
                # 序号条件只筛实时阶段，历史批次已在此前完整发送。
                if event.sequence > cutover_sequence:
                    await websocket.send_json(event.model_dump(mode="json"))
        # 断开只结束当前连接的读取，不改变被订阅任务的生命周期。
        except WebSocketDisconnect:
            pass
        # 无论主动断开还是发送失败，都撤销订阅，避免日志服务继续积压。
        finally:
            log_service.unsubscribe(task_id, queue)

    return app


# 供 ASGI 启动器导入默认应用；测试也可通过工厂构造独立实例。
app = create_app()

