from __future__ import annotations

import asyncio
import time

from app.core.errors import AppError
from app.modules.co_debug.debug.broker import DebugCommandBroker
from app.modules.co_debug.debug.manager import (
    DebugSessionManager,
    GdbMiCommandDispatcher,
)
from app.modules.co_debug.debug.platform_transport import (
    PlatformGdbTransport,
)
from app.modules.co_debug.debug.session import GdbMiSession
from app.modules.co_debug.debug.transport import GdbTransportClosed
from app.platform.domain.enums import TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.schemas.tasks import DebugTaskRequest
from app.platform.services.task_execution import (
    ManagedTaskContext,
    ManagedTaskResult,
)
from app.platform.services.task_service import TaskService


class InteractiveDebugService:
    """
    B7/B8 的真实 GDB/MI Managed Task 运行入口。

    当前阶段先支持：

        uploaded project source
            ↓
        Managed Task workspace
            ↓
        GDB/MI interactive process

    build_task_id 对应的 retained build workspace
    等 A 补充 managed source workspace 后再接入。
    """

    def __init__(
        self,
        *,
        task_service: TaskService,
        session_manager: DebugSessionManager,
    ) -> None:
        self.task_service = task_service
        self.session_manager = session_manager
        self.dispatcher = GdbMiCommandDispatcher()

    async def create_task(
        self,
        request: DebugTaskRequest,
    ) -> TaskRecord:
        if request.build_task_id is not None:
            raise AppError(
                "interactive debug from build task "
                "requires managed source workspace support"
            )

        logical_executable = (
            self.task_service
            .find_process_source_file(
                project_id=request.project_id,
                path=request.executable_path,
                source_task_id=None,
            )
        )

        if logical_executable is None:
            raise AppError(
                "debug executable does not exist: "
                f"{request.executable_path}"
            )

        logical_command = self._gdb_command(
            logical_executable,
            request.args,
        )

        broker = DebugCommandBroker()

        async def execute(
            context: ManagedTaskContext,
        ) -> ManagedTaskResult:
            started = time.perf_counter()

            workspace = context.create_workspace(
                "debug"
            )

            executable = context.resolve_path(
                workspace,
                logical_executable,
            )

            if not executable.is_file():
                raise AppError(
                    "debug executable does not exist: "
                    f"{request.executable_path}"
                )

            transport: PlatformGdbTransport | None = None
            session: GdbMiSession | None = None
            dispatcher_task: asyncio.Task[None] | None = None
            process_wait_task: asyncio.Task[int] | None = None
            registered = False

            try:
                context.report_progress(
                    10,
                    "starting GDB/MI session",
                    stream="co_debug.gdb",
                )

                transport = await PlatformGdbTransport.open(
                    context=context,
                    command=self._gdb_command(
                        str(executable),
                        request.args,
                    ),
                    workspace=workspace,
                    work_dir=request.work_dir,
                )

                session = GdbMiSession(
                    transport,
                    command_timeout_seconds=(
                        context.timeout_seconds
                    ),
                )

                await session.start(
                    timeout_seconds=(
                        context.timeout_seconds
                    )
                )

                # 只有真正收到 (gdb) prompt 后，
                # 才向API侧暴露这个Session。
                self.session_manager.register(
                    context.task_id,
                    broker,
                )
                registered = True

                context.report_progress(
                    50,
                    "GDB/MI session ready",
                    stream="co_debug.gdb",
                )

                dispatcher_task = asyncio.create_task(
                    self.dispatcher.run(
                        broker=broker,
                        session=session,
                    )
                )

                process_wait_task = asyncio.create_task(
                    transport.wait()
                )

                done, _ = await asyncio.wait(
                    {
                        dispatcher_task,
                        process_wait_task,
                    },
                    return_when=asyncio.FIRST_COMPLETED,
                )
                elapsed_ms = round(
                    (
                        time.perf_counter()
                        - started
                    )
                    * 1000
                )

                 # --------------------------
                # 情况1：
                # Dispatcher先结束。
                #
                # 当前正常情况就是客户端发送CLOSE。
                # --------------------------

                if dispatcher_task in done:
                    await dispatcher_task

                    return ManagedTaskResult(
                        status=TaskStatus.SUCCEEDED,
                        result={
                            "protocol": "GDB/MI",
                            "final_state": (
                                session.state.state.value
                            ),
                            "closed_by": "client",
                        },
                        exit_code=0,
                        elapsed_ms=elapsed_ms,
                    )

                # --------------------------
                # 情况2：
                # GDB进程先结束。
                # --------------------------
                assert (
                    process_wait_task
                    in done
                )

                exit_code = (
                    await process_wait_task
                )
                # 这里非常关键：
                #
                # client CLOSE 会先令
                #
                #     session._closing = True
                #
                # 然后 terminate GDB。
                #
                # 因此GDB可能比dispatcher更早结束，
                # 此时 -15 / -9 属于主动关闭，
                # 不能解释为DEBUG失败。
                if session.closing:
                    await dispatcher_task

                    return ManagedTaskResult(
                        status=TaskStatus.SUCCEEDED,
                        result={
                            "protocol": "GDB/MI",
                            "final_state": (
                                session.state.state.value
                            ),
                            "closed_by": "client",
                        },
                        exit_code=0,
                        elapsed_ms=elapsed_ms,
                    )
                # --------------------------
                # 情况3：
                # 没有client CLOSE，
                # GDB自己先退出。
                # --------------------------
                broker.close(
                    GdbTransportClosed(
                        exit_code
                    )
                )
                await dispatcher_task
                succeeded = (
                    exit_code == 0
                )

                return ManagedTaskResult(
                    status=(
                        TaskStatus.SUCCEEDED
                        if succeeded
                        else TaskStatus.FAILED
                    ),
                    result={
                        "protocol": "GDB/MI",
                        "final_state": (
                            session.state.state.value
                        ),
                        "closed_by": "process",
                    },
                    exit_code=exit_code,
                    elapsed_ms=elapsed_ms,
                    error=(
                        None
                        if succeeded
                        else (
                            "GDB exited with code "
                            f"{exit_code}"
                        )
                    ),
                )

            finally:
                if registered:
                    self.session_manager.unregister(
                        context.task_id
                    )

                # 这些只是B自己的等待Task，
                # 真正的OS进程由A负责最终terminate。
                for task in (
                    dispatcher_task,
                    process_wait_task,
                ):
                    if (
                        task is not None
                        and not task.done()
                    ):
                        task.cancel()

                await asyncio.gather(
                    *(
                        task
                        for task in (
                            dispatcher_task,
                            process_wait_task,
                        )
                        if task is not None
                    ),
                    return_exceptions=True,
                )

        return await self.task_service.create_managed_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.DEBUG,
            command=logical_command,
            execute=execute,
            metadata={
                **request.metadata,
                "debug_protocol": "GDB/MI",
                "interactive": True,
            },
            timeout_seconds=request.timeout_seconds,
        )

    @staticmethod
    def _gdb_command(
        executable: str,
        args: list[str],
    ) -> list[str]:
        return [
            "gdb",
            "--interpreter=mi2",
            executable,
            *args,
        ]