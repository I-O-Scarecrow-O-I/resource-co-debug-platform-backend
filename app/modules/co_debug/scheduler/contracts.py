from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from app.core.errors import CancellationRequested


@dataclass(slots=True)
class TaskContext:
    task_id: UUID
    log: Callable[[str, str], object]
    progress: Callable[[int, str], object]
    is_cancelled: Callable[[], bool]

    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancellationRequested()

