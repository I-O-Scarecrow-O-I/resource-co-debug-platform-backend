from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_metric_service
from app.schemas.common import ApiResponse
from app.services.metric_service import AcceptanceMetricService

router = APIRouter()


@router.get("/build-success-rate", response_model=ApiResponse[float])
async def build_success_rate(
    success_count: int,
    total_count: int,
    metric_service: Annotated[AcceptanceMetricService, Depends(get_metric_service)],
) -> ApiResponse[float]:
    return ApiResponse.ok(metric_service.build_success_rate(success_count, total_count))


@router.get("/improvement-rate", response_model=ApiResponse[float])
async def improvement_rate(
    fifo_millis: int,
    optimized_millis: int,
    metric_service: Annotated[AcceptanceMetricService, Depends(get_metric_service)],
) -> ApiResponse[float]:
    return ApiResponse.ok(metric_service.improvement_rate(fifo_millis, optimized_millis))
