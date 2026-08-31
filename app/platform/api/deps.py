from functools import lru_cache

from app.core.config import Settings
from app.core.config import get_settings as load_settings
from app.platform.services.debug_service import DebugSessionService
from app.platform.services.dependency_service import DependencyAnalysisService
from app.platform.services.log_service import TaskLogService
from app.platform.services.metric_service import AcceptanceMetricService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.scheduler_service import SchedulerService
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


def get_settings() -> Settings:
    return load_settings()


@lru_cache
def get_log_service() -> TaskLogService:
    return TaskLogService(max_lines=get_settings().max_log_lines_per_task)


@lru_cache
def get_workspace_service() -> WorkspaceService:
    return WorkspaceService(storage_root=get_settings().storage_root)


@lru_cache
def get_task_store() -> TaskStore:
    return TaskStore()


@lru_cache
def get_process_runner() -> ProcessRunner:
    return ProcessRunner()


@lru_cache
def get_scheduler_service() -> SchedulerService:
    return SchedulerService(log_service=get_log_service())


@lru_cache
def get_task_service() -> TaskService:
    return TaskService(
        workspace_service=get_workspace_service(),
        task_store=get_task_store(),
        log_service=get_log_service(),
        process_runner=get_process_runner(),
        scheduler_service=get_scheduler_service(),
        default_timeout_seconds=get_settings().default_task_timeout_seconds,
    )


@lru_cache
def get_dependency_service() -> DependencyAnalysisService:
    return DependencyAnalysisService(workspace_service=get_workspace_service())


@lru_cache
def get_debug_service() -> DebugSessionService:
    return DebugSessionService(task_store=get_task_store())


@lru_cache
def get_metric_service() -> AcceptanceMetricService:
    return AcceptanceMetricService()

