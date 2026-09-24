from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DebugBatchJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    executable_path: str = Field(min_length=1)
    breakpoint: str = Field(
        pattern=r"^(?:[A-Za-z_][A-Za-z0-9_:]*|[A-Za-z0-9_./-]+:[1-9][0-9]*)$"
    )
    args: list[str] = Field(default_factory=list)
    estimated_ms: int = Field(default=1000, ge=1)


class DebugBatchWorkload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    work_dir: str = "."
    jobs: list[DebugBatchJob] = Field(min_length=2)


class DebugWorkloadManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    workloads: list[DebugBatchWorkload] = Field(min_length=1, max_length=3)


class DebugComparisonRequest(BaseModel):
    project_id: UUID
    build_task_id: UUID | None = None
    manifest_path: str = "debug-workloads.json"
    core_ids: list[int] | None = Field(default=None, min_length=1)
    timeout_seconds: int | None = Field(default=None, ge=1)
