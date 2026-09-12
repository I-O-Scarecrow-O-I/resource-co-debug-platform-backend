from __future__ import annotations

import asyncio
from typing import Any

from app.modules.co_debug.debug.mi_commands import (
    MiCommandBuilder,
)
from app.modules.co_debug.debug.mi_parser import (
    MiParseError,
    parse_mi_line,
)
from app.modules.co_debug.debug.models import (
    DebugBreakpoint,
    DebugSessionState,
    DebugSessionStateModel,
    MiCommand,
    MiRecord,
    MiRecordKind,
)
from app.modules.co_debug.debug.transport import (
    GdbTransport,
    GdbTransportClosed,
)


class GdbMiError(RuntimeError):
    """
    GDB/MI 会话基础异常。
    """


class GdbMiTimeout(GdbMiError):
    """
    等待 GDB/MI 响应超时。
    """


class GdbMiCommandError(GdbMiError):
    """
    GDB 返回 ^error。
    """

    def __init__(
        self,
        command: MiCommand,
        record: MiRecord,
    ) -> None:
        self.command = command
        self.record = record

        message = None

        if isinstance(
            record.payload,
            dict,
        ):
            value = record.payload.get(
                "msg"
            )

            if isinstance(
                value,
                str,
            ):
                message = value

        super().__init__(
            message
            or (
                "GDB/MI command failed: "
                f"{command.text}"
            )
        )


class GdbMiSession:
    """
    一个独立的 GDB/MI 调试会话。

    主要职责：

        1. 生成带 token 的 MI 命令
        2. 将 token 和 ^result 对应
        3. 处理 GDB 异步事件
        4. 维护 RUNNING / STOPPED / EXITED 状态
        5. 管理断点状态

    不负责：

        1. 创建 subprocess
        2. 管理 Workspace
        3. Task 生命周期
        4. Task 取消
        5. HTTP API

    这些分别属于 Platform 或更上层 Service。
    """

    def __init__(
        self,
        transport: GdbTransport,
        *,
        command_timeout_seconds: float = 10.0,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError(
                "command_timeout_seconds "
                "must be greater than 0"
            )

        self.transport = transport

        self.command_timeout_seconds = (
            command_timeout_seconds
        )

        self.command_builder = (
            MiCommandBuilder()
        )

        self.state = (
            DebugSessionStateModel()
        )

        self._pending: dict[
            int,
            asyncio.Future[MiRecord],
        ] = {}

        self._reader_task: (
            asyncio.Task[None]
            | None
        ) = None

        self._ready_future: (
            asyncio.Future[None]
            | None
        ) = None

        self._stop_event = (
            asyncio.Event()
        )

        self._started = False
        self._closing=False
        self._closed = False
        self._console_output: list[
            str
        ] = []

        self._target_output: list[
            str
        ] = []

        self._log_output: list[
            str
        ] = []

        self._stderr_output: list[
            str
        ] = []

    @property
    def started(
        self,
    ) -> bool:
        return self._started

    @property
    def closed(
        self,
    ) -> bool:
        return self._closed
    @property
    def closing(
        self,
    ) -> bool:
        return self._closing
    @property
    def console_output(
        self,
    ) -> tuple[str, ...]:
        return tuple(
            self._console_output
        )

    @property
    def target_output(
        self,
    ) -> tuple[str, ...]:
        return tuple(
            self._target_output
        )

    @property
    def log_output(
        self,
    ) -> tuple[str, ...]:
        return tuple(
            self._log_output
        )

    @property
    def stderr_output(
        self,
    ) -> tuple[str, ...]:
        return tuple(
            self._stderr_output
        )

    async def start(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        """
        启动 MI reader，并等待：

            (gdb)

        初始 prompt。

        GDB 启动成功后：

            STARTING -> READY
        """

        if self._closed:
            raise GdbMiError(
                "GDB/MI session is closed"
            )

        if self._started:
            return

        self._started = True

        loop = (
            asyncio.get_running_loop()
        )

        self._ready_future = (
            loop.create_future()
        )

        self._reader_task = (
            asyncio.create_task(
                self._reader_loop()
            )
        )

        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.command_timeout_seconds
        )

        try:
            await asyncio.wait_for(
                asyncio.shield(
                    self._ready_future
                ),
                timeout=timeout,
            )

        except TimeoutError as exc:
            self._set_state(
                DebugSessionState.FAILED
            )

            raise GdbMiTimeout(
                "timed out waiting for "
                "initial GDB/MI prompt"
            ) from exc

    async def set_arguments(
        self,
        arguments: list[str],
    ) -> None:
        command = (
            self.command_builder
            .exec_arguments(
                arguments
            )
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

    async def insert_breakpoint(
        self,
        location: str,
        *,
        temporary: bool = False,
        disabled: bool = False,
        condition: str | None = None,
    ) -> DebugBreakpoint:
        command = (
            self.command_builder
            .break_insert(
                location,
                temporary=temporary,
                disabled=disabled,
                condition=condition,
            )
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        breakpoint = (
            self._breakpoint_from_record(
                location,
                record,
            )
        )

        self.state.breakpoints[
            breakpoint.number
        ] = breakpoint

        return breakpoint

    async def delete_breakpoint(
        self,
        breakpoint_number: str,
    ) -> None:
        command = (
            self.command_builder
            .break_delete(
                breakpoint_number
            )
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        self.state.breakpoints.pop(
            breakpoint_number,
            None,
        )

    async def enable_breakpoint(
        self,
        breakpoint_number: str,
    ) -> None:
        command = (
            self.command_builder
            .break_enable(
                breakpoint_number
            )
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        breakpoint = (
            self.state.breakpoints.get(
                breakpoint_number
            )
        )

        if breakpoint is not None:
            breakpoint.enabled = True

    async def disable_breakpoint(
        self,
        breakpoint_number: str,
    ) -> None:
        command = (
            self.command_builder
            .break_disable(
                breakpoint_number
            )
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        breakpoint = (
            self.state.breakpoints.get(
                breakpoint_number
            )
        )

        if breakpoint is not None:
            breakpoint.enabled = False

    async def run(
        self,
    ) -> None:
        command = (
            self.command_builder
            .exec_run()
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "running",
                "done",
            },
        )

    async def continue_execution(
        self,
    ) -> None:
        command = (
            self.command_builder
            .exec_continue()
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "running",
                "done",
            },
        )


    async def next(
        self,
    ) -> None:
        command = (
            self.command_builder
            .exec_next()
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "running",
                "done",
            },
        )


    async def step(
        self,
    ) -> None:
        command = (
            self.command_builder
            .exec_step()
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "running",
                "done",
            },
        )


    async def interrupt(
        self,
    ) -> None:
        command = (
            self.command_builder
            .exec_interrupt()
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
                "running",
            },
        )

    async def evaluate_expression(
        self,
        expression: str,
    ) -> str | None:
        command = (
            self.command_builder
            .data_evaluate_expression(
                expression
            )
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        if not isinstance(
            record.payload,
            dict,
        ):
            return None

        value = record.payload.get(
            "value"
        )

        return (
            value
            if isinstance(
                value,
                str,
            )
            else None
        )

    async def list_stack_frames(
        self,
    ) -> list[Any]:
        command = (
            self.command_builder
            .stack_list_frames()
        )

        record = await self._execute(
            command
        )

        self._require_success(
            command,
            record,
            allowed_classes={
                "done",
            },
        )

        if not isinstance(
            record.payload,
            dict,
        ):
            return []

        stack = record.payload.get(
            "stack"
        )

        if not isinstance(
            stack,
            list,
        ):
            return []

        return stack

    async def wait_for_stop(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> DebugSessionState:
        """
        等待 inferior：

            STOPPED
            EXITED

        或 session：

            FAILED
        """

        if (
            self.state.state
            in {
                DebugSessionState.STOPPED,
                DebugSessionState.EXITED,
                DebugSessionState.FAILED,
            }
        ):
            return self.state.state

        if timeout_seconds is None:
            await self._stop_event.wait()

        else:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=timeout_seconds,
                )

            except TimeoutError as exc:
                raise GdbMiTimeout(
                    "timed out waiting "
                    "for GDB to stop"
                ) from exc

        return self.state.state

    async def close(
        self,
    ) -> None:
        """
        关闭当前 GDB/MI 会话。

        close 是主动的生命周期结束，不应把底层进程
        被 terminate 后产生的非零退出码解释为调试失败。

        OS 级进程清理由 Transport / Platform 负责。
        """

        if self._closed:
            return

        if self._closing:
            return

        self._closing = True

        try:
            await self.transport.close()

        finally:
            if (
                self._reader_task
                is not None
                and not self._reader_task.done()
            ):
                self._reader_task.cancel()

            if self._reader_task is not None:
                await asyncio.gather(
                    self._reader_task,
                    return_exceptions=True,
                )

            self._closed = True

            if (
                self.state.state
                != DebugSessionState.FAILED
            ):
                self._set_state(
                    DebugSessionState.EXITED
                )

    async def _execute(
        self,
        command: MiCommand,
    ) -> MiRecord:
        self._require_started()

        if (
            self.state.state
            == DebugSessionState.FAILED
        ):
            raise GdbMiError(
                "GDB/MI session has failed"
            )

        if (
            self.transport.returncode
            is not None
        ):
            raise GdbTransportClosed(
                self.transport.returncode
            )

        loop = (
            asyncio.get_running_loop()
        )

        response_future: (
            asyncio.Future[MiRecord]
        ) = loop.create_future()

        # 一定要先注册 token，
        # 再真正发送命令。
        #
        # 否则极快的 GDB 响应可能先于
        # pending 注册到达。
        self._pending[
            command.token
        ] = response_future

        try:
            await self.transport.send(
                command.text
            )

        except BaseException:
            self._pending.pop(
                command.token,
                None,
            )

            if not response_future.done():
                response_future.cancel()

            raise

        try:
            return await asyncio.wait_for(
                asyncio.shield(
                    response_future
                ),
                timeout=(
                    self.command_timeout_seconds
                ),
            )

        except TimeoutError as exc:
            self._pending.pop(
                command.token,
                None,
            )

            if not response_future.done():
                response_future.cancel()

            raise GdbMiTimeout(
                "timed out waiting for "
                "GDB/MI command response: "
                f"{command.text}"
            ) from exc

    async def _reader_loop(
        self,
    ) -> None:
        try:
            while True:
                message = (
                    await self.transport
                    .receive()
                )

                if (
                    message.stream
                    == "stderr"
                ):
                    self._stderr_output.append(
                        message.text
                    )

                    continue

                try:
                    record = (
                        parse_mi_line(
                            message.text
                        )
                    )

                except MiParseError as exc:
                    raise GdbMiError(
                        "failed to parse "
                        "GDB/MI output: "
                        f"{message.text}"
                    ) from exc

                self._handle_record(
                    record
                )

        except asyncio.CancelledError:
            raise
        except GdbTransportClosed as exc:
            # 如果是 session.close() 主动结束 Transport，
            # 即使底层 terminate 导致 -15 / -9，
            # 也属于正常生命周期结束，而不是 GDB 故障。
            if self._closing:
                self._set_state(
                    DebugSessionState.EXITED
                )

            elif (
                exc.returncode
                in (
                    None,
                    0,
                )
            ):
                self._set_state(
                    DebugSessionState.EXITED
                )

            else:
                self._set_state(
                    DebugSessionState.FAILED
                )

            self._fail_waiters(
                exc
            )

        except BaseException as exc:
            self._set_state(
                DebugSessionState.FAILED
            )

            self._fail_waiters(
                exc
            )

    def _handle_record(
        self,
        record: MiRecord,
    ) -> None:
        if (
            record.kind
            == MiRecordKind.PROMPT
        ):
            if (
                self.state.state
                == DebugSessionState.STARTING
            ):
                self._set_state(
                    DebugSessionState.READY
                )

            if (
                self._ready_future
                is not None
                and not self._ready_future.done()
            ):
                self._ready_future.set_result(
                    None
                )

            return

        if (
            record.kind
            == MiRecordKind.CONSOLE_STREAM
        ):
            if isinstance(
                record.payload,
                str,
            ):
                self._console_output.append(
                    record.payload
                )

            return

        if (
            record.kind
            == MiRecordKind.TARGET_STREAM
        ):
            if isinstance(
                record.payload,
                str,
            ):
                self._target_output.append(
                    record.payload
                )

            return

        if (
            record.kind
            == MiRecordKind.LOG_STREAM
        ):
            if isinstance(
                record.payload,
                str,
            ):
                self._log_output.append(
                    record.payload
                )

            return

        if (
            record.kind
            == MiRecordKind.RESULT
            and record.token is not None
        ):
            response_future = (
                self._pending.pop(
                    record.token,
                    None,
                )
            )

            if (
                response_future
                is not None
                and not response_future.done()
            ):
                response_future.set_result(
                    record
                )

            if record.is_running:
                self._set_state(
                    DebugSessionState.RUNNING
                )

            return

        if (
            record.kind
            == MiRecordKind.EXEC_ASYNC
        ):
            if record.is_running:
                self._set_state(
                    DebugSessionState.RUNNING
                )

                return

            if record.is_stopped:
                self._handle_stopped_record(
                    record
                )

    def _handle_stopped_record(
        self,
        record: MiRecord,
    ) -> None:
        payload = (
            record.payload
            if isinstance(
                record.payload,
                dict,
            )
            else {}
        )

        reason = payload.get(
            "reason"
        )

        self.state.stop_reason = (
            reason
            if isinstance(
                reason,
                str,
            )
            else None
        )

        frame = payload.get(
            "frame"
        )

        if isinstance(
            frame,
            dict,
        ):
            file_value = (
                frame.get("fullname")
                or frame.get("file")
            )

            self.state.current_file = (
                file_value
                if isinstance(
                    file_value,
                    str,
                )
                else None
            )

            function = frame.get(
                "func"
            )

            self.state.current_function = (
                function
                if isinstance(
                    function,
                    str,
                )
                else None
            )

            self.state.current_line = (
                self._to_int(
                    frame.get(
                        "line"
                    )
                )
            )

        else:
            self.state.current_file = None
            self.state.current_function = None
            self.state.current_line = None

        if (
            isinstance(
                reason,
                str,
            )
            and reason.startswith(
                "exited"
            )
        ):
            self._set_state(
                DebugSessionState.EXITED
            )

        else:
            self._set_state(
                DebugSessionState.STOPPED
            )

    def _set_state(
        self,
        state: DebugSessionState,
    ) -> None:
        self.state.state = state

        if state == (
            DebugSessionState.RUNNING
        ):
            self._stop_event.clear()

        elif state in {
            DebugSessionState.STOPPED,
            DebugSessionState.EXITED,
            DebugSessionState.FAILED,
        }:
            self._stop_event.set()

    def _fail_waiters(
        self,
        error: BaseException,
    ) -> None:
        if (
            self._ready_future
            is not None
            and not self._ready_future.done()
        ):
            self._ready_future.set_exception(
                error
            )

        pending = list(
            self._pending.values()
        )

        self._pending.clear()

        for future in pending:
            if not future.done():
                future.set_exception(
                    error
                )

    def _require_started(
        self,
    ) -> None:
        if not self._started:
            raise GdbMiError(
                "GDB/MI session "
                "has not been started"
            )

        if self._closed:
            raise GdbMiError(
                "GDB/MI session is closed"
            )

    @staticmethod
    def _require_success(
        command: MiCommand,
        record: MiRecord,
        *,
        allowed_classes: set[str],
    ) -> None:
        if record.is_error:
            raise GdbMiCommandError(
                command,
                record,
            )

        if (
            record.kind
            != MiRecordKind.RESULT
            or record.message_class
            not in allowed_classes
        ):
            raise GdbMiError(
                "unexpected GDB/MI "
                "response for "
                f"{command.text}: "
                f"{record.raw}"
            )

    @staticmethod
    def _breakpoint_from_record(
        location: str,
        record: MiRecord,
    ) -> DebugBreakpoint:
        payload = (
            record.payload
            if isinstance(
                record.payload,
                dict,
            )
            else {}
        )

        data = payload.get(
            "bkpt"
        )

        if not isinstance(
            data,
            dict,
        ):
            raise GdbMiError(
                "GDB breakpoint response "
                "does not contain bkpt"
            )

        number = data.get(
            "number"
        )

        if not isinstance(
            number,
            str,
        ):
            raise GdbMiError(
                "GDB breakpoint response "
                "does not contain "
                "a breakpoint number"
            )

        enabled = (
            data.get(
                "enabled",
                "y",
            )
            != "n"
        )

        return DebugBreakpoint(
            number=number,
            location=location,
            enabled=enabled,
            file=(
                data.get("file")
                if isinstance(
                    data.get("file"),
                    str,
                )
                else None
            ),
            fullname=(
                data.get("fullname")
                if isinstance(
                    data.get("fullname"),
                    str,
                )
                else None
            ),
            line=GdbMiSession._to_int(
                data.get("line")
            ),
            function=(
                data.get("func")
                if isinstance(
                    data.get("func"),
                    str,
                )
                else None
            ),
        )

    @staticmethod
    def _to_int(
        value: Any,
    ) -> int | None:
        try:
            return int(value)

        except (
            TypeError,
            ValueError,
        ):
            return None