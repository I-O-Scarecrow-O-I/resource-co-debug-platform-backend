from fastapi import APIRouter

from app.platform.api.routes import health, modules, projects, tasks
from app.platform.modules.registry import get_backend_modules

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(modules.router, prefix="/modules", tags=["modules"])

for backend_module in get_backend_modules():
    api_router.include_router(
        backend_module.router,
        prefix=backend_module.route_prefix,
        tags=[backend_module.name],
    )
