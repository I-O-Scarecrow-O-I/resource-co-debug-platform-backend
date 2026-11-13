from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MiRecordKind(StrEnum):
    """
    GDB/MI 输出记录类型。
    """

    # 结果类通常带命令 token，负责归还一次请求的应答。
    # 异步类可在任何命令等待期间到达。
    RESULT = "result"

    EXEC_ASYNC = "exec-async"
    STATUS_ASYNC = "status-async"
    NOTIFY_ASYNC = "notify-async"

    # 三类流记录区分 GDB 控制台、目标程序输出和调试器日志，展示时不应合并为结果负载。
    CONSOLE_STREAM = "console-stream"
    TARGET_STREAM = "target-stream"
    LOG_STREAM = "log-stream"

    # 提示符可作为首次握手信号；未知记录仍保留原始内容以便诊断版本差异。
    PROMPT = "prompt"
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class MiCommand:
    """
    一条发送给 GDB/MI 的命令。

    token 用来关联：

        1-exec-run
        ↓
        1^running
    """

    # token 同时存在于发送文本和结构字段；读取端根据结构字段关联等待中的 Future。
    token: int
    text: str


@dataclass(slots=True, frozen=True)
class MiRecord:
    """
    一条解析后的 GDB/MI 输出。
    """

    # 保留原始行用于诊断；业务状态机使用分类、消息类和解析后的负载。
    kind: MiRecordKind
    raw: str

    # 流文本、提示符和通知没有必然可用的命令 token；缺失时不要用上一命令的编号补齐。
    token: int | None = None
    message_class: str | None = None

    payload: dict[str, Any] | str | None = None

    # 同名消息类只在结果记录中算作命令完成，异步通知不能代替成功应答。
    @property
    def is_done(self) -> bool:
        return (
            self.kind == MiRecordKind.RESULT
            and self.message_class == "done"
        )

    @property
    def is_error(self) -> bool:
        return (
            self.kind == MiRecordKind.RESULT
            and self.message_class == "error"
        )

    @property
    # 命令接受与目标开始运行可能分别以结果或执行异步记录出现，二者都可推进运行态。
    def is_running(self) -> bool:
        return (
            self.message_class == "running"
            and self.kind
            in {
                MiRecordKind.RESULT,
                MiRecordKind.EXEC_ASYNC,
            }
        )

    @property
    # 暂停由执行异步记录确认；仅收到某条控制命令成功不证明目标已经停住。
    def is_stopped(self) -> bool:
        return (
            self.kind == MiRecordKind.EXEC_ASYNC
            and self.message_class == "stopped"
        )


class DebugSessionState(StrEnum):
    """
    单个 GDB 调试会话的状态。
    """

    # 会话状态从调试器初始化开始，与平台任务的排队和运行状态是不同坐标。
    STARTING = "STARTING"

    # 就绪只确认 GDB 握手完成；目标程序尚未运行。
    READY = "READY"

    RUNNING = "RUNNING"

    # 暂停可对应断点、步进完成或信号等原因；上层需结合停止字段解读。
    STOPPED = "STOPPED"

    # 退出与故障都结束停止等待，但失败需要保留诊断，不能按正常退出处理。
    EXITED = "EXITED"

    FAILED = "FAILED"


@dataclass(slots=True)
class DebugBreakpoint:
    """
    B层维护的断点。
    """

    # 断点编号来自 GDB，只在对应会话内有意义；界面位置不参与编号唯一性。
    number: str

    location: str

    # 禁用不删除断点记录；界面仍可按原编号恢复启用。
    enabled: bool = True

    # 符号断点不一定能定位到源文件；短路径、完整路径和行号都允许缺失。
    file: str | None = None

    fullname: str | None = None

    line: int | None = None

    function: str | None = None


@dataclass(slots=True)
class DebugThread:
    """A thread reported by GDB for the current inferior."""

    # 协议线程 ID 用于 GDB 命令，Linux TID 是独立且可缺失的系统标识。
    thread_id: str
    linux_tid: int | None = None
    name: str | None = None
    state: str = "unknown"
    core: int | None = None


@dataclass(slots=True)
class DebugSessionStateModel:
    """
    单个 DebugSession 的当前业务状态。

    B8 以后会同时维护多个这样的对象。
    """

    # 每个实例从独立的启动状态和空集合开始，不沿用其他调试任务的断点或线程现场。
    state: DebugSessionState = DebugSessionState.STARTING

    # 每个会话持有独立断点表，断点号不能用于索引其他会话。
    breakpoints: dict[str, DebugBreakpoint] = field(
        default_factory=dict
    )

    # 暂停原因和源码位置由最近收到的状态记录提供；缺少符号或现场时允许为 None。
    stop_reason: str | None = None

    current_file: str | None = None

    current_line: int | None = None

    current_function: str | None = None

    # 线程表以 GDB ID 为键，另记当前选中、实际暂停和断点命中的线程。
    # 例如用户切换查看线程后，选中线程与原断点命中线程可以不同。
    threads: dict[str, DebugThread] = field(
        default_factory=dict
    )

    current_thread_id: str | None = None

    stopped_thread_id: str | None = None

    # 记录断点实际命中的线程，避免用户随后切换线程时丢失触发来源。
    breakpoint_hit_thread_id: str | None = None
