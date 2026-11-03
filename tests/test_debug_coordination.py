import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import AppError
from app.core.time import utc_now
from app.modules.co_debug.coordination.models import (
    DebugCoordinationEventOutcome,
    DebugCoordinationGroupStatus,
    DebugGroupCommandOutcome,
)
from app.modules.co_debug.debug.events import (
    DebugSessionEvent,
    DebugSessionEventBus,
    DebugSessionEventKind,
)
from app.modules.co_debug.schemas.coordination import (
    DebugCoordinationGroupRequest,
    DebugCoordinationRuleRequest,
    DebugLogicalPartitionRequest,
)
from app.modules.co_debug.schemas.debug import DebugSessionStateResponse
from app.modules.co_debug.services.debug_coordination_service import (
    DebugCoordinationService,
)
from app.modules.co_debug.services.debug_coordination_store import (
    DebugCoordinationStore,
)
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.services.task_store import TaskStore


class FakeSessionManager:
    def __init__(self, active_ids: set[UUID]) -> None:
        self.active_ids = active_ids

    def contains(self, task_id: UUID) -> bool:
        return task_id in self.active_ids


class FakeDebugService:
    def __init__(self, states: dict[UUID, DebugSessionStateResponse]) -> None:
        self.states = states
        self.calls: list[tuple[str, UUID]] = []
        self.failures: set[tuple[str, UUID]] = set()
        self.interrupt_without_stop: set[UUID] = set()
        self.wait_failures: set[UUID] = set()
        self.in_flight = 0
        self.max_in_flight = 0

    async def state(self, task_id: UUID) -> DebugSessionStateResponse:
        return self.states[task_id]

    async def run(self, task_id: UUID) -> DebugSessionStateResponse:
        return await self._command("run", task_id, "RUNNING", active=True)

    async def continue_execution(self, task_id: UUID) -> DebugSessionStateResponse:
        return await self._command("continue", task_id, "RUNNING", active=True)

    async def interrupt(self, task_id: UUID) -> DebugSessionStateResponse:
        return await self._command(
            "interrupt",
            task_id,
            "RUNNING" if task_id in self.interrupt_without_stop else "STOPPED",
            active=True,
        )

    async def close(self, task_id: UUID) -> DebugSessionStateResponse:
        return await self._command("close_sessions", task_id, "EXITED", active=False)

    async def wait_for_stop(
        self,
        task_id: UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> DebugSessionStateResponse:
        del timeout_seconds
        if task_id in self.wait_failures:
            raise TimeoutError("target did not stop")
        return self.states[task_id]

    async def _command(
        self,
        name: str,
        task_id: UUID,
        state: str,
        *,
        active: bool,
    ) -> DebugSessionStateResponse:
        self.calls.append((name, task_id))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            if (name, task_id) in self.failures:
                raise AppError(f"{name} failed for {task_id}")
            current = self.states[task_id]
            updated = current.model_copy(update={"state": state, "active": active})
            self.states[task_id] = updated
            return updated
        finally:
            self.in_flight -= 1


def _task(project_id: UUID, *, task_type: TaskType = TaskType.DEBUG) -> TaskRecord:
    return TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CO_DEBUG,
        project_id=project_id,
        task_type=task_type,
        status=TaskStatus.RUNNING,
        command=["gdb"],
        created_at=datetime.now(tz=UTC),
    )


def _state(task: TaskRecord, state: str = "READY", *, active: bool = True):
    return DebugSessionStateResponse(
        task_id=task.id,
        task_status=task.status,
        active=active,
        state=state,
    )


def _request(
    project_id: UUID,
    first: TaskRecord,
    second: TaskRecord,
    *,
    name: str = "producer-consumer",
) -> DebugCoordinationGroupRequest:
    return DebugCoordinationGroupRequest(
        project_id=project_id,
        name=name,
        members=[
            DebugLogicalPartitionRequest(
                partition_name="producer",
                session_task_id=first.id,
            ),
            DebugLogicalPartitionRequest(
                partition_name="consumer",
                session_task_id=second.id,
            ),
        ],
    )


def _services(
    tmp_path,
    tasks: list[TaskRecord],
    *,
    event_bus: DebugSessionEventBus | None = None,
):
    database_path = tmp_path / "tasks.sqlite3"
    task_store = TaskStore(database_path)
    for task in tasks:
        task_store.save(task)
    states = {task.id: _state(task) for task in tasks if task.task_type == TaskType.DEBUG}
    manager = FakeSessionManager(set(states))
    debug_service = FakeDebugService(states)
    coordination_store = DebugCoordinationStore(database_path)
    service = DebugCoordinationService(
        store=coordination_store,
        task_store=task_store,
        session_manager=manager,
        debug_service=debug_service,
        event_bus=event_bus,
    )
    return service, coordination_store, task_store, manager, debug_service


def test_coordination_group_requires_two_unique_members() -> None:
    project_id = uuid4()
    session_id = uuid4()

    with pytest.raises(ValidationError):
        DebugCoordinationGroupRequest(
            project_id=project_id,
            name="too-small",
            members=[
                DebugLogicalPartitionRequest(
                    partition_name="only",
                    session_task_id=session_id,
                )
            ],
        )

    with pytest.raises(ValidationError, match="session task IDs must be unique"):
        DebugCoordinationGroupRequest(
            project_id=project_id,
            name="duplicate",
            members=[
                DebugLogicalPartitionRequest(
                    partition_name="first",
                    session_task_id=session_id,
                ),
                DebugLogicalPartitionRequest(
                    partition_name="second",
                    session_task_id=session_id,
                ),
            ],
        )


@pytest.mark.asyncio
async def test_coordination_group_persists_and_prevents_active_session_reuse(tmp_path) -> None:
    project_id = uuid4()
    first = _task(project_id)
    second = _task(project_id)
    third = _task(project_id)
    service, store, task_store, _, _ = _services(tmp_path, [first, second, third])

    created = await service.create(_request(project_id, first, second))

    assert created.status == DebugCoordinationGroupStatus.ACTIVE
    assert created.active_member_count == 2
    assert {member.partition_name for member in created.members} == {"producer", "consumer"}
    store.close()

    reopened = DebugCoordinationStore(tmp_path / "tasks.sqlite3")
    try:
        restored = reopened.require(created.group_id)
        assert restored.project_id == project_id
        assert len(reopened.list_members(created.group_id)) == 2
        reopened_service = DebugCoordinationService(
            store=reopened,
            task_store=task_store,
            session_manager=FakeSessionManager({first.id, second.id, third.id}),
            debug_service=FakeDebugService(
                {task.id: _state(task) for task in (first, second, third)}
            ),
        )
        with pytest.raises(AppError, match="already belong"):
            await reopened_service.create(_request(project_id, first, third, name="second-group"))
    finally:
        reopened.close()
        task_store.close()


@pytest.mark.asyncio
async def test_group_creation_requires_same_project_active_debug_sessions(tmp_path) -> None:
    project_id = uuid4()
    first = _task(project_id)
    other_project = _task(uuid4())
    build = _task(project_id, task_type=TaskType.BUILD)
    service, store, task_store, manager, _ = _services(tmp_path, [first, other_project, build])
    try:
        with pytest.raises(AppError, match="same project"):
            await service.create(_request(project_id, first, other_project))

        with pytest.raises(AppError, match="DEBUG tasks"):
            await service.create(_request(project_id, first, build))

        second = _task(project_id)
        task_store.save(second)
        service.debug_service.states[second.id] = _state(second)
        manager.active_ids.discard(second.id)
        with pytest.raises(AppError, match="active DEBUG sessions"):
            await service.create(_request(project_id, first, second))
    finally:
        store.close()
        task_store.close()


@pytest.mark.asyncio
async def test_group_commands_are_concurrent_and_report_independent_results(tmp_path) -> None:
    project_id = uuid4()
    first = _task(project_id)
    second = _task(project_id)
    service, store, task_store, _, debug = _services(tmp_path, [first, second])
    try:
        group = await service.create(_request(project_id, first, second))

        run_result = await service.run(group.group_id)

        assert run_result.all_succeeded is True
        assert {result.outcome for result in run_result.results} == {
            DebugGroupCommandOutcome.SUCCEEDED
        }
        assert debug.max_in_flight == 2
        debug.states[first.id] = _state(first, "STOPPED")
        debug.states[second.id] = _state(second, "STOPPED")

        continue_result = await service.continue_execution(group.group_id)

        assert continue_result.all_succeeded is True
        debug.failures.add(("interrupt", second.id))

        interrupt_result = await service.interrupt(group.group_id)

        by_task = {result.session_task_id: result for result in interrupt_result.results}
        assert interrupt_result.all_succeeded is False
        assert by_task[first.id].outcome == DebugGroupCommandOutcome.SUCCEEDED
        assert by_task[second.id].outcome == DebugGroupCommandOutcome.FAILED
        assert "interrupt failed" in (by_task[second.id].error or "")
    finally:
        store.close()
        task_store.close()


@pytest.mark.asyncio
async def test_ineligible_member_is_skipped_and_group_becomes_degraded(tmp_path) -> None:
    project_id = uuid4()
    first = _task(project_id)
    second = _task(project_id)
    service, store, task_store, _, debug = _services(tmp_path, [first, second])
    try:
        group = await service.create(_request(project_id, first, second))
        debug.states[second.id] = _state(second, "STOPPED", active=True).model_copy(
            update={"task_status": TaskStatus.FAILED}
        )

        result = await service.run(group.group_id)
        refreshed = await service.get(group.group_id)

        by_task = {item.session_task_id: item for item in result.results}
        assert by_task[first.id].outcome == DebugGroupCommandOutcome.SUCCEEDED
        assert by_task[second.id].outcome == DebugGroupCommandOutcome.SKIPPED
        assert refreshed.status == DebugCoordinationGroupStatus.DEGRADED
        assert refreshed.active_member_count == 1
    finally:
        store.close()
        task_store.close()


@pytest.mark.asyncio
async def test_remove_and_close_group_do_not_close_member_sessions(tmp_path) -> None:
    project_id = uuid4()
    first = _task(project_id)
    second = _task(project_id)
    service, store, task_store, _, debug = _services(tmp_path, [first, second])
    try:
        group = await service.create(_request(project_id, first, second))
        removed = await service.remove_member(group.group_id, group.members[0].partition_id)

        assert removed.status == DebugCoordinationGroupStatus.DEGRADED
        assert debug.calls == []

        closed = await service.close_group(group.group_id)

        assert closed.status == DebugCoordinationGroupStatus.CLOSED
        assert closed.active_member_count == 0
        assert debug.calls == []
        recreated = await service.create(_request(project_id, first, second, name="recreated"))
        assert recreated.status == DebugCoordinationGroupStatus.ACTIVE
    finally:
        store.close()
        task_store.close()


@pytest.mark.asyncio
async def test_close_sessions_is_explicit_and_does_not_close_group(tmp_path) -> None:
    project_id = uuid4()
    first = _task(project_id)
    second = _task(project_id)
    service, store, task_store, _, debug = _services(tmp_path, [first, second])
    try:
        group = await service.create(_request(project_id, first, second))

        result = await service.close_sessions(group.group_id)
        record = store.require(group.group_id)
        refreshed = await service.get(group.group_id)

        assert result.all_succeeded is True
        assert {call[0] for call in debug.calls} == {"close_sessions"}
        assert record.closed_at is None
        assert refreshed.status == DebugCoordinationGroupStatus.DEGRADED
    finally:
        store.close()
        task_store.close()


async def _wait_for_events(
    service: DebugCoordinationService,
    group_id: UUID,
    count: int,
) -> list:
    async with asyncio.timeout(1):
        while True:
            events = await service.list_events(group_id)
            if len(events) >= count and all(
                event.outcome != DebugCoordinationEventOutcome.PROCESSING for event in events
            ):
                return events
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_breakpoint_rule_interrupts_target_and_deduplicates_event(tmp_path) -> None:
    project_id = uuid4()
    source = _task(project_id)
    target = _task(project_id)
    event_bus = DebugSessionEventBus()
    service, store, task_store, _, debug = _services(
        tmp_path,
        [source, target],
        event_bus=event_bus,
    )
    try:
        group = await service.create(_request(project_id, source, target))
        source_partition = next(item for item in group.members if item.session_task_id == source.id)
        target_partition = next(item for item in group.members if item.session_task_id == target.id)
        debug.states[source.id] = debug.states[source.id].model_copy(
            update={"breakpoints": [{"number": "7", "location": "main.c:9"}]}
        )
        debug.states[target.id] = _state(target, "RUNNING")
        rule = await service.create_rule(
            group.group_id,
            DebugCoordinationRuleRequest(
                source_partition_id=source_partition.partition_id,
                source_breakpoint_number="7",
                target_partition_id=target_partition.partition_id,
            ),
        )
        assert [item.rule_id for item in await service.list_rules(group.group_id)] == [rule.rule_id]
        with pytest.raises(AppError, match="equivalent active coordination rule"):
            await service.create_rule(
                group.group_id,
                DebugCoordinationRuleRequest(
                    source_partition_id=source_partition.partition_id,
                    source_breakpoint_number="7",
                    target_partition_id=target_partition.partition_id,
                ),
            )

        source_event = DebugSessionEvent(
            event_id=uuid4(),
            session_task_id=source.id,
            kind=DebugSessionEventKind.BREAKPOINT_HIT,
            occurred_at=utc_now(),
            breakpoint_number="7",
        )
        event_bus.publish(source_event)
        event_bus.publish(source_event)

        events = await _wait_for_events(service, group.group_id, 1)
        assert len(events) == 1
        assert events[0].outcome == DebugCoordinationEventOutcome.INTERRUPT_CONFIRMED
        assert events[0].command_sent is True
        assert events[0].target_paused is True
        assert debug.calls.count(("interrupt", target.id)) == 1

        await service.delete_rule(group.group_id, rule.rule_id)
        assert await service.list_rules(group.group_id) == []
        reopened = DebugCoordinationStore(tmp_path / "tasks.sqlite3")
        try:
            assert reopened.list_rules(group.group_id) == []
            assert len(reopened.list_events(group.group_id)) == 1
        finally:
            reopened.close()
    finally:
        service.close()
        store.close()
        task_store.close()


@pytest.mark.asyncio
async def test_breakpoint_rule_records_skipped_target_after_member_removal(tmp_path) -> None:
    project_id = uuid4()
    source = _task(project_id)
    target = _task(project_id)
    event_bus = DebugSessionEventBus()
    service, store, task_store, _, debug = _services(
        tmp_path,
        [source, target],
        event_bus=event_bus,
    )
    try:
        group = await service.create(_request(project_id, source, target))
        source_partition = next(item for item in group.members if item.session_task_id == source.id)
        target_partition = next(item for item in group.members if item.session_task_id == target.id)
        debug.states[source.id] = debug.states[source.id].model_copy(
            update={"breakpoints": [{"number": "1"}]}
        )
        await service.create_rule(
            group.group_id,
            DebugCoordinationRuleRequest(
                source_partition_id=source_partition.partition_id,
                source_breakpoint_number="1",
                target_partition_id=target_partition.partition_id,
            ),
        )
        await service.remove_member(group.group_id, target_partition.partition_id)

        event_bus.publish(
            DebugSessionEvent(
                event_id=uuid4(),
                session_task_id=source.id,
                kind=DebugSessionEventKind.BREAKPOINT_HIT,
                occurred_at=utc_now(),
                breakpoint_number="1",
            )
        )

        events = await _wait_for_events(service, group.group_id, 1)
        assert events[0].outcome == DebugCoordinationEventOutcome.SKIPPED
        assert events[0].command_sent is False
        assert events[0].target_paused is False
        assert "no longer associated" in events[0].message
        assert ("interrupt", target.id) not in debug.calls
    finally:
        service.close()
        store.close()
        task_store.close()


@pytest.mark.asyncio
async def test_breakpoint_rule_distinguishes_sent_from_confirmed_interrupt(tmp_path) -> None:
    project_id = uuid4()
    source = _task(project_id)
    target = _task(project_id)
    event_bus = DebugSessionEventBus()
    service, store, task_store, _, debug = _services(
        tmp_path,
        [source, target],
        event_bus=event_bus,
    )
    try:
        group = await service.create(_request(project_id, source, target))
        source_partition = next(item for item in group.members if item.session_task_id == source.id)
        target_partition = next(item for item in group.members if item.session_task_id == target.id)
        debug.states[source.id] = debug.states[source.id].model_copy(
            update={"breakpoints": [{"number": "3"}]}
        )
        debug.states[target.id] = _state(target, "RUNNING")
        debug.interrupt_without_stop.add(target.id)
        debug.wait_failures.add(target.id)
        await service.create_rule(
            group.group_id,
            DebugCoordinationRuleRequest(
                source_partition_id=source_partition.partition_id,
                source_breakpoint_number="3",
                target_partition_id=target_partition.partition_id,
            ),
        )

        event_bus.publish(
            DebugSessionEvent(
                event_id=uuid4(),
                session_task_id=source.id,
                kind=DebugSessionEventKind.BREAKPOINT_HIT,
                occurred_at=utc_now(),
                breakpoint_number="3",
            )
        )

        events = await _wait_for_events(service, group.group_id, 1)
        assert events[0].outcome == DebugCoordinationEventOutcome.INTERRUPT_SENT
        assert events[0].command_sent is True
        assert events[0].target_paused is False
    finally:
        service.close()
        store.close()
        task_store.close()


@pytest.mark.asyncio
async def test_breakpoint_rule_handles_stopped_exited_and_missing_targets(tmp_path) -> None:
    project_id = uuid4()
    source = _task(project_id)
    target = _task(project_id)
    event_bus = DebugSessionEventBus()
    service, store, task_store, _, debug = _services(
        tmp_path,
        [source, target],
        event_bus=event_bus,
    )
    try:
        group = await service.create(_request(project_id, source, target))
        source_partition = next(item for item in group.members if item.session_task_id == source.id)
        target_partition = next(item for item in group.members if item.session_task_id == target.id)
        debug.states[source.id] = debug.states[source.id].model_copy(
            update={"breakpoints": [{"number": "5"}]}
        )
        await service.create_rule(
            group.group_id,
            DebugCoordinationRuleRequest(
                source_partition_id=source_partition.partition_id,
                source_breakpoint_number="5",
                target_partition_id=target_partition.partition_id,
            ),
        )
        debug.states[target.id] = _state(target, "STOPPED")
        event_bus.publish(
            DebugSessionEvent(
                event_id=uuid4(),
                session_task_id=source.id,
                kind=DebugSessionEventKind.BREAKPOINT_HIT,
                occurred_at=utc_now(),
                breakpoint_number="5",
            )
        )
        first = await _wait_for_events(service, group.group_id, 1)
        assert first[0].outcome == DebugCoordinationEventOutcome.SKIPPED
        assert first[0].target_paused is True

        debug.states[target.id] = _state(target, "EXITED", active=False)
        event_bus.publish(
            DebugSessionEvent(
                event_id=uuid4(),
                session_task_id=source.id,
                kind=DebugSessionEventKind.BREAKPOINT_HIT,
                occurred_at=utc_now(),
                breakpoint_number="5",
            )
        )
        exited_events = await _wait_for_events(service, group.group_id, 2)
        assert any(
            item.outcome == DebugCoordinationEventOutcome.SKIPPED and "not active" in item.message
            for item in exited_events
        )

        del debug.states[target.id]
        event_bus.publish(
            DebugSessionEvent(
                event_id=uuid4(),
                session_task_id=source.id,
                kind=DebugSessionEventKind.BREAKPOINT_HIT,
                occurred_at=utc_now(),
                breakpoint_number="5",
            )
        )
        events = await _wait_for_events(service, group.group_id, 3)
        assert {item.outcome for item in events} == {
            DebugCoordinationEventOutcome.SKIPPED,
            DebugCoordinationEventOutcome.FAILED,
        }
        assert any("failed to read target state" in item.message for item in events)
    finally:
        service.close()
        store.close()
        task_store.close()
