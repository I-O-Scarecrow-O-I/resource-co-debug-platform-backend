from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


# 组状态描述协作能力，成员会话仍各自维护 GDB 状态。
# 关闭是组的持久关联状态；活动或降级则由当前成员现场派生。
class DebugCoordinationGroupStatus(StrEnum):
    ACTIVE = "ACTIVE"
    # 降级不删除持久组，可能因活动成员数不足或某个会话无法查询而出现。
    DEGRADED = "DEGRADED"
    # 关闭表示主动撤销组关系；底层会话仍可独立运行。
    CLOSED = "CLOSED"


# 关闭成员进程的命令不隐式删除组关系，历史查询仍可保留组及分区身份。
class DebugGroupCommand(StrEnum):
    RUN = "run"
    CONTINUE = "continue"
    INTERRUPT = "interrupt"
    # 关闭会话作用于运行资源，和解除组的关联关系是不同命令。
    CLOSE_SESSIONS = "close_sessions"


# 成员级结果允许同一组命令部分成功，跳过与命令失败分别呈现。
# SKIPPED 表示前置状态不适用；FAILED 表示查状态或执行命令发生错误。
class DebugGroupCommandOutcome(StrEnum):
    # 成功是单成员调用结果，不表示其他成员也成功或目标已经异步暂停。
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    # 跳过表示前置条件未满足，未必要把它理解为底层 GDB 故障。
    SKIPPED = "SKIPPED"


class DebugCoordinationRuleAction(StrEnum):
    INTERRUPT = "interrupt"


# 发送中断与确认暂停是不同证据，记录需要区分两种结果。
# 事件先认领，再记录命令送达与停止确认。
# PROCESSING 是持久中间态，不能作为动作已完成的凭证。
class DebugCoordinationEventOutcome(StrEnum):
    # 认领动作先落处理中标记，为重复投递提供唯一性边界。
    PROCESSING = "PROCESSING"
    INTERRUPT_CONFIRMED = "INTERRUPT_CONFIRMED"
    # 发送后尚未确认停止时保留此状态，避免将命令接受误作实际暂停。
    INTERRUPT_SENT = "INTERRUPT_SENT"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


# 组记录持有工程归属和关闭时刻，不拥有实际调试进程。
@dataclass(slots=True, frozen=True)
class DebugCoordinationGroup:
    # 组持有项目归属和时间信息，不持有 GDB 会话对象。
    id: UUID
    # 项目标识约束所有成员的归属，组名只供显示。
    project_id: UUID
    name: str
    created_at: datetime
    # 显式关闭时间在数据库保存，活动状态则根据每次读取的会话重新派生。
    closed_at: datetime | None = None


# 逻辑分区通过任务 ID 引用单个调试会话，解除关联后仍保留历史标识。
@dataclass(slots=True, frozen=True)
class DebugLogicalPartition:
    # 分区身份用于规则及历史引用，独立于实际调试任务编号。
    id: UUID
    group_id: UUID
    name: str
    # 分区映射到平台 DEBUG 任务，不能用进程 PID 代替持久任务身份。
    session_task_id: UUID
    joined_at: datetime
    # 解除关联留下分区记录；原会话的进程生命周期由调试服务负责。
    associated: bool = True
    removed_at: datetime | None = None


# 规则用断点编号绑定源分区与目标分区，删除时间保留规则历史。
@dataclass(slots=True, frozen=True)
class DebugCoordinationRule:
    # 规则通过分区身份绑定两个成员，源断点编号只在来源会话中解释。
    id: UUID
    group_id: UUID
    source_partition_id: UUID
    # 同一断点编号只在来源会话中有意义，规则须同时引用来源分区。
    source_breakpoint_number: str
    target_partition_id: UUID
    action: DebugCoordinationRuleAction
    created_at: datetime
    # 软删除时间让历史仍可识别原规则，同时阻止它成为新匹配。
    deleted_at: datetime | None = None


# 把规则与双方分区打包，方便事件处理在同一视图中读取归属。
@dataclass(slots=True, frozen=True)
class DebugCoordinationRuleBinding:
    # 绑定是查找时解析出的成员快照，执行前仍需检查当前关联和目标状态。
    rule: DebugCoordinationRule
    source: DebugLogicalPartition
    target: DebugLogicalPartition


# 协调事件同时记录源事件及规则 ID，可追踪哪次命中触发了哪个动作。
# 命令是否发送与目标是否确认暂停独立记录，诊断时不能混用。
@dataclass(slots=True, frozen=True)
class DebugCoordinationEventRecord:
    # 事件自身 ID 和来源事件 ID 区分持久记录与触发通知；同一触发可匹配多条规则。
    id: UUID
    source_event_id: UUID
    group_id: UUID
    rule_id: UUID
    # 两端保存分区与任务身份，便于组变更后仍能追踪命令归属。
    source_partition_id: UUID
    source_session_task_id: UUID
    target_partition_id: UUID
    target_session_task_id: UUID
    breakpoint_number: str
    # 发生时间记录源端命中，处理时间用于定位规则执行和确认延迟。
    # 发生时间来自会话通知；处理时间由协调服务在写入处理结果时更新。
    occurred_at: datetime
    processed_at: datetime
    outcome: DebugCoordinationEventOutcome
    # 目标已暂停不必意味着本规则发送过命令；两个布尔量独立保留。
    command_sent: bool
    target_paused: bool
    message: str
