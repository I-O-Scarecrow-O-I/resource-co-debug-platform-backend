from fastapi import APIRouter

from app.platform.api.routes import health, modules, projects, tasks
from app.platform.modules.registry import get_backend_modules

# 平台通用接口固定挂载，工程、任务及能力查询有稳定路径。
api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(modules.router, prefix="/modules", tags=["modules"])

# 业务模块通过显式注册项提供路由前缀，应用工厂统一加 API 版本前缀。
for backend_module in get_backend_modules():
    api_router.include_router(
        backend_module.router,
        prefix=backend_module.route_prefix,
        tags=[backend_module.name],
    )
