from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.errors import AppError
from app.modules.co_debug.debug.manager import (
    DebugSessionManager,
)
from app.modules.co_debug.schemas.debug import (
    DebugSessionResponse,
    DebugSessionStateResponse,
)
from app.modules.co_debug.services.interactive_debug_service import (
    InteractiveDebugService,
)
from app.platform.domain.enums import (
    TaskStatus,
    TaskType,
)
from app.platform.domain.task import (
    TaskRecord,
)
from app.platform.schemas.tasks import (
    DebugTaskRequest,
)
from app.platform.services.task_store import (
    TaskStore,
)


class DebugSessionService:
    """
    B7/B8 对 REST API 暴露的统一业务门面。

    Route 不直接访问：

        DebugSessionManager
        DebugCommandBroker
        GdbMiSession

    所有调试操作统一经过本 Service。
    """

    def __init__(
        self,
        task_store: TaskStore,
        session_manager: (
            DebugSessionManager
            | None
        ) = None,
        interactive_service: (
            InteractiveDebugService
            | None
        ) = None,
    ) -> None:
        self.task_store = task_store

        self.session_manager = (
            session_manager
        )

        self.interactive_service = (
            interactive_service
        )

    async def create(
        self,
        request: DebugTaskRequest,
    ) -> TaskRecord:
        if (
            self.interactive_service
            is None
        ):
            raise AppError(
                "interactive debug service "
                "is unavailable"
            )

        return await (
            self.interactive_service
            .create_task(request)
        )

    def describe(
        self,
        task_id: UUID,
    ) -> DebugSessionResponse:
        task = self._require_debug_task(
            task_id
        )

        active = (
            self.session_manager
            is not None
            and self.session_manager
            .contains(task_id)
        )

        return DebugSessionResponse(
            task_id=task_id,
            protocol="GDB/MI",
            supported_commands=[
                "set_arguments",
                "insert_breakpoint",
                "delete_breakpoint",
                "run",
                "continue",
                "next",
                "step",
                "interrupt",
                "evaluate",
                "stack_frames",
                "wait_for_stop",
                "close",
            ],
            note=(
                "Interactive GDB/MI "
                "session is active."
                if active
                else (
                    "The DEBUG task exists "
                    "but has no active "
                    "GDB/MI session."
                )
            ),
        )

    async def state(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        task = self._require_debug_task(
            task_id
        )

        manager = self.session_manager

        if (
            manager is not None
            and manager.contains(
                task_id
            )
        ):
            snapshot = (
                await manager.state(
                    task_id
                )
            )

            return (
                self._state_response(
                    task,
                    snapshot,
                    active=True,
                )
            )

        final_state = None

        if isinstance(
            task.result,
            dict,
        ):
            value = task.result.get(
                "final_state"
            )

            if isinstance(
                value,
                str,
            ):
                final_state = value

        if final_state is None:
            if task.status in {
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
            }:
                final_state = "STARTING"

            elif (
                task.status
                == TaskStatus.FAILED
            ):
                final_state = "FAILED"

            else:
                final_state = "EXITED"

        return DebugSessionStateResponse(
            task_id=task.id,
            task_status=task.status,
            active=False,
            state=final_state,
        )

    async def set_arguments(
        self,
        task_id: UUID,
        arguments: list[str],
    ) -> None:
        manager = self._require_active(
            task_id
        )

        await manager.set_arguments(
            task_id,
            arguments,
        )

    async def insert_breakpoint(
        self,
        task_id: UUID,
        *,
        location: str,
        temporary: bool = False,
        disabled: bool = False,
        condition: str | None = None,
    ) -> dict[str, Any]:
        manager = self._require_active(
            task_id
        )

        return await (
            manager.insert_breakpoint(
                task_id,
                location,
                temporary=temporary,
                disabled=disabled,
                condition=condition,
            )
        )

    async def delete_breakpoint(
        self,
        task_id: UUID,
        breakpoint_number: str,
    ) -> None:
        manager = self._require_active(
            task_id
        )

        await manager.delete_breakpoint(
            task_id,
            breakpoint_number,
        )

    async def run(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        return await self._state_command(
            task_id,
            "run",
        )

    async def continue_execution(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        return await self._state_command(
            task_id,
            "continue_execution",
        )

    async def next(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        return await self._state_command(
            task_id,
            "next",
        )

    async def step(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        return await self._state_command(
            task_id,
            "step",
        )

    async def interrupt(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        return await self._state_command(
            task_id,
            "interrupt",
        )

    async def wait_for_stop(
        self,
        task_id: UUID,
        *,
        timeout_seconds: (
            float
            | None
        ) = None,
    ) -> DebugSessionStateResponse:
        task = self._require_debug_task(
            task_id
        )

        manager = self._require_active(
            task_id
        )

        snapshot = (
            await manager.wait_for_stop(
                task_id,
                stop_timeout_seconds=(
                    timeout_seconds
                ),
                request_timeout_seconds=(
                    None
                    if timeout_seconds is None
                    else timeout_seconds + 1
                ),
            )
        )

        return self._state_response(
            task,
            snapshot,
            active=True,
        )

    async def evaluate(
        self,
        task_id: UUID,
        expression: str,
    ) -> dict[str, Any]:
        manager = self._require_active(
            task_id
        )

        return await manager.evaluate(
            task_id,
            expression,
        )

    async def stack_frames(
        self,
        task_id: UUID,
    ) -> dict[str, Any]:
        manager = self._require_active(
            task_id
        )

        return await (
            manager.stack_frames(
                task_id
            )
        )

    async def close(
        self,
        task_id: UUID,
    ) -> DebugSessionStateResponse:
        task = self._require_debug_task(
            task_id
        )

        manager = self._require_active(
            task_id
        )

        snapshot = (
            await manager.close_session(
                task_id
            )
        )

        return self._state_response(
            task,
            snapshot,
            active=False,
        )

    async def _state_command(
        self,
        task_id: UUID,
        operation: str,
    ) -> DebugSessionStateResponse:
        task = self._require_debug_task(
            task_id
        )

        manager = self._require_active(
            task_id
        )

        method = getattr(
            manager,
            operation,
        )

        snapshot = await method(
            task_id
        )

        return self._state_response(
            task,
            snapshot,
            active=True,
        )

    def _require_debug_task(
        self,
        task_id: UUID,
    ) -> TaskRecord:
        task = self.task_store.require(
            task_id
        )

        if (
            task.task_type
            != TaskType.DEBUG
        ):
            raise AppError(
                "task is not a DEBUG task"
            )

        return task

    def _require_active(
        self,
        task_id: UUID,
    ) -> DebugSessionManager:
        self._require_debug_task(
            task_id
        )

        manager = (
            self.session_manager
        )

        if (
            manager is None
            or not manager.contains(
                task_id
            )
        ):
            raise AppError(
                "debug session is not active"
            )

        return manager

    @staticmethod
    def _state_response(
        task: TaskRecord,
        snapshot: dict[str, Any],
        *,
        active: bool,
    ) -> DebugSessionStateResponse:
        return DebugSessionStateResponse(
            task_id=task.id,
            task_status=task.status,
            active=active,
            state=snapshot["state"],
            stop_reason=(
                snapshot.get(
                    "stop_reason"
                )
            ),
            current_file=(
                snapshot.get(
                    "current_file"
                )
            ),
            current_line=(
                snapshot.get(
                    "current_line"
                )
            ),
            current_function=(
                snapshot.get(
                    "current_function"
                )
            ),
            breakpoints=list(
                snapshot.get(
                    "breakpoints",
                    [],
                )
            ),
            console_output=list(
                snapshot.get(
                    "console_output",
                    [],
                )
            ),
            target_output=list(
                snapshot.get(
                    "target_output",
                    [],
                )
            ),
            log_output=list(
                snapshot.get(
                    "log_output",
                    [],
                )
            ),
            stderr_output=list(
                snapshot.get(
                    "stderr_output",
                    [],
                )
            ),
        )