from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.platform.domain.enums import TaskStatus


class DebugExecutableCandidate(BaseModel):
    name: str
    executable_path: str


class DebugBuildCandidate(BaseModel):
    project_id: UUID
    build_task_id: UUID
    build_kind: str
    created_at: datetime
    executables: list[DebugExecutableCandidate] = Field(
        default_factory=list
    )


class DebugSessionResponse(BaseModel):
    task_id: UUID
    protocol: str
    supported_commands: list[str]
    note: str


class DebugSessionStateResponse(BaseModel):
    task_id: UUID
    task_status: TaskStatus
    active: bool

    state: str

    stop_reason: str | None = None
    current_file: str | None = None
    current_line: int | None = None
    current_function: str | None = None
    current_thread_id: str | None = None
    stopped_thread_id: str | None = None
    breakpoint_hit_thread_id: str | None = None

    breakpoints: list[
        dict[str, Any]
    ] = Field(
        default_factory=list
    )

    console_output: list[str] = Field(
        default_factory=list
    )

    target_output: list[str] = Field(
        default_factory=list
    )

    log_output: list[str] = Field(
        default_factory=list
    )

    stderr_output: list[str] = Field(
        default_factory=list
    )


class DebugArgumentsRequest(BaseModel):
    arguments: list[str] = Field(
        default_factory=list
    )


class DebugBreakpointRequest(BaseModel):
    location: str = Field(
        min_length=1
    )

    temporary: bool = False
    disabled: bool = False
    condition: str | None = None


class DebugExpressionRequest(BaseModel):
    expression: str = Field(
        min_length=1
    )


class DebugThreadSelectRequest(BaseModel):
    thread_id: str = Field(min_length=1, pattern=r"^\d+$")


class DebugWaitForStopRequest(BaseModel):
    timeout_seconds: (
        float
        | None
    ) = Field(
        default=None,
        gt=0,
    )


class DebugBreakpointResponse(BaseModel):
    number: str
    location: str
    enabled: bool

    file: str | None = None
    fullname: str | None = None
    line: int | None = None
    function: str | None = None


class DebugExpressionResponse(BaseModel):
    value: str | None = None


class DebugStackFramesResponse(BaseModel):
    frames: list[Any] = Field(
        default_factory=list
    )


class DebugLocalVariablesResponse(BaseModel):
    variables: list[Any] = Field(default_factory=list)


class DebugThreadResponse(BaseModel):
    thread_id: str
    linux_tid: int | None = None
    name: str | None = None
    state: str
    core: int | None = None
    current: bool = False
    breakpoint_hit: bool = False


class DebugThreadsResponse(BaseModel):
    threads: list[DebugThreadResponse] = Field(default_factory=list)
    current_thread_id: str | None = None
    stopped_thread_id: str | None = None
    breakpoint_hit_thread_id: str | None = None
