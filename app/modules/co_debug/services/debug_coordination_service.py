from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace
from uuid import UUID, uuid4

from app.core.errors import AppError
from app.core.time import utc_now
from app.modules.co_debug.coordination.models import (
    DebugCoordinationEventOutcome,
    DebugCoordinationEventRecord,
    DebugCoordinationGroup,
    DebugCoordinationGroupStatus,
    DebugCoordinationRule,
    DebugCoordinationRuleAction,
    DebugCoordinationRuleBinding,
    DebugGroupCommand,
    DebugGroupCommandOutcome,
    DebugLogicalPartition,
)
from app.modules.co_debug.debug.events import (
    DebugSessionEvent,
    DebugSessionEventBus,
    DebugSessionEventKind,
)
from app.modules.co_debug.debug.manager import DebugSessionManager
from app.modules.co_debug.schemas.coordination import (
    DebugCoordinationEventResponse,
    DebugCoordinationGroupRequest,
    DebugCoordinationGroupResponse,
    DebugCoordinationRuleRequest,
    DebugCoordinationRuleResponse,
    DebugGroupCommandResponse,
    DebugGroupMemberCommandResult,
    DebugLogicalPartitionResponse,
)
from app.modules.co_debug.schemas.debug import DebugSessionStateResponse
from app.modules.co_debug.services.debug_coordination_store import DebugCoordinationStore
from app.modules.co_debug.services.debug_service import DebugSessionService
from app.platform.domain.enums import TaskStatus, TaskType
from app.platform.services.task_store import TaskStore

logger = logging.getLogger(__name__)

_INTERRUPT_CONFIRMATION_TIMEOUT_SECONDS = 3.0


class DebugCoordinationService:
    def __init__(
        self,
        *,
        store: DebugCoordinationStore,
        task_store: TaskStore,
        session_manager: DebugSessionManager,
        debug_service: DebugSessionService,
        event_bus: DebugSessionEventBus | None = None,
    ) -> None:
        self.store = store
        self.task_store = task_store
        self.session_manager = session_manager
        self.debug_service = debug_service
        self.event_bus = event_bus
        self._operation_locks: dict[UUID, asyncio.Lock] = {}
        self._event_lock = threading.RLock()
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._event_subscription_id: UUID | None = None
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def create(
        self,
        request: DebugCoordinationGroupRequest,
    ) -> DebugCoordinationGroupResponse:
        self._ensure_event_subscription()
        for member in request.members:
            task = self.task_store.require(member.session_task_id)
            if task.project_id != request.project_id:
                raise AppError("all debug sessions must belong to the same project")
            if task.task_type != TaskType.DEBUG:
                raise AppError("coordination members must reference DEBUG tasks")
            if task.status != TaskStatus.RUNNING or not self.session_manager.contains(task.id):
                raise AppError("coordination members must reference active DEBUG sessions")

        created_at = utc_now()
        group = DebugCoordinationGroup(
            id=uuid4(),
            project_id=request.project_id,
            name=request.name,
            created_at=created_at,
        )
        members = [
            DebugLogicalPartition(
                id=uuid4(),
                group_id=group.id,
                name=member.partition_name,
                session_task_id=member.session_task_id,
                joined_at=created_at,
            )
            for member in request.members
        ]
        self.store.create(group, members)
        return await self._response(group)

    async def list(
        self,
        project_id: UUID | None = None,
    ) -> list[DebugCoordinationGroupResponse]:
        self._ensure_event_subscription()
        groups = self.store.list(project_id)
        return await asyncio.gather(*(self._response(group) for group in groups))

    async def get(self, group_id: UUID) -> DebugCoordinationGroupResponse:
        self._ensure_event_subscription()
        return await self._response(self.store.require(group_id))

    async def create_rule(
        self,
        group_id: UUID,
        request: DebugCoordinationRuleRequest,
    ) -> DebugCoordinationRuleResponse:
        self._ensure_event_subscription()
        async with self._lock_for(group_id):
            group = self.store.require(group_id)
            if group.closed_at is not None:
                raise AppError("debug coordination group is closed")
            if request.source_partition_id == request.target_partition_id:
                raise AppError("source and target partitions must be different")

            source = self.store.require_member(group_id, request.source_partition_id)
            target = self.store.require_member(group_id, request.target_partition_id)
            if not source.associated or not target.associated:
                raise AppError("coordination rule members must still be associated")

            source_state, target_state = await asyncio.gather(
                self.debug_service.state(source.session_task_id),
                self.debug_service.state(target.session_task_id),
            )
            if not self._is_active_state(source_state) or not self._is_active_state(target_state):
                raise AppError("coordination rule members must reference active DEBUG sessions")
            breakpoint_numbers = {
                str(item.get("number"))
                for item in source_state.breakpoints
                if isinstance(item, dict) and item.get("number") is not None
            }
            if request.source_breakpoint_number not in breakpoint_numbers:
                raise AppError("source breakpoint does not exist in the active debug session")

            rule = DebugCoordinationRule(
                id=uuid4(),
                group_id=group_id,
                source_partition_id=source.id,
                source_breakpoint_number=request.source_breakpoint_number,
                target_partition_id=target.id,
                action=DebugCoordinationRuleAction.INTERRUPT,
                created_at=utc_now(),
            )
            self.store.create_rule(rule)
            return self._rule_response(rule)

    async def list_rules(self, group_id: UUID) -> list[DebugCoordinationRuleResponse]:
        self._ensure_event_subscription()
        return [self._rule_response(rule) for rule in self.store.list_rules(group_id)]

    async def delete_rule(self, group_id: UUID, rule_id: UUID) -> None:
        async with self._lock_for(group_id):
            self.store.delete_rule(group_id, rule_id)

    async def list_events(
        self,
        group_id: UUID,
        *,
        limit: int = 100,
    ) -> list[DebugCoordinationEventResponse]:
        self._ensure_event_subscription()
        return [
            self._event_response(event) for event in self.store.list_events(group_id, limit=limit)
        ]

    async def remove_member(
        self,
        group_id: UUID,
        partition_id: UUID,
    ) -> DebugCoordinationGroupResponse:
        async with self._lock_for(group_id):
            self.store.remove_member(group_id, partition_id)
            return await self.get(group_id)

    async def close_group(self, group_id: UUID) -> DebugCoordinationGroupResponse:
        """Close only the association. Existing GDB sessions remain untouched."""
        async with self._lock_for(group_id):
            group = self.store.close_group(group_id)
            return await self._response(group)

    async def run(self, group_id: UUID) -> DebugGroupCommandResponse:
        return await self._command(group_id, DebugGroupCommand.RUN)

    async def continue_execution(self, group_id: UUID) -> DebugGroupCommandResponse:
        return await self._command(group_id, DebugGroupCommand.CONTINUE)

    async def interrupt(self, group_id: UUID) -> DebugGroupCommandResponse:
        return await self._command(group_id, DebugGroupCommand.INTERRUPT)

    async def close_sessions(self, group_id: UUID) -> DebugGroupCommandResponse:
        """Close member sessions without closing or deleting the group association."""
        return await self._command(group_id, DebugGroupCommand.CLOSE_SESSIONS)

    async def _command(
        self,
        group_id: UUID,
        command: DebugGroupCommand,
    ) -> DebugGroupCommandResponse:
        async with self._lock_for(group_id):
            group = self.store.require(group_id)
            if group.closed_at is not None:
                raise AppError("debug coordination group is closed")
            members = self.store.list_members(group_id, include_removed=False)
            if len(members) < 2 and command != DebugGroupCommand.CLOSE_SESSIONS:
                raise AppError("debug coordination group requires at least two associated members")
            results = await asyncio.gather(
                *(self._command_member(member, command) for member in members)
            )
            return DebugGroupCommandResponse(
                group_id=group_id,
                command=command,
                all_succeeded=bool(results)
                and all(result.outcome == DebugGroupCommandOutcome.SUCCEEDED for result in results),
                results=results,
            )

    async def _command_member(
        self,
        member: DebugLogicalPartition,
        command: DebugGroupCommand,
    ) -> DebugGroupMemberCommandResult:
        try:
            before = await self.debug_service.state(member.session_task_id)
        except Exception as exc:
            return self._command_result(
                member,
                DebugGroupCommandOutcome.FAILED,
                error=self._error_message(exc),
            )

        skip_reason = self._skip_reason(before, command)
        if skip_reason is not None:
            return self._command_result(
                member,
                DebugGroupCommandOutcome.SKIPPED,
                state=before,
                error=skip_reason,
            )

        try:
            if command == DebugGroupCommand.RUN:
                state = await self.debug_service.run(member.session_task_id)
            elif command == DebugGroupCommand.CONTINUE:
                state = await self.debug_service.continue_execution(member.session_task_id)
            elif command == DebugGroupCommand.INTERRUPT:
                state = await self.debug_service.interrupt(member.session_task_id)
            else:
                state = await self.debug_service.close(member.session_task_id)
        except Exception as exc:
            return self._command_result(
                member,
                DebugGroupCommandOutcome.FAILED,
                state=before,
                error=self._error_message(exc),
            )

        return self._command_result(
            member,
            DebugGroupCommandOutcome.SUCCEEDED,
            state=state,
        )

    @staticmethod
    def _skip_reason(
        state: DebugSessionStateResponse,
        command: DebugGroupCommand,
    ) -> str | None:
        if not state.active:
            return "debug session is not active"
        if state.task_status != TaskStatus.RUNNING:
            return "debug task is not running"
        normalized = state.state.upper()
        required = {
            DebugGroupCommand.RUN: "READY",
            DebugGroupCommand.CONTINUE: "STOPPED",
            DebugGroupCommand.INTERRUPT: "RUNNING",
        }.get(command)
        if required is not None and normalized != required:
            return f"debug session must be {required}"
        return None

    @staticmethod
    def _command_result(
        member: DebugLogicalPartition,
        outcome: DebugGroupCommandOutcome,
        *,
        state: DebugSessionStateResponse | None = None,
        error: str | None = None,
    ) -> DebugGroupMemberCommandResult:
        return DebugGroupMemberCommandResult(
            partition_id=member.id,
            partition_name=member.name,
            session_task_id=member.session_task_id,
            outcome=outcome,
            state=state,
            error=error,
        )

    async def _response(
        self,
        group: DebugCoordinationGroup,
    ) -> DebugCoordinationGroupResponse:
        members = self.store.list_members(group.id)
        member_responses = await asyncio.gather(
            *(self._member_response(member) for member in members)
        )
        associated_members = [member for member in member_responses if member.associated]
        active_count = sum(
            member.session is not None
            and member.session.active
            and member.session.task_status == TaskStatus.RUNNING
            for member in associated_members
        )
        if group.closed_at is not None:
            status = DebugCoordinationGroupStatus.CLOSED
        elif (
            len(associated_members) >= 2
            and active_count == len(associated_members)
            and all(member.error is None for member in associated_members)
        ):
            status = DebugCoordinationGroupStatus.ACTIVE
        else:
            status = DebugCoordinationGroupStatus.DEGRADED
        return DebugCoordinationGroupResponse(
            group_id=group.id,
            project_id=group.project_id,
            name=group.name,
            status=status,
            created_at=group.created_at,
            closed_at=group.closed_at,
            active_member_count=active_count,
            members=member_responses,
        )

    async def _member_response(
        self,
        member: DebugLogicalPartition,
    ) -> DebugLogicalPartitionResponse:
        state = None
        error = None
        if member.associated:
            try:
                state = await self.debug_service.state(member.session_task_id)
            except Exception as exc:
                error = self._error_message(exc)
        return DebugLogicalPartitionResponse(
            partition_id=member.id,
            partition_name=member.name,
            session_task_id=member.session_task_id,
            associated=member.associated,
            joined_at=member.joined_at,
            removed_at=member.removed_at,
            session=state,
            error=error,
        )

    def _lock_for(self, group_id: UUID) -> asyncio.Lock:
        return self._operation_locks.setdefault(group_id, asyncio.Lock())

    def _ensure_event_subscription(self) -> None:
        event_bus = self.event_bus
        if event_bus is None:
            return
        loop = asyncio.get_running_loop()
        with self._event_lock:
            if self._closed:
                raise RuntimeError("debug coordination service is closed")
            if self._event_subscription_id is not None and self._event_loop is loop:
                return
            if self._event_subscription_id is not None:
                event_bus.unsubscribe(self._event_subscription_id)
            self._event_loop = loop
            self._event_subscription_id = event_bus.subscribe(loop, self._schedule_event)

    def _schedule_event(self, event: DebugSessionEvent) -> None:
        if self._closed:
            return
        task = asyncio.create_task(self._handle_session_event(event))
        self._event_tasks.add(task)
        task.add_done_callback(self._event_task_done)

    def _event_task_done(self, task: asyncio.Task[None]) -> None:
        self._event_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "debug coordination event processing failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _handle_session_event(self, event: DebugSessionEvent) -> None:
        if event.kind != DebugSessionEventKind.BREAKPOINT_HIT:
            return
        bindings = self.store.matching_rules(
            event.session_task_id,
            event.breakpoint_number,
        )
        await asyncio.gather(*(self._handle_rule_event(event, binding) for binding in bindings))

    async def _handle_rule_event(
        self,
        source_event: DebugSessionEvent,
        binding: DebugCoordinationRuleBinding,
    ) -> None:
        initial = DebugCoordinationEventRecord(
            id=uuid4(),
            source_event_id=source_event.event_id,
            group_id=binding.rule.group_id,
            rule_id=binding.rule.id,
            source_partition_id=binding.source.id,
            source_session_task_id=binding.source.session_task_id,
            target_partition_id=binding.target.id,
            target_session_task_id=binding.target.session_task_id,
            breakpoint_number=source_event.breakpoint_number,
            occurred_at=source_event.occurred_at,
            processed_at=utc_now(),
            outcome=DebugCoordinationEventOutcome.PROCESSING,
            command_sent=False,
            target_paused=False,
            message="processing breakpoint coordination rule",
        )
        if not self.store.claim_event(initial):
            return

        async with self._lock_for(binding.rule.group_id):
            try:
                final = await self._execute_rule_event(initial, binding)
            except Exception as exc:
                final = self._finish_event(
                    initial,
                    DebugCoordinationEventOutcome.FAILED,
                    f"coordination rule failed: {self._error_message(exc)}",
                )
            self.store.finalize_event(final)

    async def _execute_rule_event(
        self,
        event: DebugCoordinationEventRecord,
        binding: DebugCoordinationRuleBinding,
    ) -> DebugCoordinationEventRecord:
        group = self.store.require(binding.rule.group_id)
        source = self.store.require_member(group.id, binding.source.id)
        target = self.store.require_member(group.id, binding.target.id)
        if group.closed_at is not None:
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                "group is closed",
            )
        if not source.associated:
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                "source partition is no longer associated",
            )
        if not target.associated:
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                "target partition is no longer associated",
            )

        try:
            target_state = await self.debug_service.state(target.session_task_id)
        except Exception as exc:
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.FAILED,
                f"failed to read target state: {self._error_message(exc)}",
            )

        if not self._is_active_state(target_state):
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                "target debug session is not active",
            )
        if target_state.state.upper() == "STOPPED":
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                "target debug session is already stopped",
                target_paused=True,
            )
        if target_state.state.upper() != "RUNNING":
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.SKIPPED,
                f"target debug session is {target_state.state}",
            )

        try:
            interrupted = await self.debug_service.interrupt(target.session_task_id)
        except Exception as exc:
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.FAILED,
                f"failed to send interrupt: {self._error_message(exc)}",
            )

        if interrupted.active and interrupted.state.upper() == "STOPPED":
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.INTERRUPT_CONFIRMED,
                "interrupt command succeeded and target is stopped",
                command_sent=True,
                target_paused=True,
            )

        try:
            stopped = await self.debug_service.wait_for_stop(
                target.session_task_id,
                timeout_seconds=_INTERRUPT_CONFIRMATION_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return self._finish_event(
                event,
                DebugCoordinationEventOutcome.INTERRUPT_SENT,
                "interrupt command succeeded but stop was not confirmed: "
                f"{self._error_message(exc)}",
                command_sent=True,
            )

        paused = stopped.active and stopped.state.upper() == "STOPPED"
        return self._finish_event(
            event,
            (
                DebugCoordinationEventOutcome.INTERRUPT_CONFIRMED
                if paused
                else DebugCoordinationEventOutcome.INTERRUPT_SENT
            ),
            (
                "interrupt command succeeded and target is stopped"
                if paused
                else f"interrupt command succeeded but target reached {stopped.state}"
            ),
            command_sent=True,
            target_paused=paused,
        )

    @staticmethod
    def _finish_event(
        event: DebugCoordinationEventRecord,
        outcome: DebugCoordinationEventOutcome,
        message: str,
        *,
        command_sent: bool = False,
        target_paused: bool = False,
    ) -> DebugCoordinationEventRecord:
        return replace(
            event,
            processed_at=utc_now(),
            outcome=outcome,
            command_sent=command_sent,
            target_paused=target_paused,
            message=message,
        )

    @staticmethod
    def _is_active_state(state: DebugSessionStateResponse) -> bool:
        return state.active and state.task_status == TaskStatus.RUNNING

    @staticmethod
    def _rule_response(rule: DebugCoordinationRule) -> DebugCoordinationRuleResponse:
        return DebugCoordinationRuleResponse(
            rule_id=rule.id,
            group_id=rule.group_id,
            source_partition_id=rule.source_partition_id,
            source_breakpoint_number=rule.source_breakpoint_number,
            target_partition_id=rule.target_partition_id,
            action=rule.action,
            created_at=rule.created_at,
        )

    @staticmethod
    def _event_response(event: DebugCoordinationEventRecord) -> DebugCoordinationEventResponse:
        return DebugCoordinationEventResponse(
            event_id=event.id,
            source_event_id=event.source_event_id,
            group_id=event.group_id,
            rule_id=event.rule_id,
            source_partition_id=event.source_partition_id,
            source_session_task_id=event.source_session_task_id,
            target_partition_id=event.target_partition_id,
            target_session_task_id=event.target_session_task_id,
            breakpoint_number=event.breakpoint_number,
            occurred_at=event.occurred_at,
            processed_at=event.processed_at,
            outcome=event.outcome,
            command_sent=event.command_sent,
            target_paused=event.target_paused,
            message=event.message,
        )

    def close(self) -> None:
        with self._event_lock:
            self._closed = True
            if self.event_bus is not None and self._event_subscription_id is not None:
                self.event_bus.unsubscribe(self._event_subscription_id)
            self._event_subscription_id = None
            loop = self._event_loop
            tasks = tuple(self._event_tasks)
            self._event_loop = None
        if loop is not None and tasks:

            def cancel_tasks() -> None:
                for task in tasks:
                    task.cancel()

            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                cancel_tasks()
            else:
                try:
                    loop.call_soon_threadsafe(cancel_tasks)
                except RuntimeError:
                    pass

    @staticmethod
    def _error_message(error: Exception) -> str:
        return error.message if isinstance(error, AppError) else str(error)
