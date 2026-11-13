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
# 终止消息不是进程的一行文本；即使某个 stdout 文本与诊断相似也不会被认作关闭。
class _TransportClosedMarker:
    returncode: int | None = None
    error: BaseException | None = None


_QueueItem = (
    GdbTransportMessage
    | _TransportClosedMarker
)


# 适配平台交互进程为 GDB 所需的按行发送和接收接口。
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
        # 进程资源归平台会话管理。
        # 适配层只持有访问接口与输出队列，不使用进程 PID 重新定位或终止进程。
        self._session = session
        self._output_queue = output_queue

        # 完成监听和消息读取并行运行。
        # 即使当前没有读取方，进程结束也会留下随后可读的终止标记。
        self._completion_task = (
            asyncio.create_task(
                self._monitor_completion()
            )
        )

    @classmethod
    # 创建进程仍由平台上下文执行，传输不自行解析工作区权限。
    async def open(
        cls,
        *,
        context: ManagedTaskContext,
        command: list[str],
        workspace,
        work_dir: str = ".",
    ) -> PlatformGdbTransport:
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

        # 将进程输出转成当前调试循环内的记录流，等待者不直接操作底层管道。
        # 在平台创建进程前准备队列，避免漏掉启动时立即输出的提示符。
        # 消息连同来源保留，以便读循环区分协议输出和标准错误。
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

            # 保留进程回调交付的顺序；应用层协议解析留给会话的单一 reader。
            output_queue.put_nowait(
                GdbTransportMessage(
                    stream=stream,
                    text=message,
                )
            )

        # 平台完成资源登记后返回会话；适配器只持有会话接口和输出队列。
        session = (
            await context.open_interactive_process(
                command,
                workspace,
                work_dir=work_dir,
                on_output=on_output,
            )
        )

        # 将已有输出队列交给实例，避免启动过程中积累的提示和日志因重新建队列而丢失。
        return cls(
            session=session,
            output_queue=output_queue,
        )

    @property
    def returncode(
        self,
    ) -> int | None:
        # 退出码是底层观察值；读取到它并不表示队列中没有待消费的尾部消息。
        return (
            self._session.returncode
        )

    # 发送的是单条 MI 命令，传输保证行结束符并先检查进程是否已退出。
    async def send(
        self,
        command: str,
    ) -> None:
        # 拒绝非字符串输入使错误在编码和写入前暴露，不污染已经打开的协议管道。
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

        # 进程已退出时拒绝发送，避免调用方把写入错误误解为 GDB 拒绝了某条 MI 命令。
        if (
            self._session.returncode
            is not None
        ):
            raise GdbTransportClosed(
                self._session.returncode
            )

        # GDB/MI 是按行读取命令。
        # 调用方无需关心末尾换行。
        # 为命令补齐行结束符，使 GDB 可以在完整行到达后处理它。
        # 引号和参数语义仍由命令构造器处理。
        wire_command = (
            command
            if command.endswith("\n")
            else command + "\n"
        )

        try:
            await self._session.write(
                wire_command
            )

        # 写入失败后重新观察退出码，区分传输已关闭和其他运行错误。
        # 发送检查和写入之间仍可能发生退出。
        # 仅在有退出码时转换为传输关闭，其他底层运行错误按原异常传播。
        except RuntimeError as exc:
            if (
                self._session.returncode
                is not None
            ):
                raise GdbTransportClosed(
                    self._session.returncode
                ) from exc

            raise

    # 读操作既可返回普通输出也可收到关闭标记；关闭异常继续向会话传播。
    async def receive(
        self,
    ) -> GdbTransportMessage:
        # 接收端是单一协议读取任务。
        # 关闭标记被消费后不广播给其他 reader，不能把此接口当多订阅流。
        item = await (
            self._output_queue.get()
        )

        # 关闭标记与输出共用有序队列，先到的协议记录仍能被消费。
        # 真正的进程等待异常保留原错误，而不是折算为正常 EOF。
        if isinstance(
            item,
            GdbTransportMessage,
        ):
            return item

        # 进程完成时若记录了异常，使用相同异常向读取路径报告，避免只剩无原因的 EOF。
        if item.error is not None:
            raise item.error

        raise GdbTransportClosed(
            item.returncode
        )

    # 使用已有完成监视任务，不为每个观察者重复等待或关闭进程。
    async def wait(
        self,
    ) -> int:
        return (
            await self._session.wait()
        )

    # 关闭由平台会话终止底层进程，适配器等待自身的完成观察路径收尾。
    async def close(
        self,
    ) -> None:
        """
        terminate 在 A 中是幂等的，
        因此重复调用是安全的。
        """

        # 先让平台终止与收尾，再等待适配层监视任务，不在适配层另发系统信号。
        await self._session.terminate()

        # 等完成监听退出再返回，使终止标记的交付与传输关闭有明确的收束点。
        await asyncio.gather(
            self._completion_task,
            return_exceptions=True,
        )

    # 把平台进程的退出码或异常转换成输出队列里的终止信号，唤醒协议读者。
    async def _monitor_completion(
        self,
    ) -> None:
        """
        防止 GDB 意外退出后 receive() 永久阻塞。

        当底层进程结束时向 Queue 放置一个终止标记。
        """

        # 监视的是平台的完整进程等待；标准输出尾部已由平台读任务处理后才结束。
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

        # 正常结束携带实际退出码，即使它不是零。
        # 进程业务成功与传输是否正常交付关闭是两个判断。
        await self._output_queue.put(
            _TransportClosedMarker(
                returncode=returncode,
            )
        )