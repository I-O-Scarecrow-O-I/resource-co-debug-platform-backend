import threading
from uuid import UUID

from app.core.errors import NotFoundError
from app.core.time import utc_now
from app.platform.domain.enums import TaskStatus
from app.platform.domain.task import TaskRecord


class TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[UUID, TaskRecord] = {}
        self._lock = threading.RLock()

    def save(self, task: TaskRecord) -> TaskRecord:
        with self._lock:
            self._tasks[task.id] = task
        return task

    def require(self, task_id: UUID) -> TaskRecord:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        return task

    def list(self) -> list[TaskRecord]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)

    def request_cancel(self, task_id: UUID) -> TaskRecord:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            task.cancel_requested = True
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED
            return task

    def try_start(self, task_id: UUID) -> TaskRecord | None:
        """Atomically move a task from PENDING to RUNNING."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if task.status != TaskStatus.PENDING:
                return None
            task.status = TaskStatus.RUNNING
            task.started_at = utc_now()
            return task

