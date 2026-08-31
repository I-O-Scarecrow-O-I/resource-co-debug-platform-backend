from collections.abc import Callable
from uuid import UUID

from app.domain.enums import SchedulerStrategy
from app.module_c.contracts import TaskContext
from app.module_c.scheduler import plan_tasks
from app.schemas.scheduler import SchedulePlan
from app.schemas.tasks import TaskExecutionSpec
from app.services.log_service import TaskLogService


class SchedulerService:
    def __init__(self, log_service: TaskLogService) -> None:
        self.log_service = log_service

    def create_plan(
        self,
        task_id: UUID,
        strategy: SchedulerStrategy,
        tasks: list[TaskExecutionSpec],
        is_cancelled: Callable[[], bool],
    ) -> SchedulePlan:
        context = TaskContext(
            task_id=task_id,
            log=lambda message, stream="module_c": self.log_service.append(
                task_id, message, stream=stream
            ),
            progress=lambda percent, message: self.log_service.append(
                task_id, message, stream="module_c", progress=percent
            ),
            is_cancelled=is_cancelled,
        )
        return plan_tasks(strategy=strategy, tasks=tasks, context=context)
