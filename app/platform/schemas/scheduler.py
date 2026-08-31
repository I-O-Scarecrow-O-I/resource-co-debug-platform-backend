from pydantic import BaseModel

from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec


class SchedulePlan(BaseModel):
    strategy: SchedulerStrategy
    ordered_tasks: list[TaskExecutionSpec]
    estimated_total_ms: int
    notes: list[str]

