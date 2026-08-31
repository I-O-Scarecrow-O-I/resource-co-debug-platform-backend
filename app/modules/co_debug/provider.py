from app.modules.co_debug.routes import router
from app.platform.modules.contracts import BackendModule


def co_debug_module() -> BackendModule:
    return BackendModule(
        name="co_debug",
        route_prefix="/modules/co-debug",
        router=router,
    )
