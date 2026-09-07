from app.modules.co_debug.provider import co_debug_module
from app.modules.code_generation.provider import code_generation_module
from app.platform.modules.contracts import BackendModule


def get_backend_modules() -> list[BackendModule]:
    return [co_debug_module(), code_generation_module()]
