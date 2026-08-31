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
    ) -> SchedulePlan:
        context = TaskContext(
            task_id=task_id,
            log=lambda message, stream="co_debug.scheduler": self.log_service.append(
                task_id, message, stream=stream
            ),
            progress=lambda percent, message: self.log_service.append(
                task_id, message, stream="co_debug.scheduler", progress=percent
            ),
            is_cancelled=is_cancelled,
        )
        return plan_tasks(strategy=strategy, tasks=tasks, context=context)

