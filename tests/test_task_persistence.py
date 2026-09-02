from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore


def _task() -> TaskRecord:
    created_at = datetime(2026, 9, 2, 12, 34, 56, tzinfo=UTC)
    return TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CO_DEBUG,
        project_id=uuid4(),
        task_type=TaskType.BUILD,
        status=TaskStatus.PENDING,
        command=["python", "-c", "print('ok')"],
        created_at=created_at,
        result={"success": True, "items": [1, 2]},
        metadata={"owner": "test"},
    )


def test_task_store_round_trips_task_through_sqlite(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    original = _task()
    original.started_at = datetime(2026, 9, 2, 12, 35, tzinfo=UTC)
    original.finished_at = datetime(2026, 9, 2, 12, 36, tzinfo=UTC)
    original.status = TaskStatus.FAILED
    original.exit_code = 1
    original.elapsed_ms = 123
    original.progress = 80
    original.error = "command failed"
    original.cancel_requested = True

    TaskStore(database_path).save(original)
    restored = TaskStore(database_path).require(original.id)

    assert restored == original
    assert restored.module is BackendModuleName.CO_DEBUG
    assert restored.task_type is TaskType.BUILD
    assert restored.created_at.tzinfo is not None


def test_task_store_try_start_has_one_concurrent_winner() -> None:
    store = TaskStore()
    task = _task()
    store.save(task)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: store.try_start(task.id), range(2)))

    assert sum(result is not None for result in results) == 1
    assert store.require(task.id).status == TaskStatus.RUNNING


def test_task_store_try_start_has_one_winner_across_instances(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    first = TaskStore(database_path)
    second = TaskStore(database_path)
    task = _task()
    first.save(task)

    assert (first.try_start(task.id) is not None) != (second.try_start(task.id) is not None)


def test_stale_save_does_not_overwrite_newer_revision(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    first = TaskStore(database_path)
    second = TaskStore(database_path)
    task = _task()
    first.save(task)
    stale = second.require(task.id)

    task.status = TaskStatus.SUCCEEDED
    first.save(task)
    stale.status = TaskStatus.FAILED
    result = second.save(stale)

    assert result.status == TaskStatus.SUCCEEDED
    assert second.require(task.id).status == TaskStatus.SUCCEEDED


def test_task_store_close_is_idempotent() -> None:
    store = TaskStore()
    store.close()
    store.close()

    with pytest.raises(RuntimeError, match="task store is closed"):
        store.list()


def test_default_task_database_path_follows_storage_root(tmp_path) -> None:
    storage_root = tmp_path / "workspaces"

    settings = Settings(storage_root=storage_root, task_database_path=None)

    assert settings.task_database_path == tmp_path / "tasks.sqlite3"


def test_task_store_recovers_only_interrupted_tasks(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    pending = _task()
    running = _task()
    succeeded = _task()
    cancelled = _task()
    running.status = TaskStatus.RUNNING
    running.started_at = datetime(2026, 9, 2, 12, 35, tzinfo=UTC)
    succeeded.status = TaskStatus.SUCCEEDED
    succeeded.finished_at = datetime(2026, 9, 2, 12, 36, tzinfo=UTC)
    cancelled.status = TaskStatus.CANCELLED
    cancelled.finished_at = datetime(2026, 9, 2, 12, 37, tzinfo=UTC)

    store = TaskStore(database_path)
    for task in [pending, running, succeeded, cancelled]:
        store.save(task)

    recovered_store = TaskStore(database_path)
    recovered = recovered_store.recover_interrupted_tasks()

    assert {task.id for task in recovered} == {pending.id, running.id}
    for task in [pending, running]:
        restored = recovered_store.require(task.id)
        assert restored.status == TaskStatus.FAILED
        assert restored.error == "interrupted by process restart"
        assert restored.finished_at is not None
    assert recovered_store.require(succeeded.id) == succeeded
    assert recovered_store.require(cancelled.id) == cancelled


def _service(task_store: TaskStore) -> TaskService:
    return TaskService(
        workspace_service=None,
        task_store=task_store,
        log_service=None,
        process_runner=None,
        scheduler_service=None,
        schedule_execution_service=None,
        schedule_comparison_service=None,
        default_timeout_seconds=10,
    )


@pytest.mark.asyncio
async def test_task_service_startup_recovers_interrupted_tasks() -> None:
    store = TaskStore()
    task = _task()
    store.save(task)
    service = _service(store)

    await service.startup()

    restored = store.require(task.id)
    assert restored.status == TaskStatus.FAILED
    assert restored.error == "interrupted by process restart"
    assert restored.finished_at is not None

    await service.shutdown(grace_seconds=0)
