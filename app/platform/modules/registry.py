from app.modules.co_debug.provider import co_debug_module
from app.platform.modules.contracts import BackendModule


def get_backend_modules() -> list[BackendModule]:
    return [co_debug_module()]
