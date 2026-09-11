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

    stream: str
    text: str


class GdbTransportClosed(RuntimeError):
    """
    Transport 已经关闭或底层 GDB 进程已经退出。
    """

    def __init__(
        self,
        returncode: int | None = None,
    ) -> None:
        self.returncode = returncode

        super().__init__(
            "GDB transport is closed"
            if returncode is None
            else (
                "GDB transport is closed "
                f"with return code {returncode}"
            )
        )


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

    @property
    def returncode(
        self,
    ) -> int | None:
        ...

    async def send(
        self,
        command: str,
    ) -> None:
        """
        发送一条完整的 GDB/MI 命令。

        调用方不需要自行附加换行符。
        """

        ...

    async def receive(
        self,
    ) -> GdbTransportMessage:
        """
        等待下一条 GDB stdout/stderr 输出。
        """

        ...

    async def wait(
        self,
    ) -> int:
        """
        等待底层 GDB 进程退出。
        """

        ...

    async def close(
        self,
    ) -> None:
        """
        强制关闭底层 Transport。

        GDB 协议级的 -gdb-exit 由 GdbMiSession 负责，
        Transport 本身不理解 GDB/MI。
        """

        ...