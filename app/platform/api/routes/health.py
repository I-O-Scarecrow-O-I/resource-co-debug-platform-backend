from fastapi import APIRouter

from app.core.time import utc_now
from app.platform.schemas.common import ApiResponse

router = APIRouter()


# 这是平台进程存活检查，不发起 NaturalCC、工具链或模型调用。
@router.get("/health", response_model=ApiResponse[dict])
async def health() -> ApiResponse[dict]:
    return ApiResponse.ok(
        {
            "service": "resource-co-debug-platform-backend",
            "status": "UP",
            "time": utc_now(),
        }
    )

