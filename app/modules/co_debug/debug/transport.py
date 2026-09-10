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

    GDB/MI 协议数据主要来自 stdout。
    stderr 保留下来用于错误诊断和日志。
    """

    stream: str

    text: str


class GdbTransport(Protocol):
    """
    B7 与底层进程系统之间的唯一边界。

    GdbMiSession 不应该知道底层究竟是：

        asyncio subprocess

    还是：

        A模块 InteractiveProcessSession

    甚至测试中的：

        FakeGdbTransport

    只依赖下面三个操作即可。
    """

    async def send(
        self,
        command: str,
    ) -> None:
        """
        向正在运行的 GDB stdin 写入一条命令。

        command 不要求调用方自己附加换行符，
        Transport 实现负责处理具体发送格式。
        """

        ...

    async def receive(
        self,
    ) -> GdbTransportMessage:
        """
        等待并返回 GDB 的下一条 stdout/stderr 输出。
        """

        ...

    async def close(
        self,
    ) -> None:
        """
        关闭当前 GDB Transport。

        最终由具体适配器决定是：

            -gdb-exit

        还是：

            Platform terminate
        """

        ...