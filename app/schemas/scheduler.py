from pydantic import BaseModel

from app.domain.enums import SchedulerStrategy
from app.schemas.tasks import TaskExecutionSpec


class SchedulePlan(BaseModel):
    strategy: SchedulerStrategy
    ordered_tasks: list[TaskExecutionSpec]
    estimated_total_ms: int
    notes: list[str]
