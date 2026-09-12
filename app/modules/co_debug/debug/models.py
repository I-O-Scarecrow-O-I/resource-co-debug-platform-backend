from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MiRecordKind(str, Enum):
    """
    GDB/MI 输出记录类型。
    """

    RESULT = "result"

    EXEC_ASYNC = "exec-async"
    STATUS_ASYNC = "status-async"
    NOTIFY_ASYNC = "notify-async"

    CONSOLE_STREAM = "console-stream"
    TARGET_STREAM = "target-stream"
    LOG_STREAM = "log-stream"

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

    token: int
    text: str


@dataclass(slots=True, frozen=True)
class MiRecord:
    """
    一条解析后的 GDB/MI 输出。
    """

    kind: MiRecordKind
    raw: str

    token: int | None = None
    message_class: str | None = None

    payload: dict[str, Any] | str | None = None

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
    def is_stopped(self) -> bool:
        return (
            self.kind == MiRecordKind.EXEC_ASYNC
            and self.message_class == "stopped"
        )


class DebugSessionState(str, Enum):
    """
    单个 GDB 调试会话的状态。
    """

    STARTING = "STARTING"

    READY = "READY"

    RUNNING = "RUNNING"

    STOPPED = "STOPPED"

    EXITED = "EXITED"

    FAILED = "FAILED"


@dataclass(slots=True)
class DebugBreakpoint:
    """
    B层维护的断点。
    """

    number: str

    location: str

    enabled: bool = True

    file: str | None = None

    fullname: str | None = None

    line: int | None = None

    function: str | None = None


@dataclass(slots=True)
class DebugSessionStateModel:
    """
    单个 DebugSession 的当前业务状态。

    B8 以后会同时维护多个这样的对象。
    """

    state: DebugSessionState = DebugSessionState.STARTING

    breakpoints: dict[str, DebugBreakpoint] = field(
        default_factory=dict
    )

    stop_reason: str | None = None

    current_file: str | None = None

    current_line: int | None = None

    current_function: str | None = None