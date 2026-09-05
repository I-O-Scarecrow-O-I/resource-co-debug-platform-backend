import asyncio
from concurrent.futures import Future
from uuid import uuid4

import pytest
from fastapi import FastAPI

from app.core.errors import AppError
from app.core.time import utc_now
from app.main import lifespan
from app.platform.api.deps import (
    clear_log_service_cache,
    clear_task_service_cache,
    clear_task_store_cache,
    get_task_service,
    get_task_store,
)
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.schemas.tasks import BuildTaskRequest
from app.platform.services.log_service import TaskLogService
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore


def _service(task_store: TaskStore | None = None) -> TaskService:
    return TaskService(
        workspace_service=None,
        task_store=task_store or TaskStore(),
        log_service=TaskLogService(max_lines=10),
        process_runner=None,
        scheduler_service=None,
        schedule_execution_service=None,
        schedule_comparison_service=None,
        default_timeout_seconds=10,
    )


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_rejects_new_tasks() -> None:
    service = _service()

    await service.shutdown(grace_seconds=0)
    await service.shutdown(grace_seconds=0)

    with pytest.raises(AppError, match="shutting down"):
        await service.create_build_task(BuildTaskRequest(project_id=uuid4()))


@pytest.mark.asyncio
async def test_shutdown_requests_cancellation_for_pending_and_running_tasks() -> None:
    store = TaskStore()
    pending = TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CO_DEBUG,
        project_id=uuid4(),
        task_type=TaskType.BUILD,
        status=TaskStatus.PENDING,
        command=["make"],
        created_at=utc_now(),
    )
    running = TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CO_DEBUG,
        project_id=uuid4(),
        task_type=TaskType.BUILD,
        status=TaskStatus.RUNNING,
        command=["make"],
        created_at=utc_now(),
    )
    store.save(pending)
    store.save(running)
    service = _service(store)

    await service.shutdown(grace_seconds=0)

    assert store.require(pending.id).status == TaskStatus.CANCELLED
    assert store.require(running.id).cancel_requested is True
    messages = [event.message for event in service.log_service.history(pending.id)]
    assert messages == ["task cancelled"]

    await service.shutdown(grace_seconds=0)
    assert [event.message for event in service.log_service.history(pending.id)] == messages


@pytest.mark.asyncio
async def test_cancel_task_returns_persisted_state_when_cancellation_log_fails(caplog) -> None:
    store = TaskStore()
    task = TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CO_DEBUG,
        project_id=uuid4(),
        task_type=TaskType.BUILD,
        status=TaskStatus.PENDING,
        command=["make"],
        created_at=utc_now(),
    )
    store.save(task)
    service = _service(store)
    future: Future[None] = Future()
    service._background_futures[task.id] = future
    service.log_service.append = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("log failed")
    )

    with caplog.at_level("WARNING"):
        cancelled = await service.cancel_task(task.id)

    assert cancelled.status == TaskStatus.CANCELLED
    assert store.require(task.id).status == TaskStatus.CANCELLED
    assert future.cancelled()
    assert "log failed" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_continues_after_cancellation_log_failure(caplog) -> None:
    store = TaskStore()
    tasks = [
        TaskRecord(
            id=uuid4(),
            module=BackendModuleName.CO_DEBUG,
            project_id=uuid4(),
            task_type=TaskType.BUILD,
            status=TaskStatus.PENDING,
            command=["make"],
            created_at=utc_now(),
        )
        for _ in range(2)
    ]
    for task in tasks:
        store.save(task)
    service = _service(store)
    futures = {task.id: Future() for task in tasks}
    service._background_futures.update(futures)
    original_append = service.log_service.append

    def fail_first_terminal_log(task_id, *args, **kwargs):
        if task_id == tasks[0].id:
            raise OSError("log failed")
        return original_append(task_id, *args, **kwargs)

    service.log_service.append = fail_first_terminal_log

    with caplog.at_level("WARNING"):
        await service.shutdown(grace_seconds=0)

    assert [store.require(task.id).status for task in tasks] == [TaskStatus.CANCELLED] * 2
    assert all(future.cancelled() for future in futures.values())
    assert service._lifecycle_state == "CLOSED"
    assert "log failed" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_closes_executor_when_task_listing_fails(monkeypatch) -> None:
    service = _service()

    def fail_list() -> list[TaskRecord]:
        raise OSError("task store unavailable")

    monkeypatch.setattr(service.task_store, "list", fail_list)

    with pytest.raises(OSError, match="task store unavailable"):
        await service.shutdown(grace_seconds=0)

    assert service._lifecycle_state == "CLOSED"
    await service.shutdown(grace_seconds=0)


@pytest.mark.asyncio
async def test_shutdown_waits_for_short_background_task() -> None:
    service = _service()
    service._start_background(uuid4(), lambda: asyncio.sleep(0.05))

    await service.shutdown(grace_seconds=1)

    assert service._lifecycle_state == "CLOSED"
    assert not service._background_futures


@pytest.mark.asyncio
async def test_clearing_resource_caches_tracks_service_after_task_cache_is_cleared() -> None:
    clear_task_service_cache()
    clear_log_service_cache()
    clear_task_store_cache()
    service = get_task_service()
    service._start_background(uuid4(), lambda: asyncio.sleep(0.05))
    clear_task_service_cache()

    try:
        with pytest.raises(RuntimeError, match="must be shut down"):
            clear_log_service_cache()
        with pytest.raises(RuntimeError, match="must be shut down"):
            clear_task_store_cache()
        assert not service.log_service._closed
        assert not service.task_store._closed

        await service.shutdown(grace_seconds=1)
        service.close_resources_when_idle()
        clear_log_service_cache()
        clear_task_store_cache()
    finally:
        if service._lifecycle_state != "CLOSED":
            await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()
        clear_task_service_cache()
        clear_log_service_cache()
        clear_task_store_cache()


@pytest.mark.asyncio
async def test_fastapi_lifespan_owns_task_service(monkeypatch) -> None:
    events: list[str] = []

    class FakeTaskService:
        async def startup(self) -> None:
            events.append("startup")

        async def shutdown(self) -> None:
            events.append("shutdown")

        def close_resources_when_idle(self) -> None:
            pass

    monkeypatch.setattr("app.main.get_task_service", lambda: FakeTaskService())

    async with lifespan(FastAPI()):
        events.append("running")

    assert events == ["startup", "running", "shutdown"]


@pytest.mark.asyncio
async def test_lifespan_recreates_cached_task_service_after_shutdown() -> None:
    clear_task_service_cache()
    clear_log_service_cache()
    try:
        async with lifespan(FastAPI()):
            first_service = get_task_service()

        async with lifespan(FastAPI()):
            second_service = get_task_service()
            assert second_service is not first_service
            assert second_service.log_service is not first_service.log_service
            assert not second_service.log_service._closed
            assert second_service.scheduler_service.log_service is second_service.log_service
            assert (
                second_service.schedule_comparison_service.scheduler_service.log_service
                is second_service.log_service
            )

        assert first_service._lifecycle_state == "CLOSED"
        assert second_service.log_service._closed
    finally:
        clear_task_service_cache()
        clear_log_service_cache()


def test_clearing_log_cache_recreates_dependent_task_service() -> None:
    clear_task_service_cache()
    clear_log_service_cache()
    clear_task_store_cache()
    try:
        first_service = get_task_service()

        with pytest.raises(RuntimeError, match="must be shut down"):
            clear_log_service_cache()
        asyncio.run(first_service.shutdown(grace_seconds=0))
        clear_log_service_cache()

        second_service = get_task_service()
        assert second_service is not first_service
        assert second_service.log_service is not first_service.log_service
        assert not second_service.log_service._closed
        event = second_service.log_service.append(uuid4(), "log service is available")
        assert event.message == "log service is available"
        asyncio.run(second_service.shutdown(grace_seconds=0))
    finally:
        clear_task_service_cache()
        clear_log_service_cache()
        clear_task_store_cache()


def test_clearing_task_store_cache_recreates_dependent_task_service() -> None:
    clear_task_service_cache()
    clear_task_store_cache()
    try:
        first_service = get_task_service()
        first_store = first_service.task_store

        with pytest.raises(RuntimeError, match="must be shut down"):
            clear_task_store_cache()
        asyncio.run(first_service.shutdown(grace_seconds=0))
        clear_task_store_cache()

        second_service = get_task_service()
        assert second_service is not first_service
        assert second_service.task_store is not first_store
        assert not second_service.task_store._closed
        assert get_task_store() is second_service.task_store
        asyncio.run(second_service.shutdown(grace_seconds=0))
    finally:
        clear_task_service_cache()
        clear_task_store_cache()


def test_background_failure_is_logged(caplog) -> None:
    service = _service()
    task_id = uuid4()
    future: Future[None] = Future()
    service._background_futures[task_id] = future
    future.set_exception(RuntimeError("background failure"))

    with caplog.at_level("ERROR"):
        service._forget_background(task_id, future)

    assert str(task_id) in caplog.text
    assert "background failure" in caplog.text


def test_terminal_log_failure_does_not_skip_workspace_cleanup() -> None:
    store = TaskStore()
    task = TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CO_DEBUG,
        project_id=uuid4(),
        task_type=TaskType.BUILD,
        status=TaskStatus.PENDING,
        command=["make"],
        created_at=utc_now(),
    )
    store.save(task)
    running = store.try_start(task.id)
    assert running is not None
    running.status = TaskStatus.FAILED
    running.error = "command failed"

    service = _service(store)
    service.log_service.append = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("log write failed")
    )
    cleaned: list[TaskStatus] = []

    with pytest.raises(RuntimeError, match="log write failed"):
        service._finalize_with_cleanup(running, lambda finalized: cleaned.append(finalized.status))

    assert cleaned == [TaskStatus.FAILED]
    assert store.require(task.id).status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_lifespan_startup_failure_clears_task_service_cache(monkeypatch) -> None:
    clear_task_service_cache()
    service = get_task_service()

    async def fail_startup() -> None:
        raise RuntimeError("startup failed")

    monkeypatch.setattr(service, "startup", fail_startup)
    try:
        with pytest.raises(RuntimeError, match="startup failed"):
            async with lifespan(FastAPI()):
                pass

        assert get_task_service.cache_info().currsize == 0
    finally:
        if not service.task_store._closed:
            await service.shutdown(grace_seconds=0)
        clear_task_service_cache()
