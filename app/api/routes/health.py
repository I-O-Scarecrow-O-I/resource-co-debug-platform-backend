from fastapi import APIRouter

from app.core.time import utc_now
from app.schemas.common import ApiResponse

router = APIRouter()


@router.get("/health", response_model=ApiResponse[dict])
async def health() -> ApiResponse[dict]:
    return ApiResponse.ok(
        {
            "service": "resource-co-debug-platform-backend",
            "status": "UP",
            "time": utc_now(),
        }
    )
