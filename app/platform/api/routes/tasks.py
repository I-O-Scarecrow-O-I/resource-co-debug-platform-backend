from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.modules.co_debug.services.task_service import CoDebugTaskService
from app.platform.api.deps import get_co_debug_task_service, get_log_service, get_task_service
from app.platform.schemas.common import ApiResponse, LogEvent
from app.platform.schemas.tasks import (
    BuildArtifactResponse,
    BuildTaskRequest,
    DebugTaskRequest,
    ScheduleComparisonRequest,
    ScheduleExperimentRequest,
    TaskResponse,
)
from app.platform.services.log_service import TaskLogService
from app.platform.services.task_service import TaskService

router = APIRouter()


# 产物下载直接传二进制，文档不把它描述成统一 JSON 包络。
ARTIFACT_DOWNLOAD_RESPONSE = {
    "description": "Binary task artifact download.",
    "content": {
        "application/octet-stream": {
            "schema": {"type": "string", "format": "binary"},
        }
    },
}


# 兼容任务路由把构建请求交给模块适配层；这里不直接运行编译器。
@router.post("/build", response_model=ApiResponse[TaskResponse])
async def create_build_task(
    request: BuildTaskRequest,
    task_service: Annotated[CoDebugTaskService, Depends(get_co_debug_task_service)],
) -> ApiResponse[TaskResponse]:
    # 任务提交返回可轮询记录，编译结果由后台任务终态给出。
    # 构建任务响应中的任务 ID 同时用于查状态、看日志和定位成功后发布的文件。
    task = await task_service.create_build_task(request)
    # HTTP 返回使用领域记录快照，命令退出码和产物可用性以后续轮询为准。
    return ApiResponse.ok(TaskResponse.from_record(task))


# 批处理调试记录仍使用平台任务协议。
# 需要断点、单步或查看现场的客户端应走交互调试入口。
@router.post("/debug", response_model=ApiResponse[TaskResponse])
async def create_debug_task(
    request: DebugTaskRequest,
    task_service: Annotated[CoDebugTaskService, Depends(get_co_debug_task_service)],
) -> ApiResponse[TaskResponse]:
    # 此路由提交一次性调试任务，不打开用于后续发命令的交互会话。
    task = await task_service.create_debug_task(request)
    # 返回快照属于新登记的任务。
    # 如果需要 GDB 实时现场，应使用调试会话的状态接口而不是此处的任务字段。
    return ApiResponse.ok(TaskResponse.from_record(task))


# 一次实验只执行所选策略，可用于看核心分配与实际进程结果，不生成两策略验收结论。
@router.post("/schedule-experiments", response_model=ApiResponse[TaskResponse])
async def create_schedule_experiment(
    request: ScheduleExperimentRequest,
    task_service: Annotated[CoDebugTaskService, Depends(get_co_debug_task_service)],
) -> ApiResponse[TaskResponse]:
    # 单次策略实验与成对策略对比走不同入口，结果契约也各自独立。
    # 实验接口只执行一个指定策略。
    # 若需要基线和优化的成对观测，应提交专门的比较任务。
    task = await task_service.create_schedule_experiment(request)
    return ApiResponse.ok(TaskResponse.from_record(task))


# 比较任务需要在同一组负载上先后执行基线与优化。
# 接口回执只提供可追踪任务身份。
@router.post("/schedule-comparisons", response_model=ApiResponse[TaskResponse])
async def create_schedule_comparison(
    request: ScheduleComparisonRequest,
    task_service: Annotated[CoDebugTaskService, Depends(get_co_debug_task_service)],
) -> ApiResponse[TaskResponse]:
    # 比较任务可被接收后执行失败，HTTP 提交成功不代表验收指标满足。
    # 比较任务的 ID 仍属于统一任务空间。
    # 轮次不会注册成多个可以独立取消的 HTTP 任务。
    task = await task_service.create_schedule_comparison(request)
    return ApiResponse.ok(TaskResponse.from_record(task))


# 列表展示平台当前已知任务；用户可从每条记录的模块和任务类型选择解析方式。
@router.get("", response_model=ApiResponse[list[TaskResponse]])
async def list_tasks(
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[list[TaskResponse]]:
    # 所有模块任务使用统一响应类型，模块专用产物仍由任务类型区分。
    # 同一入口列出平台已知的各种任务类型，列表无需按模块重复拼接。
    # 更新一个任务不改变它的创建时间。
    return ApiResponse.ok([TaskResponse.from_record(task) for task in task_service.list_tasks()])


# 非法 UUID 在路由参数解析时失败；有效但未知的任务 ID 走存储层的业务错误。
@router.get("/{task_id}", response_model=ApiResponse[TaskResponse])
async def get_task(
    task_id: UUID,
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[TaskResponse]:
    # 轮询按 ID 读取任务存储的最新快照，不访问后台进程对象。
    return ApiResponse.ok(TaskResponse.from_record(task_service.require_task(task_id)))


# 日志 GET 读取保留窗口，不等待进程输出。
# 持续消费需要另走实时日志通道，不能把一次空返回当作任务结束。
@router.get("/{task_id}/logs", response_model=ApiResponse[list[LogEvent]])
# 历史接口用于回放持久化日志；实时增量由独立的 WebSocket 提供。
async def get_task_logs(
    task_id: UUID,
    task_service: Annotated[TaskService, Depends(get_task_service)],
    log_service: Annotated[TaskLogService, Depends(get_log_service)],
) -> ApiResponse[list[LogEvent]]:
    # 先检查任务存在；没有日志的有效任务与未知任务是不同结果。
    task_service.require_task(task_id)
    # 历史只包含当前保留窗口；客户端不应把没有早期日志当作任务从未运行。
    return ApiResponse.ok(log_service.history(str(task_id)))


# 清单包含相对路径及体积，不返回副本目录的服务器路径。
@router.get("/{task_id}/artifacts", response_model=ApiResponse[list[BuildArtifactResponse]])
async def list_task_artifacts(
    task_id: UUID,
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[list[BuildArtifactResponse]]:
    # 产物可见性和路径归属由平台检查，路由只映射下载所需信息。
    # 产物列表由任务服务按发布资格获取。
    # 磁盘上出现过的临时文件不必然属于公开下载范围。
    artifacts = task_service.list_task_artifacts(task_id)
    # 列表只序列化可公开产物的名字和长度，不在此读取整个文件内容。
    return ApiResponse.ok(
        [BuildArtifactResponse(path=path, size_bytes=size_bytes) for path, size_bytes in artifacts]
    )


# 路径转换器允许嵌套产物名，最终归属仍由文件服务验证。
@router.get(
    "/{task_id}/artifacts/{artifact_path:path}",
    response_class=FileResponse,
    responses={200: ARTIFACT_DOWNLOAD_RESPONSE},
)
# 清单和下载是不同请求，下载时仍需重新确认任务与产物的关系。
async def download_task_artifact(
    task_id: UUID,
    artifact_path: str,
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> FileResponse:
    # 下载重新解析并检查归属，不依赖客户端之前拿到的清单。
    # 下载使用完整的工作区相对路径定位文件；同名文件可能位于不同子目录。
    # 访问边界仍由任务服务核验。
    artifact = task_service.resolve_task_artifact(task_id, artifact_path)
    # 下载文件名用真实文件的末级名称，目录结构不写入附件名。
    return FileResponse(
        artifact,
        media_type="application/octet-stream",
        filename=artifact.name,
    )


# 取消接口可重复调用；是否完成由返回任务状态而非 HTTP 成功本身决定。
@router.post("/{task_id}/cancel", response_model=ApiResponse[TaskResponse])
# 运行中取消是协作信号，处理请求不等于底层进程已停止或文件已回收。
async def cancel_task(
    task_id: UUID,
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[TaskResponse]:
    # 响应表示平台已处理取消请求；运行中的任务仍需等待后台收尾。
    # 如果目标正在运行，响应可能只表示取消意图已登记。
    # 实际终态与回收结果仍以随后的任务查询为准。
    return ApiResponse.ok(TaskResponse.from_record(await task_service.cancel_task(task_id)))

