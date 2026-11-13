from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.code_generation.deps import (
    get_code_generation_service,
    get_code_generation_task_service,
)
from app.modules.code_generation.schemas import (
    CodeGenerationCapabilities,
    CodeGenerationTaskRequest,
    NaturalCCHealthStatus,
)
from app.modules.code_generation.service import NaturalCCService
from app.modules.code_generation.task_service import CodeGenerationTaskService
from app.platform.schemas.common import ApiResponse
from app.platform.schemas.tasks import TaskResponse

router = APIRouter()


# 上游应用不可达以健康数据表达；这一接口不会启动生成任务。
@router.get("/health", response_model=ApiResponse[NaturalCCHealthStatus])
async def health(
    service: Annotated[NaturalCCService, Depends(get_code_generation_service)],
) -> ApiResponse[NaturalCCHealthStatus]:
    # 响应成功指平台正常取得健康结论；应用不可用仍以数据中的 status 表达。
    # 客户端不能只看 HTTP 状态判断 NaturalCC 可用。
    return ApiResponse.ok(await service.health())


# 能力清单可在未启动远端运行时查询，不等同于一次模型验证。
@router.get("/capabilities", response_model=ApiResponse[CodeGenerationCapabilities])
async def capabilities(
    service: Annotated[NaturalCCService, Depends(get_code_generation_service)],
) -> ApiResponse[CodeGenerationCapabilities]:
    # 这是适配器声明的能力，不读取某个运行的模型、线程或授权。
    # 前端可用它展示操作入口，再单独查询服务健康。
    return ApiResponse.ok(service.capabilities())


# 返回平台任务记录供统一轮询；上游运行 ID 和实际文件在任务结果或产物接口获取。
@router.post("/tasks", response_model=ApiResponse[TaskResponse])
async def create_code_generation_task(
    request: CodeGenerationTaskRequest,
    task_service: Annotated[
        CodeGenerationTaskService,
        Depends(get_code_generation_task_service),
    ],
) -> ApiResponse[TaskResponse]:
    # 统一任务结果和产物尚未产生时，创建响应仍可返回登记身份。
    # 完成、修复和重构都用同一任务轮询契约。
    task = await task_service.create_code_generation_task(request)
    return ApiResponse.ok(TaskResponse.from_record(task))
