from app.modules.co_debug.provider import co_debug_module
from app.modules.code_generation.provider import code_generation_module
from app.modules.vulnerability.provider import vulnerability_module
from app.platform.modules.contracts import BackendModule


# 模块列表显式接线，调用方拿到能力描述而不是任意模块扫描结果。
def get_backend_modules() -> list[BackendModule]:
    return [co_debug_module(), code_generation_module(), vulnerability_module()]
