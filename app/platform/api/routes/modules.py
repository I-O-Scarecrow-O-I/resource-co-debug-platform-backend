from fastapi import APIRouter

from app.platform.modules.registry import get_backend_modules
from app.platform.schemas.common import ApiResponse
from app.platform.schemas.modules import BackendModuleResponse

router = APIRouter()


@router.get("", response_model=ApiResponse[list[BackendModuleResponse]])
async def list_backend_modules() -> ApiResponse[list[BackendModuleResponse]]:
    modules = [
        BackendModuleResponse(
            name=backend_module.name,
            route_prefix=backend_module.route_prefix,
            version=backend_module.version,
        )
        for backend_module in get_backend_modules()
    ]
    return ApiResponse.ok(modules)
