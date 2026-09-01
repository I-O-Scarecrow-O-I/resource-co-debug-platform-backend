import os
from collections.abc import Callable
from uuid import UUID

from app.modules.co_debug.scheduler.contracts import TaskContext
from app.modules.co_debug.scheduler.scheduler import plan_tasks
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.scheduler import SchedulePlan
from app.platform.schemas.tasks import TaskExecutionSpec
from app.platform.services.log_service import TaskLogService


class SchedulerService:
    def __init__(self, log_service: TaskLogService) -> None:
        self.log_service = log_service

    def create_plan(
        self,
        task_id: UUID,
        strategy: SchedulerStrategy,
        tasks: list[TaskExecutionSpec],
        is_cancelled: Callable[[], bool],
        core_ids: list[int] | None = None,
        progress_start: int = 10,
        progress_end: int = 35,
    ) -> SchedulePlan:
        context = TaskContext(
            task_id=task_id,
            log=lambda message, stream="co_debug.scheduler": self.log_service.append(
                task_id, message, stream=stream
            ),
            progress=lambda percent, message: self.log_service.append(
                task_id,
                message,
                stream="co_debug.scheduler",
                progress=progress_start
                + round(percent * (progress_end - progress_start) / 100),
            ),
            is_cancelled=is_cancelled,
        )
        selected_cores = _default_core_ids() if core_ids is None else core_ids
        return plan_tasks(
            strategy=strategy,
            tasks=tasks,
            context=context,
            core_ids=selected_cores,
        )


def _default_core_ids() -> list[int]:
    get_affinity = getattr(os, "sched_getaffinity", None)
    if get_affinity is not None:
        try:
            allowed_cores = sorted(get_affinity(0))
        except OSError:
            pass
        else:
            if allowed_cores:
                return allowed_cores

    return list(range(os.cpu_count() or 1))

