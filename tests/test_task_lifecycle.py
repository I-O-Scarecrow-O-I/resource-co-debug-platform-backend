import asyncio
from uuid import uuid4

import pytest
from fastapi import FastAPI

from app.core.errors import AppError
from app.core.time import utc_now
from app.main import lifespan
from app.platform.api.deps import clear_task_service_cache, get_task_service
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.schemas.tasks import BuildTaskRequest
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore


def _service(task_store: TaskStore | None = None) -> TaskService:
    return TaskService(
        workspace_service=None,
        task_store=task_store or TaskStore(),
        log_service=None,
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


@pytest.mark.asyncio
async def test_shutdown_waits_for_short_background_task() -> None:
    service = _service()
    service._start_background(uuid4(), lambda: asyncio.sleep(0.05))

    await service.shutdown(grace_seconds=1)

    assert service._lifecycle_state == "CLOSED"
    assert not service._background_futures


@pytest.mark.asyncio
async def test_fastapi_lifespan_owns_task_service(monkeypatch) -> None:
    events: list[str] = []

    class FakeTaskService:
        async def startup(self) -> None:
            events.append("startup")

        async def shutdown(self) -> None:
            events.append("shutdown")

    monkeypatch.setattr("app.main.get_task_service", lambda: FakeTaskService())

    async with lifespan(FastAPI()):
        events.append("running")

    assert events == ["startup", "running", "shutdown"]


@pytest.mark.asyncio
async def test_lifespan_recreates_cached_task_service_after_shutdown() -> None:
    clear_task_service_cache()
    try:
        async with lifespan(FastAPI()):
            first_service = get_task_service()

        async with lifespan(FastAPI()):
            second_service = get_task_service()

        assert first_service._lifecycle_state == "CLOSED"
        assert second_service is not first_service
    finally:
        clear_task_service_cache()


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
        await service.shutdown(grace_seconds=0)
        clear_task_service_cache()
