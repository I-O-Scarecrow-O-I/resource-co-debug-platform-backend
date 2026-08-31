from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.platform.api.deps import get_log_service, get_task_service
from app.platform.schemas.common import ApiResponse, LogEvent
from app.platform.schemas.tasks import (
    BuildTaskRequest,
    DebugTaskRequest,
    ScheduleExperimentRequest,
    TaskResponse,
)
from app.platform.services.log_service import TaskLogService
from app.platform.services.task_service import TaskService

router = APIRouter()


@router.post("/build", response_model=ApiResponse[TaskResponse])
async def create_build_task(
    request: BuildTaskRequest,
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[TaskResponse]:
    task = await task_service.create_build_task(request)
    return ApiResponse.ok(TaskResponse.from_record(task))


@router.post("/debug", response_model=ApiResponse[TaskResponse])
async def create_debug_task(
    request: DebugTaskRequest,
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[TaskResponse]:
    task = await task_service.create_debug_task(request)
    return ApiResponse.ok(TaskResponse.from_record(task))


@router.post("/schedule-experiments", response_model=ApiResponse[TaskResponse])
async def create_schedule_experiment(
    request: ScheduleExperimentRequest,
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[TaskResponse]:
    task = await task_service.create_schedule_experiment(request)
    return ApiResponse.ok(TaskResponse.from_record(task))


@router.get("", response_model=ApiResponse[list[TaskResponse]])
async def list_tasks(
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[list[TaskResponse]]:
    return ApiResponse.ok([TaskResponse.from_record(task) for task in task_service.list_tasks()])


@router.get("/{task_id}", response_model=ApiResponse[TaskResponse])
async def get_task(
    task_id: UUID,
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[TaskResponse]:
    return ApiResponse.ok(TaskResponse.from_record(task_service.require_task(task_id)))


@router.get("/{task_id}/logs", response_model=ApiResponse[list[LogEvent]])
async def get_task_logs(
    task_id: UUID,
    task_service: Annotated[TaskService, Depends(get_task_service)],
    log_service: Annotated[TaskLogService, Depends(get_log_service)],
) -> ApiResponse[list[LogEvent]]:
    task_service.require_task(task_id)
    return ApiResponse.ok(log_service.history(str(task_id)))


@router.post("/{task_id}/cancel", response_model=ApiResponse[TaskResponse])
async def cancel_task(
    task_id: UUID,
    task_service: Annotated[TaskService, Depends(get_task_service)],
) -> ApiResponse[TaskResponse]:
    return ApiResponse.ok(TaskResponse.from_record(await task_service.cancel_task(task_id)))

