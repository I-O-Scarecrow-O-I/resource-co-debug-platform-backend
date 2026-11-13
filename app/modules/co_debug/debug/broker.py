from __future__ import annotations

import asyncio
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DebugCommandKind(StrEnum):
    SET_ARGUMENTS = "set_arguments"

    # 断点操作与执行控制分开编码，消费者可为不同操作选择不同状态检查。
    INSERT_BREAKPOINT = "insert_breakpoint"
    DELETE_BREAKPOINT = "delete_breakpoint"
    ENABLE_BREAKPOINT = "enable_breakpoint"
    DISABLE_BREAKPOINT = "disable_breakpoint"

    RUN = "run"
    CONTINUE = "continue"
    NEXT = "next"
    STEP = "step"
    INTERRUPT = "interrupt"

    # 检查命令都经过同一请求通道；读取调试现场也必须在拥有会话的事件循环中执行。
    EVALUATE = "evaluate"
    STACK_FRAMES = "stack_frames"
    LOCAL_VARIABLES = "local_variables"
    THREADS = "threads"
    SELECT_THREAD = "select_thread"

    STATE = "state"
    WAIT_FOR_STOP = "wait_for_stop"

    CLOSE = "close"


class DebugCommandBrokerError(RuntimeError):
    pass


# 通道关闭和某次 MI 命令失败可分别处理；关闭后不能继续在此队列提交。
class DebugCommandBrokerClosed(
    DebugCommandBrokerError
):
    pass


class DebugCommandTimeout(
    DebugCommandBrokerError
):
    pass


@dataclass(slots=True)
# 请求跨线程传递；结果使用 concurrent Future，避免被某一方的事件循环绑定。
class DebugCommandRequest:
    # 每个请求拥有独立的跨线程 Future，正常结果和异常走同一响应通道。
    operation: DebugCommandKind
    arguments: dict[str, Any]
    response: Future[Any]


@dataclass(
    slots=True,
    frozen=True,
)
# 关闭标记不是用户命令，只负责唤醒正在等队列的消费者。
# 关闭原因随队列标记交给已经等待的消费者；它不是一条需要执行的命令。
class _BrokerClosedMarker:
    error: BaseException | None = None


_BrokerItem = (
    DebugCommandRequest
    | _BrokerClosedMarker
)


class DebugCommandBroker:
    """
    FastAPI EventLoop 与 Managed Task EventLoop
    之间的线程安全命令通道。

    API侧：
        await broker.request(...)

    Managed Task侧：
        request = await broker.receive()
        broker.resolve(...)
        broker.reject(...)

    Broker本身不理解GDB。
    """

    # 同步队列连接 API 循环和独立调试循环，关闭与入队通过同一把锁协调。
    def __init__(
        self,
    ) -> None:
        # 同步队列与响应对象不绑定某个 asyncio 循环；提交和消费可在不同线程。
        # 使用线程安全队列承接两个事件循环的所有权交接。
        # 队列中的 Future 是返回通道，不携带对 GDB transport 的直接访问。
        self._queue: queue.Queue[
            _BrokerItem
        ] = queue.Queue()

        self._lock = (
            threading.RLock()
        )

        self._closed = False

        # 保留首次关闭原因，令关闭前后观察到的诊断尽可能一致。
        self._close_error: (
            BaseException
            | None
        ) = None

    @property
    def closed(
        self,
    ) -> bool:
        with self._lock:
            return self._closed

    async def request(
        self,
        operation: DebugCommandKind,
        *,
        timeout_seconds: float | None = None,
        **arguments: Any,
    ) -> Any:
        """
        从任意 asyncio EventLoop 提交命令。

        response使用 concurrent.futures.Future，
        因此可以安全地跨线程完成。
        """

        # 有界等待必须有正的预算；缺省预算则由调用方承担等待责任。
        # 非法等待预算在入队前报错，防止调用失败后仍发生调试副作用。
        if (
            timeout_seconds
            is not None
            and timeout_seconds <= 0
        ):
            raise ValueError(
                "timeout_seconds "
                "must be greater than 0"
            )

        response: Future[Any] = (
            Future()
        )

        # 复制命令参数，入队后不依赖提交方保留原字典。
        # 命令包装保存独立的参数映射；它并不递归复制参数中的可变对象。
        command = (
            DebugCommandRequest(
                operation=operation,
                arguments=dict(
                    arguments
                ),
                response=response,
            )
        )

        # 关闭检查与入队不可分开，否则请求可能落在关闭标记之后而永远无人处理。
        # 关闭检查与入队共用同一把锁，防止把请求排到终止标记之后。
        with self._lock:
            if self._closed:
                raise self._closed_error()

            self._queue.put(
                command
            )

        # 在提交方循环上适配 Future，实际命令仍由调试循环执行。
        # 请求端在自己的循环包装跨线程响应对象；消费端不需要持有请求循环。
        # 转换后的等待器属于提交方循环，底层 Future 仍可由任务侧线程完成。
        wrapped = (
            asyncio.wrap_future(
                response
            )
        )

        # 无显式预算时等待由命令消费者或 broker 关闭结束；不在通道层虚构一个截止时刻。
        try:
            if timeout_seconds is None:
                return await wrapped

            return await asyncio.wait_for(
                wrapped,
                timeout=timeout_seconds,
            )

        # 超时取消等待者；队列尚未派发时可跳过，已经执行的命令不会由此撤回。
        # 期限只约束本次结果等待。
        # 已被消费者领取并执行的动作不会因 HTTP 超时自动撤销。
        except TimeoutError as exc:
            response.cancel()

            # 超时只意味着提交者没有及时收到结果，不能反推已出队的 GDB 动作未执行。
            raise DebugCommandTimeout(
                "debug command timed out: "
                f"{operation.value}"
            ) from exc

    async def receive(
        self,
    ) -> DebugCommandRequest:
        """
        由 Managed Task EventLoop 调用。

        queue.Queue 是线程安全的；
        每次最多阻塞0.1秒，避免 asyncio task
        被取消后留下长期阻塞的工作线程。
        """

        # 一次线程等待有短上限，让停止会话时不会留下长期阻塞的取队列调用。
        while True:
            try:
                item = await asyncio.to_thread(
                    self._queue.get,
                    True,
                    0.1,
                )

            # 空队列结合关闭状态判断终止；普通短超时只是再次观察。
            # 轮询到空队列时再次检查关闭状态，避免终止标记已被取走后仍继续等待。
            # 短轮询返回后再次观察关闭状态，以便结束没有取到关闭标记的等待者。
            except queue.Empty:
                with self._lock:
                    if (
                        self._closed
                        and self._queue.empty()
                    ):
                        raise self._closed_error() from None

                continue

            # 显式关闭标记优先结束调试分发循环，不进入 GDB 操作分派。
            # 终止标记不具有操作和参数，不能交给业务命令分派器。
            # 关闭标记优先于普通响应处理；消费者据此退出，而不是将其分派给会话。
            if isinstance(
                item,
                _BrokerClosedMarker,
            ):
                raise self._closed_error(
                    item.error
                )

            # HTTP请求可能已经超时或取消。
            # 这种命令不再交给GDB执行。
            # 只对仍在队列里的请求有效。
            # 出队后若响应端再取消，调用方仍需考虑命令可能改变现场。
            if item.response.cancelled():
                continue

            return item

    # 客户端可能已经放弃等待，只尝试完成仍未结束的请求。
    def resolve(
        self,
        request: DebugCommandRequest,
        result: Any = None,
    ) -> None:
        # 请求可能已取消或完成，此时不再写入结果。
        # 回应前判断等待者是否还在接收；已完成的 Future 不会被有意重复写入。
        if (
            not request.response.done()
        ):
            request.response.set_result(
                result
            )

    # 将执行错误交给原请求者；失败并不必然关闭整个会话。
    def reject(
        self,
        request: DebugCommandRequest,
        error: BaseException,
    ) -> None:
        # 业务错误只返回给对应请求；关闭路径另向未领取的请求提供统一失败。
        if (
            not request.response.done()
        ):
            request.response.set_exception(
                error
            )

    def close(
        self,
        error: BaseException | None = None,
    ) -> None:
        """
        关闭命令通道。

        尚未被Managed Task取出的请求全部失败，
        然后放入终止标记唤醒receive()。
        """

        # 关闭是一次状态迁移，此后不再接受新的请求。
        # 关闭先禁止新的提交，然后清空尚未领取的命令。
        # 已经交给消费者的请求由其自身完成路径处理。
        with self._lock:
            if self._closed:
                return

            # 先封入口再排空，避免提交端在最后一个待处理项之后插入新请求。
            self._closed = True
            self._close_error = error

            # 排空只取得仍归队列所有的请求；已经出队的操作由会话任务自行收尾。
            pending: list[
                DebugCommandRequest
            ] = []

            # 只排出尚未分派的请求；已经被分派的命令由其执行者处理。
            while True:
                try:
                    item = (
                        self._queue
                        .get_nowait()
                    )

                except queue.Empty:
                    break

                if isinstance(
                    item,
                    DebugCommandRequest,
                ):
                    pending.append(
                        item
                    )

            # 等待者收到可识别的通道关闭错误，原始故障可作为诊断来源。
            close_error = (
                self._closed_error()
            )

            # 主动结束积压请求，客户端无需等自己的超时才发现会话已不可用。
            for request in pending:
                if (
                    not request
                    .response.done()
                ):
                    request.response.set_exception(
                        close_error
                    )

            # 最后放入唤醒标记，避免空队列上的消费方继续轮询。
            # 完成积压清理后唤醒接收方，让消费循环识别生命周期已结束。
            # 关闭标记即使在无待处理请求时也要入队，以唤醒正在等待队列的消费者。
            self._queue.put(
                _BrokerClosedMarker(
                    error=error
                )
            )

    def _closed_error(
        self,
        error: BaseException | None = None,
    ) -> DebugCommandBrokerClosed:
        source = (
            error
            if error is not None
            else self._close_error
        )

        # 无上游异常时使用统一关闭说明；存在原因时保留诊断给调用方。
        if source is None:
            return DebugCommandBrokerClosed(
                "debug command broker is closed"
            )

        return DebugCommandBrokerClosed(
            "debug command broker is closed: "
            f"{source}"
        )
