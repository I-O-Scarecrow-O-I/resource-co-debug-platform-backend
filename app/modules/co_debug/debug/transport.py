from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True, frozen=True)
class GdbTransportMessage:
    """
    GDB Transport 返回给 B 的一条原始输出。

    stream:
        stdout
        stderr

    GDB/MI 协议记录主要位于 stdout。
    stderr 保留用于异常诊断。
    """

    # 消息只保留一条记录的正文和来源；协议 token、状态或时间不由传输填充。
    stream: str
    text: str


class GdbTransportClosed(RuntimeError):
    """
    Transport 已经关闭或底层 GDB 进程已经退出。
    """

    # 无退出码时无法从底层确认退出原因，上层还需参考是否主动关闭。
    def __init__(
        self,
        returncode: int | None = None,
    ) -> None:
        # 非空退出码是底层观察事实，不等于某条 MI 命令的结果码。
        self.returncode = returncode

        super().__init__(
            "GDB transport is closed"
            if returncode is None
            else (
                "GDB transport is closed "
                f"with return code {returncode}"
            )
        )


# 传输可以由平台进程适配器或测试替身实现；会话仅依赖下面的异步契约。
class GdbTransport(Protocol):
    """
    B7 与底层进程系统之间唯一的进程边界。

    GdbMiSession 不直接依赖：

        ManagedTaskContext
        InteractiveProcessSession
        ProcessRunner
        asyncio.subprocess.Process

    它只依赖这个协议。
    """

    # 无退出码不代表 MI 握手已完成；协议就绪由读循环确认。
    @property
    def returncode(
        self,
    ) -> int | None:
        ...

    # 发送完成表示传输层已处理写入，不表示 GDB 已返回对应 token 的结果。
    async def send(
        self,
        command: str,
    ) -> None:
        """
        发送一条完整的 GDB/MI 命令。

        调用方不需要自行附加换行符。
        """

        ...

    # 接收以一条消息为边界，来源字段决定是否交给 MI 解析器。
    async def receive(
        self,
    ) -> GdbTransportMessage:
        """
        等待下一条 GDB stdout/stderr 输出。
        """

        ...

    # 等待的是 GDB 进程退出；被调试程序结束时调试器仍可能保持运行。
    async def wait(
        self,
    ) -> int:
        """
        等待底层 GDB 进程退出。
        """

        ...

    # 传输关闭用于资源回收；正常调试器退出命令仍由上层状态机编排。
    async def close(
        self,
    ) -> None:
        """
        强制关闭底层 Transport。

        GDB 协议级的 -gdb-exit 由 GdbMiSession 负责，
        Transport 本身不理解 GDB/MI。
        """

        ...