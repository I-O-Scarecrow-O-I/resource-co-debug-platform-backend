from __future__ import annotations

import asyncio
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from typing import Any


class DebugCommandKind(str, Enum):
    SET_ARGUMENTS = "set_arguments"

    INSERT_BREAKPOINT = "insert_breakpoint"
    DELETE_BREAKPOINT = "delete_breakpoint"
    ENABLE_BREAKPOINT = "enable_breakpoint"
    DISABLE_BREAKPOINT = "disable_breakpoint"

    RUN = "run"
    CONTINUE = "continue"
    NEXT = "next"
    STEP = "step"
    INTERRUPT = "interrupt"

    EVALUATE = "evaluate"
    STACK_FRAMES = "stack_frames"

    STATE = "state"
    WAIT_FOR_STOP = "wait_for_stop"

    CLOSE = "close"


class DebugCommandBrokerError(RuntimeError):
    pass


class DebugCommandBrokerClosed(
    DebugCommandBrokerError
):
    pass


class DebugCommandTimeout(
    DebugCommandBrokerError
):
    pass


@dataclass(slots=True)
class DebugCommandRequest:
    operation: DebugCommandKind
    arguments: dict[str, Any]
    response: Future[Any]


@dataclass(
    slots=True,
    frozen=True,
)
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

    def __init__(
        self,
    ) -> None:
        self._queue: queue.Queue[
            _BrokerItem
        ] = queue.Queue()

        self._lock = (
            threading.RLock()
        )

        self._closed = False

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

        command = (
            DebugCommandRequest(
                operation=operation,
                arguments=dict(
                    arguments
                ),
                response=response,
            )
        )

        with self._lock:
            if self._closed:
                raise self._closed_error()

            self._queue.put(
                command
            )

        wrapped = (
            asyncio.wrap_future(
                response
            )
        )

        try:
            if timeout_seconds is None:
                return await wrapped

            return await asyncio.wait_for(
                wrapped,
                timeout=timeout_seconds,
            )

        except TimeoutError as exc:
            response.cancel()

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

        while True:
            try:
                item = await asyncio.to_thread(
                    self._queue.get,
                    True,
                    0.1,
                )

            except queue.Empty:
                with self._lock:
                    if (
                        self._closed
                        and self._queue.empty()
                    ):
                        raise (
                            self._closed_error()
                        )

                continue

            if isinstance(
                item,
                _BrokerClosedMarker,
            ):
                raise self._closed_error(
                    item.error
                )

            # HTTP请求可能已经超时或取消。
            # 这种命令不再交给GDB执行。
            if item.response.cancelled():
                continue

            return item

    def resolve(
        self,
        request: DebugCommandRequest,
        result: Any = None,
    ) -> None:
        if (
            not request.response.done()
        ):
            request.response.set_result(
                result
            )

    def reject(
        self,
        request: DebugCommandRequest,
        error: BaseException,
    ) -> None:
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

        with self._lock:
            if self._closed:
                return

            self._closed = True
            self._close_error = error

            pending: list[
                DebugCommandRequest
            ] = []

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

            close_error = (
                self._closed_error()
            )

            for request in pending:
                if (
                    not request
                    .response.done()
                ):
                    request.response.set_exception(
                        close_error
                    )

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

        if source is None:
            return DebugCommandBrokerClosed(
                "debug command broker is closed"
            )

        return DebugCommandBrokerClosed(
            "debug command broker is closed: "
            f"{source}"
        )