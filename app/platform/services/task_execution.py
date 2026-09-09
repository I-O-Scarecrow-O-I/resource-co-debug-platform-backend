from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.core.errors import CancellationRequested
from app.platform.domain.enums import TaskStatus


@dataclass(slots=True, frozen=True)
class PreparedProcess:
    """The local process a module asks the platform to execute."""

    command: list[str]
    work_dir: str = "."
    recorded_command: list[str] | None = None


LogAppender = Callable[[str, str], None]
ProgressReporter = Callable[[int, str], None]
CancelCheck = Callable[[], bool]
PathResolver = Callable[[str], Path]
WorkspaceCreator = Callable[[str | None], Path]
WorkspacePathResolver = Callable[[Path, str], Path]
ManagedLogAppender = Callable[[str, str, int | None], None]
ManagedProgressReporter = Callable[[int, str, str], None]
MetadataMerger = Callable[[dict[str, object]], None]
WorkspaceHolder = Callable[[dict[str, object] | None], None]
WorkspaceReleaser = Callable[[bool, dict[str, object] | None], None]


@dataclass(slots=True)
class TaskPreparationContext:
    """The bounded platform capabilities available while a module prepares a task."""

    task_id: UUID
    project_id: UUID
    workspace: Path
    _append_log: LogAppender = field(repr=False)
    _report_progress: ProgressReporter = field(repr=False)
    _is_cancelled: CancelCheck = field(repr=False)
    _resolve_path: PathResolver = field(repr=False)

    def log(self, message: str, stream: str = "module.prepare") -> None:
        self._append_log(message, stream)

    def report_progress(self, percent: int, message: str) -> None:
        self._report_progress(percent, message)

    def is_cancelled(self) -> bool:
        return self._is_cancelled()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancellationRequested()

    def resolve_path(self, path: str) -> Path:
        return self._resolve_path(path)


class TaskPreparer(Protocol):
    async def __call__(
        self,
        context: TaskPreparationContext,
    ) -> PreparedProcess: ...


@dataclass(slots=True)
class ManagedTaskContext:
    """Bounded platform capabilities for module-owned task orchestration."""

    task_id: UUID
    project_id: UUID
    timeout_seconds: int
    _append_log: ManagedLogAppender = field(repr=False)
    _report_progress: ManagedProgressReporter = field(repr=False)
    _is_cancelled: CancelCheck = field(repr=False)
    _create_workspace: WorkspaceCreator = field(repr=False)
    _resolve_path: WorkspacePathResolver = field(repr=False)
    _merge_metadata: MetadataMerger = field(repr=False)
    _hold_workspaces: WorkspaceHolder = field(repr=False)
    _release_workspaces: WorkspaceReleaser = field(repr=False)

    def log(
        self,
        message: str,
        stream: str = "module.managed",
        progress: int | None = None,
    ) -> None:
        self._append_log(message, stream, progress)

    def report_progress(
        self,
        percent: int,
        message: str,
        stream: str = "module.managed",
    ) -> None:
        self._report_progress(percent, message, stream)

    def is_cancelled(self) -> bool:
        return self._is_cancelled()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancellationRequested()

    def create_workspace(self, workspace_name: str | None = None) -> Path:
        return self._create_workspace(workspace_name)

    def resolve_path(self, workspace: Path, relative_path: str) -> Path:
        return self._resolve_path(workspace, relative_path)

    def merge_metadata(self, updates: dict[str, object]) -> None:
        self._merge_metadata(updates)

    def hold_workspaces(
        self,
        metadata_updates: dict[str, object] | None = None,
    ) -> None:
        self._hold_workspaces(metadata_updates)

    def release_workspaces(
        self,
        *,
        cleanup: bool = False,
        completion_metadata: dict[str, object] | None = None,
    ) -> None:
        self._release_workspaces(cleanup, completion_metadata)


@dataclass(slots=True, frozen=True)
class ManagedTaskResult:
    status: TaskStatus
    result: dict = field(default_factory=dict)
    exit_code: int | None = None
    elapsed_ms: int | None = None
    error: str | None = None


class ManagedTaskExecutor(Protocol):
    async def __call__(self, context: ManagedTaskContext) -> ManagedTaskResult: ...


class ManagedTaskCancellationHandler(Protocol):
    def __call__(
        self,
        task_id: UUID,
        deadline: float | None = None,
    ) -> Awaitable[None]: ...
