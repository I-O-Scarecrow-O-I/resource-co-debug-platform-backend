from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import get_dependency_service
from app.schemas.common import ApiResponse
from app.schemas.dependencies import DependencyAnalysisResponse
from app.services.dependency_service import DependencyAnalysisService

router = APIRouter()


@router.post("/analyze", response_model=ApiResponse[DependencyAnalysisResponse])
async def analyze_dependencies(
    project_id: UUID,
    dependency_service: Annotated[DependencyAnalysisService, Depends(get_dependency_service)],
) -> ApiResponse[DependencyAnalysisResponse]:
    return ApiResponse.ok(dependency_service.analyze(project_id))
