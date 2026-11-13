from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.platform.domain.enums import TaskStatus


# 候选可执行路径是构建产物中的相对路径，选取时需携带所属构建任务。
class DebugExecutableCandidate(BaseModel):
    # 展示名取文件名，目录层级保留在路径字段。
    # 不同子目录的同名程序应按路径选择。
    name: str
    executable_path: str


# 构建候选汇集一次构建的可用可执行文件，不能用文件名跨构建关联会话。
class DebugBuildCandidate(BaseModel):
    # 候选关联原工程和构建任务，后续会话用这些标识核验来源。
    project_id: UUID
    build_task_id: UUID
    # 来源类型区分普通构建与补偿构建；不会改变候选程序的调试协议。
    build_kind: str
    created_at: datetime
    # 候选列表来自当前可下载构建快照；展示出来不表示调试进程已启动。
    executables: list[DebugExecutableCandidate] = Field(
        default_factory=list
    )


# 描述响应列出协议支持的操作；是否已连接调试器另查状态接口。
class DebugSessionResponse(BaseModel):
    # 这里描述任务所用协议及命令能力，不等同于活跃性检查。
    task_id: UUID
    protocol: str
    supported_commands: list[str]
    note: str


# 平台任务状态和 GDB 目标状态是两个时间轴。
# 例如平台仍在 RUNNING 时，目标程序可能已经暂停。
class DebugSessionStateResponse(BaseModel):
    # 平台任务状态与 GDB 现场状态分开返回。
    # 例如任务仍在运行时，被调试程序可以处于断点停止态。
    task_id: UUID
    task_status: TaskStatus
    # active 表示当前可找到实时命令通道；它不能代替检查目标执行态。
    active: bool

    state: str

    # 停止原因和源位置可能为空；终态或无调试符号时不能推断缺省行号。
    stop_reason: str | None = None
    current_file: str | None = None
    current_line: int | None = None
    current_function: str | None = None
    # 选中线程用于当前查看，停止和断点标记保留原暂停事件归属。
    # 当前选中线程用于表达式和栈检查；停止或命中线程反映最近事件的来源。
    current_thread_id: str | None = None
    stopped_thread_id: str | None = None
    breakpoint_hit_thread_id: str | None = None

    # 断点表是会话快照；编号只在这个 GDB 会话内可用于控制。
    breakpoints: list[
        dict[str, Any]
    ] = Field(
        default_factory=list
    )

    # 协议控制台、目标程序输出、MI 日志和进程标准错误分别保留。
    # 调用方可据来源选择展示位置，而非混成程序输出。
    console_output: list[str] = Field(
        default_factory=list
    )

    # 目标流保留被调试程序输出；它不是命令成功结果。
    target_output: list[str] = Field(
        default_factory=list
    )

    # GDB 的协议日志流与进程标准错误区分，便于识别诊断来源。
    log_output: list[str] = Field(
        default_factory=list
    )

    stderr_output: list[str] = Field(
        default_factory=list
    )


# 列表元素各占一个被调试程序的参数位置，空列表可表达无启动参数。
class DebugArgumentsRequest(BaseModel):
    arguments: list[str] = Field(
        default_factory=list
    )


# 断点可用符号或源位置描述，条件与初始启用状态独立于位置。
class DebugBreakpointRequest(BaseModel):
    location: str = Field(
        min_length=1
    )

    # 临时断点、初始禁用和条件分别交给 GDB，不因为其中一项隐式设置其他项。
    temporary: bool = False
    disabled: bool = False
    condition: str | None = None


# 表达式由当前检查上下文求值，服务不在 Python 中解释其语法。
class DebugExpressionRequest(BaseModel):
    expression: str = Field(
        min_length=1
    )


# 选择字段接收 GDB 的数字线程 ID；系统 TID 不作为 API 入参。
class DebugThreadSelectRequest(BaseModel):
    # 选择使用协议线程编号文本，不接受系统线程号或列表下标作为替代。
    thread_id: str = Field(min_length=1, pattern=r"^\d+$")


# 超时约束此次等待，不改变正在运行的目标；无值表示不在该入口设停止期限。
class DebugWaitForStopRequest(BaseModel):
    timeout_seconds: (
        float
        | None
    ) = Field(
        default=None,
        gt=0,
    )


# 返回的实际断点号是后续修改和删除的凭证，位置字段主要用于展示。
class DebugBreakpointResponse(BaseModel):
    number: str
    location: str
    enabled: bool

    # 调试符号不完整时，源路径、行号或函数名可能缺失。
    # 完整路径、简短文件名、行号和函数可独立缺失，不要求 GDB 每次都返回符号。
    file: str | None = None
    fullname: str | None = None
    line: int | None = None
    function: str | None = None


# 表达式响应保留 GDB 字符串表示，不推断 Python 数字或布尔类型。
class DebugExpressionResponse(BaseModel):
    # GDB 结果文本原样承载，空值表示没有可用文本而不是固定的空字符串。
    value: str | None = None


# 栈帧和局部变量保留协议列表的形状，调用方须容忍可选字段。
class DebugStackFramesResponse(BaseModel):
    frames: list[Any] = Field(
        default_factory=list
    )


# 局部变量是当前选中线程和帧的快照，不是整个目标程序的变量索引。
class DebugLocalVariablesResponse(BaseModel):
    variables: list[Any] = Field(default_factory=list)


# 系统线程号与核心编号由目标环境提供；缺失时不生成默认值。
# 会话线程操作仍以 thread_id 为准。
class DebugThreadResponse(BaseModel):
    # 线程 ID 用于调试命令；Linux TID 是操作系统诊断值，其他平台可缺失。
    thread_id: str
    linux_tid: int | None = None
    name: str | None = None
    state: str
    # 核心号来自 GDB 观测，不是平台调度计划的分配证明。
    core: int | None = None
    # 当前选中与命中断点独立设置；用户切换线程后可以不同。
    current: bool = False
    breakpoint_hit: bool = False


# 线程数组与三个线程引用来自同一份快照；引用可为空。
class DebugThreadsResponse(BaseModel):
    threads: list[DebugThreadResponse] = Field(default_factory=list)
    # 保留三种焦点标记，前端无需由列表首项猜测当前检查现场。
    current_thread_id: str | None = None
    stopped_thread_id: str | None = None
    breakpoint_hit_thread_id: str | None = None
