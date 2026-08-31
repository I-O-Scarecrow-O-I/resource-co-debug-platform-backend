from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import get_debug_service
from app.schemas.common import ApiResponse
from app.schemas.debug import DebugSessionResponse
from app.services.debug_service import DebugSessionService

router = APIRouter()


@router.get("/sessions/{task_id}", response_model=ApiResponse[DebugSessionResponse])
async def describe_debug_session(
    task_id: UUID,
    debug_service: Annotated[DebugSessionService, Depends(get_debug_service)],
) -> ApiResponse[DebugSessionResponse]:
    return ApiResponse.ok(debug_service.describe(task_id))
