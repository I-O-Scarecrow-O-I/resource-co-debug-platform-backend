from __future__ import annotations

import threading
from typing import Any
from uuid import UUID

from app.modules.co_debug.debug.broker import (
    DebugCommandBroker,
    DebugCommandBrokerClosed,
    DebugCommandKind,
    DebugCommandRequest,
)
from app.modules.co_debug.debug.models import (
    DebugBreakpoint,
)
from app.modules.co_debug.debug.session import (
    GdbMiSession,
)


class DebugSessionNotFound(
    KeyError
):
    pass


class DebugSessionAlreadyExists(
    RuntimeError
):
    pass


class GdbMiCommandDispatcher:
    """
    运行在 Managed Task EventLoop 中。

    它是 Broker 与 GdbMiSession 之间的业务适配层。
    """

    async def run(
        self,
        *,
        broker: DebugCommandBroker,
        session: GdbMiSession,
    ) -> None:
        """
        持续消费 API 侧发来的调试命令。

        CLOSE命令处理后正常退出。
        Broker被关闭也正常退出。
        """

        while True:
            try:
                request = (
                    await broker.receive()
                )

            except DebugCommandBrokerClosed:
                return

            try:
                result, should_close = (
                    await self._dispatch(
                        session,
                        request,
                    )
                )

            except BaseException as exc:
                broker.reject(
                    request,
                    exc,
                )

                continue

            broker.resolve(
                request,
                result,
            )

            if should_close:
                return

    async def _dispatch(
        self,
        session: GdbMiSession,
        request: DebugCommandRequest,
    ) -> tuple[Any, bool]:
        operation = (
            request.operation
        )

        arguments = (
            request.arguments
        )

        if (
            operation
            == DebugCommandKind.SET_ARGUMENTS
        ):
            await session.set_arguments(
                list(
                    arguments.get(
                        "arguments",
                        [],
                    )
                )
            )

            return None, False

        if (
            operation
            == DebugCommandKind.INSERT_BREAKPOINT
        ):
            breakpoint = (
                await session
                .insert_breakpoint(
                    arguments["location"],
                    temporary=bool(
                        arguments.get(
                            "temporary",
                            False,
                        )
                    ),
                    disabled=bool(
                        arguments.get(
                            "disabled",
                            False,
                        )
                    ),
                    condition=arguments.get(
                        "condition"
                    ),
                )
            )

            return (
                self._breakpoint_snapshot(
                    breakpoint
                ),
                False,
            )

        if (
            operation
            == DebugCommandKind.DELETE_BREAKPOINT
        ):
            await session.delete_breakpoint(
                arguments[
                    "breakpoint_number"
                ]
            )

            return None, False

        if (
            operation
            == DebugCommandKind.ENABLE_BREAKPOINT
        ):
            await session.enable_breakpoint(
                arguments[
                    "breakpoint_number"
                ]
            )

            return None, False

        if (
            operation
            == DebugCommandKind.DISABLE_BREAKPOINT
        ):
            await session.disable_breakpoint(
                arguments[
                    "breakpoint_number"
                ]
            )

            return None, False

        if (
            operation
            == DebugCommandKind.RUN
        ):
            await session.run()

            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        if (
            operation
            == DebugCommandKind.CONTINUE
        ):
            await (
                session
                .continue_execution()
            )

            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        if (
            operation
            == DebugCommandKind.NEXT
        ):
            await session.next()

            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        if (
            operation
            == DebugCommandKind.STEP
        ):
            await session.step()

            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        if (
            operation
            == DebugCommandKind.INTERRUPT
        ):
            await session.interrupt()

            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        if (
            operation
            == DebugCommandKind.EVALUATE
        ):
            value = (
                await session
                .evaluate_expression(
                    arguments[
                        "expression"
                    ]
                )
            )

            return {
                "value": value,
            }, False

        if (
            operation
            == DebugCommandKind.STACK_FRAMES
        ):
            frames = (
                await session
                .list_stack_frames()
            )

            return {
                "frames": frames,
            }, False

        if (
            operation
            == DebugCommandKind.STATE
        ):
            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        if (
            operation
            == DebugCommandKind.WAIT_FOR_STOP
        ):
            await session.wait_for_stop(
                timeout_seconds=(
                    arguments.get(
                        "stop_timeout_seconds"
                    )
                )
            )

            return (
                self._state_snapshot(
                    session
                ),
                False,
            )

        if (
            operation
            == DebugCommandKind.CLOSE
        ):
            await session.close()

            return (
                self._state_snapshot(
                    session
                ),
                True,
            )

        raise ValueError(
            "unsupported debug command: "
            f"{operation}"
        )

    @staticmethod
    def _breakpoint_snapshot(
        breakpoint: DebugBreakpoint,
    ) -> dict[str, Any]:
        return {
            "number": breakpoint.number,
            "location": breakpoint.location,
            "enabled": breakpoint.enabled,
            "file": breakpoint.file,
            "fullname": breakpoint.fullname,
            "line": breakpoint.line,
            "function": breakpoint.function,
        }

    @classmethod
    def _state_snapshot(
        cls,
        session: GdbMiSession,
    ) -> dict[str, Any]:
        state = session.state

        return {
            "state": state.state.value,
            "stop_reason": (
                state.stop_reason
            ),
            "current_file": (
                state.current_file
            ),
            "current_line": (
                state.current_line
            ),
            "current_function": (
                state.current_function
            ),
            "breakpoints": [
                cls._breakpoint_snapshot(
                    breakpoint
                )
                for breakpoint
                in state.breakpoints.values()
            ],
            "console_output": list(
                session.console_output
            ),
            "target_output": list(
                session.target_output
            ),
            "log_output": list(
                session.log_output
            ),
            "stderr_output": list(
                session.stderr_output
            ),
        }


class DebugSessionManager:
    """
    B8 多调试会话注册表。

    每个 task_id 对应一个独立 Broker。

    Manager自身不保存GdbMiSession对象，
    因为GdbMiSession属于Managed Task EventLoop，
    不能暴露给FastAPI线程。
    """

    def __init__(
        self,
    ) -> None:
        self._lock = (
            threading.RLock()
        )

        self._brokers: dict[
            UUID,
            DebugCommandBroker,
        ] = {}

    def register(
        self,
        task_id: UUID,
        broker: DebugCommandBroker,
    ) -> None:
        with self._lock:
            if (
                task_id
                in self._brokers
            ):
                raise (
                    DebugSessionAlreadyExists(
                        "debug session "
                        "already exists: "
                        f"{task_id}"
                    )
                )

            self._brokers[
                task_id
            ] = broker

    def unregister(
        self,
        task_id: UUID,
        *,
        error: (
            BaseException
            | None
        ) = None,
    ) -> None:
        with self._lock:
            broker = (
                self._brokers.pop(
                    task_id,
                    None,
                )
            )

        if broker is not None:
            broker.close(
                error
            )

    def require(
        self,
        task_id: UUID,
    ) -> DebugCommandBroker:
        with self._lock:
            broker = (
                self._brokers.get(
                    task_id
                )
            )

        if broker is None:
            raise DebugSessionNotFound(
                "debug session "
                "does not exist: "
                f"{task_id}"
            )

        return broker

    def contains(
        self,
        task_id: UUID,
    ) -> bool:
        with self._lock:
            return (
                task_id
                in self._brokers
            )

    def list_session_ids(
        self,
    ) -> list[UUID]:
        with self._lock:
            return list(
                self._brokers.keys()
            )

    async def state(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.STATE,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def set_arguments(
        self,
        task_id: UUID,
        arguments: list[str],
        *,
        timeout_seconds: float = 10,
    ) -> None:
        await self._request(
            task_id,
            DebugCommandKind.SET_ARGUMENTS,
            timeout_seconds=(
                timeout_seconds
            ),
            arguments=arguments,
        )

    async def insert_breakpoint(
        self,
        task_id: UUID,
        location: str,
        *,
        temporary: bool = False,
        disabled: bool = False,
        condition: str | None = None,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.INSERT_BREAKPOINT,
            timeout_seconds=(
                timeout_seconds
            ),
            location=location,
            temporary=temporary,
            disabled=disabled,
            condition=condition,
        )

    async def delete_breakpoint(
        self,
        task_id: UUID,
        breakpoint_number: str,
        *,
        timeout_seconds: float = 10,
    ) -> None:
        await self._request(
            task_id,
            DebugCommandKind.DELETE_BREAKPOINT,
            timeout_seconds=(
                timeout_seconds
            ),
            breakpoint_number=(
                breakpoint_number
            ),
        )

    async def enable_breakpoint(
        self,
        task_id: UUID,
        breakpoint_number: str,
        *,
        timeout_seconds: float = 10,
    ) -> None:
        await self._request(
            task_id,
            DebugCommandKind.ENABLE_BREAKPOINT,
            timeout_seconds=(
                timeout_seconds
            ),
            breakpoint_number=(
                breakpoint_number
            ),
        )

    async def disable_breakpoint(
        self,
        task_id: UUID,
        breakpoint_number: str,
        *,
        timeout_seconds: float = 10,
    ) -> None:
        await self._request(
            task_id,
            DebugCommandKind.DISABLE_BREAKPOINT,
            timeout_seconds=(
                timeout_seconds
            ),
            breakpoint_number=(
                breakpoint_number
            ),
        )

    async def run(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.RUN,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def continue_execution(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.CONTINUE,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def next(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.NEXT,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def step(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.STEP,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def interrupt(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.INTERRUPT,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def evaluate(
        self,
        task_id: UUID,
        expression: str,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.EVALUATE,
            timeout_seconds=(
                timeout_seconds
            ),
            expression=expression,
        )

    async def stack_frames(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.STACK_FRAMES,
            timeout_seconds=(
                timeout_seconds
            ),
        )

    async def wait_for_stop(
        self,
        task_id: UUID,
        *,
        stop_timeout_seconds: (
            float
            | None
        ) = None,
        request_timeout_seconds: (
            float
            | None
        ) = None,
    ) -> dict[str, Any]:
        return await self._request(
            task_id,
            DebugCommandKind.WAIT_FOR_STOP,
            timeout_seconds=(
                request_timeout_seconds
            ),
            stop_timeout_seconds=(
                stop_timeout_seconds
            ),
        )

    async def close_session(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        result = await self._request(
            task_id,
            DebugCommandKind.CLOSE,
            timeout_seconds=(
                timeout_seconds
            ),
        )

        self.unregister(
            task_id
        )

        return result

    async def _request(
        self,
        task_id: UUID,
        operation: DebugCommandKind,
        *,
        timeout_seconds: (
            float
            | None
        ),
        stop_timeout_seconds: (
            float
            | None
        ) = None,
        **arguments: Any,
    ) -> Any:
        broker = self.require(
            task_id
        )

        if (
            operation
            == DebugCommandKind.WAIT_FOR_STOP
        ):
            arguments[
                "stop_timeout_seconds"
            ] = stop_timeout_seconds

        return await broker.request(
            operation,
            timeout_seconds=(
                timeout_seconds
            ),
            **arguments,
        )