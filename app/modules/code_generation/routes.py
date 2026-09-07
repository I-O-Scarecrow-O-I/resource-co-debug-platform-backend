from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.code_generation.deps import get_code_generation_service
from app.modules.code_generation.schemas import (
    CodeGenerationCapabilities,
    CodeGenerationTaskRequest,
    NaturalCCHealthStatus,
)
from app.modules.code_generation.service import NaturalCCService
from app.platform.api.deps import get_task_service
from app.platform.schemas.common import ApiResponse
from app.platform.schemas.tasks import TaskResponse
from app.platform.services.task_service import TaskService

router = APIRouter()


@router.get("/health", response_model=ApiResponse[NaturalCCHealthStatus])
async def health(
    service: Annotated[NaturalCCService, Depends(get_code_generation_service)],
) -> ApiResponse[NaturalCCHealthStatus]:
    return ApiResponse.ok(await service.health())


@router.get("/capabilities", response_model=ApiResponse[CodeGenerationCapabilities])
async def capabilities(
    service: Annotated[NaturalCCService, Depends(get_code_generation_service)],
) -> ApiResponse[CodeGenerationCapabilities]:
    return ApiResponse.ok(service.capabilities())


@router.post("/tasks", response_model=ApiResponse[TaskResponse])
async def create_code_generation_task(
    request: CodeGenerationTaskRequest,
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[TaskResponse]:
    task = await task_service.create_code_generation_task(request)
    return ApiResponse.ok(TaskResponse.from_record(task))
