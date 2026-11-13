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


# 请求引用已有调试任务作为逻辑分区，不接受进程标识或二进制路径。
class DebugLogicalPartitionRequest(BaseModel):
    # 逻辑名是组内显示和识别标记，不是 GDB 自己的线程名。
    partition_name: str = Field(min_length=1, max_length=128)
    session_task_id: UUID

    # 去掉边缘空白后再做非空和后续组内唯一性检查，避免视觉重复。
    @field_validator("partition_name")
    @classmethod
    def normalize_partition_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("partition_name must not be blank")
        return normalized


class DebugCoordinationGroupRequest(BaseModel):
    # 组中会话必须同项目；请求模型先验证形状，服务再查询实时任务归属。
    project_id: UUID
    name: str = Field(min_length=1, max_length=128)
    # 创建至少需要两个独立分区；后续移除可能使组进入降级状态。
    members: list[DebugLogicalPartitionRequest] = Field(min_length=2)

    # 显示名的标准化与成员名称采用相同空白规则，不改内部连续空格。
    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_unique_members(self) -> Self:
        # 展示名称与调试任务分别要求组内唯一。
        # 空白归一化先于检查，避免仅靠首尾空格形成两个看似相同的分区。
        partition_names = [member.partition_name for member in self.members]
        if len(partition_names) != len(set(partition_names)):
            raise ValueError("partition names must be unique within a coordination group")
        # 一个会话不能用两个别名同时加入同组；跨组活动关联由存储约束。
        session_ids = [member.session_task_id for member in self.members]
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("session task IDs must be unique within a coordination group")
        return self


class DebugLogicalPartitionResponse(BaseModel):
    # 分区身份在解除关联后仍可查看，调试任务身份不会因移除而重写。
    partition_id: UUID
    partition_name: str
    session_task_id: UUID
    # 关联历史与会话现场分开表达。
    # 已移除或暂不可查询的成员仍保留标识和加入时间。
    associated: bool
    joined_at: datetime
    removed_at: datetime | None
    # 历史成员可能没有实时快照。
    # 仍关联但查询失败时使用独立 error 字段说明。
    session: DebugSessionStateResponse | None = None
    error: str | None = None


class DebugCoordinationGroupResponse(BaseModel):
    group_id: UUID
    project_id: UUID
    name: str
    # 组状态是关联和活动会话的聚合，不能代替逐个查看成员状态。
    status: DebugCoordinationGroupStatus
    created_at: datetime
    closed_at: datetime | None
    # 只统计仍关联且活跃的会话，成员列表还可能包含已移除的历史行。
    active_member_count: int
    members: list[DebugLogicalPartitionResponse]


class DebugGroupMemberCommandResult(BaseModel):
    # 结果携带分区和任务身份，以便结果顺序不同仍能对应到界面成员。
    partition_id: UUID
    partition_name: str
    session_task_id: UUID
    # 成员结果区分未发送与执行失败，并尽量附状态快照用于定位。
    outcome: DebugGroupCommandOutcome
    state: DebugSessionStateResponse | None = None
    error: str | None = None


class DebugGroupCommandResponse(BaseModel):
    group_id: UUID
    # 同一组可有部分成员成功；整体布尔值不能替代逐成员回执。
    command: DebugGroupCommand
    # 组级布尔值只表示成员结果全部成功；局部结果始终可从列表查看。
    all_succeeded: bool
    results: list[DebugGroupMemberCommandResult]


class DebugCoordinationRuleRequest(BaseModel):
    # 来源和目标均为组内分区 ID，不直接接受任意会话任务或系统 PID。
    source_partition_id: UUID
    # 断点编号以字符串保留 GDB 的身份格式，并与来源分区共同定位。
    source_breakpoint_number: str = Field(min_length=1, max_length=64)
    target_partition_id: UUID

    @field_validator("source_breakpoint_number")
    @classmethod
    # 仅标准化空白；编号在该会话中是否存在由服务验证。
    def normalize_breakpoint_number(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source_breakpoint_number must not be blank")
        return normalized


class DebugCoordinationRuleResponse(BaseModel):
    # 规则 ID 用于后续删除；响应保留来源断点和目标分区供核对触发方向。
    rule_id: UUID
    group_id: UUID
    source_partition_id: UUID
    source_breakpoint_number: str
    target_partition_id: UUID
    # 动作由服务固定为中断，不由请求传入任意调试命令。
    action: DebugCoordinationRuleAction
    created_at: datetime


class DebugCoordinationEventResponse(BaseModel):
    # 同一来源事件可以产生多条规则处理记录，两个事件标识不能互换。
    event_id: UUID
    source_event_id: UUID
    group_id: UUID
    rule_id: UUID
    # 同时保留逻辑分区和实际会话任务，以便成员移除后仍可追查事件。
    source_partition_id: UUID
    source_session_task_id: UUID
    target_partition_id: UUID
    target_session_task_id: UUID
    breakpoint_number: str
    # 发生时间来自源事件，处理时间来自规则结果。
    # 命令发送与实际暂停分别记录，不能仅由时间顺序推断因果。
    # 命中与处理是两个时间点；处理时间可能晚于命中，例如等待同组操作锁。
    occurred_at: datetime
    processed_at: datetime
    # 处理结果区分跳过、失败、仅发送和确认暂停，不能只看一个布尔值。
    outcome: DebugCoordinationEventOutcome
    # 目标已停可能没有发送命令；发送成功也可能尚未观察到暂停。
    command_sent: bool
    target_paused: bool
    message: str
