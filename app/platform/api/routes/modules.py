from fastapi import APIRouter

from app.platform.modules.registry import get_backend_modules
from app.platform.schemas.common import ApiResponse
from app.platform.schemas.modules import BackendModuleResponse

router = APIRouter()


# 清单与实际注册的路由使用同一来源，避免前端依赖另一份硬编码模块表。
@router.get("", response_model=ApiResponse[list[BackendModuleResponse]])
async def list_backend_modules() -> ApiResponse[list[BackendModuleResponse]]:
    # 只投影名称、前缀和版本；路由对象与服务依赖不属于公开响应。
    modules = [
        BackendModuleResponse(
            name=backend_module.name,
            route_prefix=backend_module.route_prefix,
            version=backend_module.version,
        )
        for backend_module in get_backend_modules()
    ]
    return ApiResponse.ok(modules)
