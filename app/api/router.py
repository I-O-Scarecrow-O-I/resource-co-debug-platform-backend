from fastapi import APIRouter

from app.api.routes import debug, dependencies, health, metrics, projects, tasks

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(dependencies.router, prefix="/dependencies", tags=["dependencies"])
api_router.include_router(debug.router, prefix="/debug", tags=["debug"])
api_router.include_router(metrics.router, prefix="/metrics", tags=["metrics"])
