from uuid import UUID

from app.platform.domain.enums import TaskType
from app.platform.schemas.debug import DebugSessionResponse
from app.platform.services.task_store import TaskStore


class DebugSessionService:
    def __init__(self, task_store: TaskStore) -> None:
        self.task_store = task_store

    def describe(self, task_id: UUID) -> DebugSessionResponse:
        task = self.task_store.require(task_id)
        note = "GDB/MI command channel is reserved for the next implementation step."
        if task.task_type != TaskType.DEBUG:
            note = (
                "The task exists but is not a DEBUG task. "
                "Create a debug task before using GDB/MI."
            )
        return DebugSessionResponse(
            task_id=task_id,
            protocol="GDB/MI",
            supported_commands=[
                "-break-insert",
                "-exec-run",
                "-exec-continue",
                "-exec-next",
                "-exec-step",
            ],
            note=note,
        )

