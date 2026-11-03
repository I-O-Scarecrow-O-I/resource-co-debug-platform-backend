from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.co_debug.coordination.models import (
    DebugCoordinationEventOutcome,
    DebugCoordinationGroupStatus,
    DebugCoordinationRuleAction,
    DebugGroupCommand,
    DebugGroupCommandOutcome,
)
from app.modules.co_debug.schemas.debug import DebugSessionStateResponse


class DebugLogicalPartitionRequest(BaseModel):
    partition_name: str = Field(min_length=1, max_length=128)
    session_task_id: UUID

    @field_validator("partition_name")
    @classmethod
    def normalize_partition_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("partition_name must not be blank")
        return normalized


class DebugCoordinationGroupRequest(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=128)
    members: list[DebugLogicalPartitionRequest] = Field(min_length=2)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_unique_members(self) -> Self:
        partition_names = [member.partition_name for member in self.members]
        if len(partition_names) != len(set(partition_names)):
            raise ValueError("partition names must be unique within a coordination group")
        session_ids = [member.session_task_id for member in self.members]
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("session task IDs must be unique within a coordination group")
        return self


class DebugLogicalPartitionResponse(BaseModel):
    partition_id: UUID
    partition_name: str
    session_task_id: UUID
    associated: bool
    joined_at: datetime
    removed_at: datetime | None
    session: DebugSessionStateResponse | None = None
    error: str | None = None


class DebugCoordinationGroupResponse(BaseModel):
    group_id: UUID
    project_id: UUID
    name: str
    status: DebugCoordinationGroupStatus
    created_at: datetime
    closed_at: datetime | None
    active_member_count: int
    members: list[DebugLogicalPartitionResponse]


class DebugGroupMemberCommandResult(BaseModel):
    partition_id: UUID
    partition_name: str
    session_task_id: UUID
    outcome: DebugGroupCommandOutcome
    state: DebugSessionStateResponse | None = None
    error: str | None = None


class DebugGroupCommandResponse(BaseModel):
    group_id: UUID
    command: DebugGroupCommand
    all_succeeded: bool
    results: list[DebugGroupMemberCommandResult]


class DebugCoordinationRuleRequest(BaseModel):
    source_partition_id: UUID
    source_breakpoint_number: str = Field(min_length=1, max_length=64)
    target_partition_id: UUID

    @field_validator("source_breakpoint_number")
    @classmethod
    def normalize_breakpoint_number(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source_breakpoint_number must not be blank")
        return normalized


class DebugCoordinationRuleResponse(BaseModel):
    rule_id: UUID
    group_id: UUID
    source_partition_id: UUID
    source_breakpoint_number: str
    target_partition_id: UUID
    action: DebugCoordinationRuleAction
    created_at: datetime


class DebugCoordinationEventResponse(BaseModel):
    event_id: UUID
    source_event_id: UUID
    group_id: UUID
    rule_id: UUID
    source_partition_id: UUID
    source_session_task_id: UUID
    target_partition_id: UUID
    target_session_task_id: UUID
    breakpoint_number: str
    occurred_at: datetime
    processed_at: datetime
    outcome: DebugCoordinationEventOutcome
    command_sent: bool
    target_paused: bool
    message: str
