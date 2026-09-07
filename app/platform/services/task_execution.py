from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.core.errors import CancellationRequested


@dataclass(slots=True, frozen=True)
class PreparedProcess:
    """The local process a module asks the platform to execute."""

    command: list[str]
    work_dir: str = "."


LogAppender = Callable[[str, str], None]
ProgressReporter = Callable[[int, str], None]
CancelCheck = Callable[[], bool]


@dataclass(slots=True)
class TaskPreparationContext:
    """The bounded platform capabilities available while a module prepares a task."""

    task_id: UUID
    project_id: UUID
    workspace: Path
    _append_log: LogAppender = field(repr=False)
    _report_progress: ProgressReporter = field(repr=False)
    _is_cancelled: CancelCheck = field(repr=False)

    def log(self, message: str, stream: str = "module.prepare") -> None:
        self._append_log(message, stream)

    def report_progress(self, percent: int, message: str) -> None:
        self._report_progress(percent, message)

    def is_cancelled(self) -> bool:
        return self._is_cancelled()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancellationRequested()


class TaskPreparer(Protocol):
    async def __call__(
        self,
        context: TaskPreparationContext,
    ) -> PreparedProcess: ...
