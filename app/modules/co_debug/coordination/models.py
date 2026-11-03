from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class DebugCoordinationGroupStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    CLOSED = "CLOSED"


class DebugGroupCommand(StrEnum):
    RUN = "run"
    CONTINUE = "continue"
    INTERRUPT = "interrupt"
    CLOSE_SESSIONS = "close_sessions"


class DebugGroupCommandOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class DebugCoordinationRuleAction(StrEnum):
    INTERRUPT = "interrupt"


class DebugCoordinationEventOutcome(StrEnum):
    PROCESSING = "PROCESSING"
    INTERRUPT_CONFIRMED = "INTERRUPT_CONFIRMED"
    INTERRUPT_SENT = "INTERRUPT_SENT"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass(slots=True, frozen=True)
class DebugCoordinationGroup:
    id: UUID
    project_id: UUID
    name: str
    created_at: datetime
    closed_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class DebugLogicalPartition:
    id: UUID
    group_id: UUID
    name: str
    session_task_id: UUID
    joined_at: datetime
    associated: bool = True
    removed_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class DebugCoordinationRule:
    id: UUID
    group_id: UUID
    source_partition_id: UUID
    source_breakpoint_number: str
    target_partition_id: UUID
    action: DebugCoordinationRuleAction
    created_at: datetime
    deleted_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class DebugCoordinationRuleBinding:
    rule: DebugCoordinationRule
    source: DebugLogicalPartition
    target: DebugLogicalPartition


@dataclass(slots=True, frozen=True)
class DebugCoordinationEventRecord:
    id: UUID
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
