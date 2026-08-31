from uuid import UUID

from app.core.errors import NotFoundError
from app.domain.enums import TaskStatus
from app.domain.task import TaskRecord


class TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[UUID, TaskRecord] = {}

    def save(self, task: TaskRecord) -> TaskRecord:
        self._tasks[task.id] = task
        return task

    def require(self, task_id: UUID) -> TaskRecord:
        task = self._tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        return task

    def list(self) -> list[TaskRecord]:
        return sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)

    def request_cancel(self, task_id: UUID) -> TaskRecord:
        task = self.require(task_id)
        task.cancel_requested = True
        if task.status == TaskStatus.PENDING:
            task.status = TaskStatus.CANCELLED
        return task
