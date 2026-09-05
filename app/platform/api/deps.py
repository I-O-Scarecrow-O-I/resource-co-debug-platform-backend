import threading
import weakref
from functools import lru_cache

from app.core.config import Settings
from app.core.config import get_settings as load_settings
from app.modules.co_debug.services.debug_service import DebugSessionService
from app.modules.co_debug.services.dependency_service import DependencyAnalysisService
from app.modules.co_debug.services.metric_service import AcceptanceMetricService
from app.modules.co_debug.services.schedule_comparison_service import ScheduleComparisonService
from app.modules.co_debug.services.schedule_execution_service import ScheduleExecutionService
from app.modules.co_debug.services.scheduler_service import SchedulerService
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService

_task_services: weakref.WeakSet[TaskService] = weakref.WeakSet()
_task_services_lock = threading.Lock()


def get_settings() -> Settings:
    return load_settings()


@lru_cache
def get_log_service() -> TaskLogService:
    settings = get_settings()
    return TaskLogService(
        max_lines=settings.max_log_lines_per_task,
        database_path=settings.task_database_path,
    )


@lru_cache
def get_workspace_service() -> WorkspaceService:
    return WorkspaceService(storage_root=get_settings().storage_root)


@lru_cache
def get_task_store() -> TaskStore:
    return TaskStore(database_path=get_settings().task_database_path)


@lru_cache
def get_process_runner() -> ProcessRunner:
    return ProcessRunner()


@lru_cache
def get_scheduler_service() -> SchedulerService:
    return SchedulerService(log_service=get_log_service())


@lru_cache
def get_schedule_execution_service() -> ScheduleExecutionService:
    return ScheduleExecutionService(process_runner=get_process_runner())


@lru_cache
def get_schedule_comparison_service() -> ScheduleComparisonService:
    return ScheduleComparisonService(
        scheduler_service=get_scheduler_service(),
        execution_service=get_schedule_execution_service(),
        metric_service=get_metric_service(),
    )


@lru_cache
def get_task_service() -> TaskService:
    task_service = TaskService(
        workspace_service=get_workspace_service(),
        task_store=get_task_store(),
        log_service=get_log_service(),
        process_runner=get_process_runner(),
        scheduler_service=get_scheduler_service(),
        schedule_execution_service=get_schedule_execution_service(),
        schedule_comparison_service=get_schedule_comparison_service(),
        default_timeout_seconds=get_settings().default_task_timeout_seconds,
    )
    with _task_services_lock:
        _task_services.add(task_service)
    return task_service


def clear_task_service_cache() -> None:
    get_task_service.cache_clear()


def _ensure_task_services_can_close(resource_name: str) -> None:
    with _task_services_lock:
        active_services = [
            task_service
            for task_service in _task_services
            if not task_service.can_close_resources()
        ]
    if active_services:
        raise RuntimeError(f"task service must be shut down before clearing its {resource_name}")


def clear_log_service_cache(*, close: bool = True) -> None:
    if close:
        _ensure_task_services_can_close("log service")
    get_task_service.cache_clear()
    get_schedule_comparison_service.cache_clear()
    get_scheduler_service.cache_clear()
    if close and get_log_service.cache_info().currsize:
        get_log_service().close()
    get_log_service.cache_clear()


def clear_task_store_cache(*, close: bool = True) -> None:
    if close:
        _ensure_task_services_can_close("task store")
    get_task_service.cache_clear()
    get_debug_service.cache_clear()
    if close and get_task_store.cache_info().currsize:
        get_task_store().close()
    get_task_store.cache_clear()


@lru_cache
def get_dependency_service() -> DependencyAnalysisService:
    return DependencyAnalysisService(workspace_service=get_workspace_service())


@lru_cache
def get_debug_service() -> DebugSessionService:
    return DebugSessionService(task_store=get_task_store())


@lru_cache
def get_metric_service() -> AcceptanceMetricService:
    return AcceptanceMetricService()

