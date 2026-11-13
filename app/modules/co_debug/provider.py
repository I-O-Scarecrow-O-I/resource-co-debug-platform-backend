from app.modules.co_debug.routes import router
from app.platform.modules.contracts import BackendModule


# 模块提供相对于平台 API 根的挂载点，HTTP 路由由平台统一装配。
def co_debug_module() -> BackendModule:
    return BackendModule(
        name="co_debug",
        route_prefix="/modules/co-debug",
        router=router,
    )
