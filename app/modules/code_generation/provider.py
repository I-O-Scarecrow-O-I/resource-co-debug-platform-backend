from app.modules.code_generation.routes import router
from app.platform.modules.contracts import BackendModule


def code_generation_module() -> BackendModule:
    return BackendModule(
        name="code_generation",
        route_prefix="/modules/code-generation",
        router=router,
    )
