from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.modules.co_debug.schemas.debug import DebugSessionResponse
from app.modules.co_debug.schemas.dependencies import DependencyAnalysisResponse
from app.modules.co_debug.services.debug_service import DebugSessionService
from app.modules.co_debug.services.dependency_service import DependencyAnalysisService
from app.modules.co_debug.services.metric_service import AcceptanceMetricService
from app.platform.api.deps import get_debug_service, get_dependency_service, get_metric_service
from app.platform.schemas.common import ApiResponse

router = APIRouter()


@router.post("/dependencies/analyze", response_model=ApiResponse[DependencyAnalysisResponse])
async def analyze_dependencies(
    project_id: UUID,
    dependency_service: Annotated[DependencyAnalysisService, Depends(get_dependency_service)],
) -> ApiResponse[DependencyAnalysisResponse]:
    return ApiResponse.ok(dependency_service.analyze(project_id))


@router.get("/debug/sessions/{task_id}", response_model=ApiResponse[DebugSessionResponse])
async def describe_debug_session(
    task_id: UUID,
    debug_service: Annotated[DebugSessionService, Depends(get_debug_service)],
) -> ApiResponse[DebugSessionResponse]:
    return ApiResponse.ok(debug_service.describe(task_id))


@router.get("/metrics/build-success-rate", response_model=ApiResponse[float])
async def build_success_rate(
    success_count: int,
    total_count: int,
    metric_service: Annotated[AcceptanceMetricService, Depends(get_metric_service)],
) -> ApiResponse[float]:
    return ApiResponse.ok(metric_service.build_success_rate(success_count, total_count))


@router.get("/metrics/improvement-rate", response_model=ApiResponse[float])
async def improvement_rate(
    fifo_millis: int,
    optimized_millis: int,
    metric_service: Annotated[AcceptanceMetricService, Depends(get_metric_service)],
) -> ApiResponse[float]:
    return ApiResponse.ok(metric_service.improvement_rate(fifo_millis, optimized_millis))
