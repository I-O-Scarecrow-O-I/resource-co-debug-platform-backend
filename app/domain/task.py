from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.domain.enums import TaskStatus, TaskType


@dataclass(slots=True)
class TaskRecord:
    id: UUID
    project_id: UUID
    task_type: TaskType
    status: TaskStatus
    command: list[str]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    elapsed_ms: int | None = None
    progress: int = 0
    result: dict = field(default_factory=dict)
    error: str | None = None
    metadata: dict = field(default_factory=dict)
    cancel_requested: bool = False
