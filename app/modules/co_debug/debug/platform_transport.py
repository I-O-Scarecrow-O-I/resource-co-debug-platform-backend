from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.modules.co_debug.debug.transport import (
    GdbTransportClosed,
    GdbTransportMessage,
)
from app.platform.services.task_execution import (
    InteractiveProcessSession,
    ManagedTaskContext,
)


@dataclass(slots=True, frozen=True)
class _TransportClosedMarker:
    returncode: int | None = None
    error: BaseException | None = None


_QueueItem = (
    GdbTransportMessage
    | _TransportClosedMarker
)


class PlatformGdbTransport:
    """
    B模块对 A 模块 InteractiveProcessSession 的适配器。

    A 提供的是：

        callback式 stdout/stderr
        write()
        wait()
        terminate()

    B 的 GdbMiSession 希望使用的是：

        send()
        receive()
        wait()
        close()

    因此这里使用 asyncio.Queue
    把 callback 模式转换为 receive 模式。

    这一文件是 B7 与 A 平台之间唯一需要直接了解
    InteractiveProcessSession 的地方。
    """

    def __init__(
        self,
        session: InteractiveProcessSession,
        output_queue: asyncio.Queue[_QueueItem],
    ) -> None:
        self._session = session
        self._output_queue = output_queue

        self._completion_task = (
            asyncio.create_task(
                self._monitor_completion()
            )
        )

    @classmethod
    async def open(
        cls,
        *,
        context: ManagedTaskContext,
        command: list[str],
        workspace,
        work_dir: str = ".",
    ) -> "PlatformGdbTransport":
        """
        在 A 管理的 Managed Task Workspace 中
        启动一个交互式进程。

        对 B7 来说 command 通常是：

            [
                "gdb",
                "--interpreter=mi2",
                executable,
            ]
        """

        output_queue: asyncio.Queue[
            _QueueItem
        ] = asyncio.Queue()

        def on_output(
            message: str,
            stream: str,
        ) -> None:
            """
            这里刻意只负责入队。

            不在 A 的输出 callback 中：
                - 解析 MI
                - 修改 Debug 状态
                - 执行业务逻辑

            因为 callback 抛异常会导致 A 将
            interactive process 判定为失败。
            """

            output_queue.put_nowait(
                GdbTransportMessage(
                    stream=stream,
                    text=message,
                )
            )

        session = (
            await context.open_interactive_process(
                command,
                workspace,
                work_dir=work_dir,
                on_output=on_output,
            )
        )

        return cls(
            session=session,
            output_queue=output_queue,
        )

    @property
    def returncode(
        self,
    ) -> int | None:
        return (
            self._session.returncode
        )

    async def send(
        self,
        command: str,
    ) -> None:
        if not isinstance(
            command,
            str,
        ):
            raise TypeError(
                "GDB command must be a string"
            )

        if not command:
            raise ValueError(
                "GDB command must not be empty"
            )

        if (
            self._session.returncode
            is not None
        ):
            raise GdbTransportClosed(
                self._session.returncode
            )

        # GDB/MI 是按行读取命令。
        # 调用方无需关心末尾换行。
        wire_command = (
            command
            if command.endswith("\n")
            else command + "\n"
        )

        try:
            await self._session.write(
                wire_command
            )

        except RuntimeError as exc:
            if (
                self._session.returncode
                is not None
            ):
                raise GdbTransportClosed(
                    self._session.returncode
                ) from exc

            raise

    async def receive(
        self,
    ) -> GdbTransportMessage:
        item = await (
            self._output_queue.get()
        )

        if isinstance(
            item,
            GdbTransportMessage,
        ):
            return item

        if item.error is not None:
            raise item.error

        raise GdbTransportClosed(
            item.returncode
        )

    async def wait(
        self,
    ) -> int:
        return (
            await self._session.wait()
        )

    async def close(
        self,
    ) -> None:
        """
        terminate 在 A 中是幂等的，
        因此重复调用是安全的。
        """

        await self._session.terminate()

        await asyncio.gather(
            self._completion_task,
            return_exceptions=True,
        )

    async def _monitor_completion(
        self,
    ) -> None:
        """
        防止 GDB 意外退出后 receive() 永久阻塞。

        当底层进程结束时向 Queue 放置一个终止标记。
        """

        try:
            returncode = (
                await self._session.wait()
            )

        except BaseException as exc:
            await self._output_queue.put(
                _TransportClosedMarker(
                    error=exc,
                )
            )

            return

        await self._output_queue.put(
            _TransportClosedMarker(
                returncode=returncode,
            )
        )