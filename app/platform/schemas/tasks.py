from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.platform.domain.enums import BackendModuleName, SchedulerStrategy, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord


class TaskExecutionSpec(BaseModel):
    name: str
    command: list[str] = Field(min_length=1)
    estimated_ms: int = Field(default=1000, ge=1)
    depends_on: list[str] = Field(default_factory=list)
    preferred_core: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BuildTaskRequest(BaseModel):
    module: BackendModuleName = BackendModuleName.CO_DEBUG
    project_id: UUID
    command: list[str] = Field(default_factory=lambda: ["make"], min_length=1)
    work_dir: str = "."
    timeout_seconds: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DebugTaskRequest(BaseModel):
    module: BackendModuleName = BackendModuleName.CO_DEBUG
    project_id: UUID
    executable_path: str
    args: list[str] = Field(default_factory=list)
    work_dir: str = "."
    timeout_seconds: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScheduleExperimentRequest(BaseModel):
    module: BackendModuleName = BackendModuleName.CO_DEBUG
    project_id: UUID
    strategy: SchedulerStrategy = SchedulerStrategy.RESOURCE_AWARE
    tasks: list[TaskExecutionSpec] = Field(default_factory=list)
    core_ids: list[int] | None = Field(default=None, min_length=1)
    timeout_seconds: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScheduleWorkloadSpec(BaseModel):
    name: str
    tasks: list[TaskExecutionSpec] = Field(min_length=1)


class ScheduleComparisonRequest(BaseModel):
    module: BackendModuleName = BackendModuleName.CO_DEBUG
    project_id: UUID
    workloads: list[ScheduleWorkloadSpec] = Field(min_length=1, max_length=3)
    core_ids: list[int] | None = Field(default=None, min_length=1)
    timeout_seconds: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    id: UUID
    module: BackendModuleName
    project_id: UUID
    task_type: TaskType
    status: TaskStatus
    command: list[str]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    elapsed_ms: int | None
    progress: int
    result: dict[str, Any]
    error: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_record(cls, task: TaskRecord) -> "TaskResponse":
        return cls(
            id=task.id,
            module=task.module,
            project_id=task.project_id,
            task_type=task.task_type,
            status=task.status,
            command=task.command,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            exit_code=task.exit_code,
            elapsed_ms=task.elapsed_ms,
            progress=task.progress,
            result=task.result,
            error=task.error,
            metadata=task.metadata,
        )

