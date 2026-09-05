import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi import UploadFile

from app.core.config import Settings
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.services.log_service import TaskLogService
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


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


@pytest.mark.parametrize("status", [TaskStatus.SUCCEEDED, TaskStatus.FAILED])
def test_cancel_request_wins_over_stale_worker_finalization(tmp_path, status: TaskStatus) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    worker_store = TaskStore(database_path)
    canceller_store = TaskStore(database_path)
    task = _task()
    worker_store.save(task)

    stale_worker_task = worker_store.try_start(task.id)
    assert stale_worker_task is not None
    canceller_store.request_cancel(task.id)
    stale_worker_task.status = status
    stale_worker_task.finished_at = datetime(2026, 9, 2, 12, 36, tzinfo=UTC)
    stale_worker_task.error = "command failed" if status == TaskStatus.FAILED else None

    finalized = worker_store.finalize(stale_worker_task)

    assert finalized.status == TaskStatus.CANCELLED
    assert finalized.error == "cancelled"
    assert finalized.finished_at is not None
    assert canceller_store.require(task.id).status == TaskStatus.CANCELLED


def test_pending_cancel_writes_terminal_fields_and_late_cancel_keeps_terminal(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    store = TaskStore(database_path)
    pending = _task()
    store.save(pending)

    cancelled = store.request_cancel(pending.id)

    assert cancelled.status == TaskStatus.CANCELLED
    assert cancelled.error == "cancelled"
    assert cancelled.finished_at is not None

    for status in [TaskStatus.SUCCEEDED, TaskStatus.FAILED]:
        task = _task()
        store.save(task)
        running = store.try_start(task.id)
        assert running is not None
        running.status = status
        running.finished_at = datetime(2026, 9, 2, 12, 36, tzinfo=UTC)
        if status == TaskStatus.FAILED:
            running.error = "command failed"
        finalized = store.finalize(running)

        assert store.request_cancel(task.id) == finalized


def test_progress_updates_are_atomic_monotonic_and_do_not_overwrite_cancellation(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    first = TaskStore(database_path)
    second = TaskStore(database_path)
    task = _task()
    first.save(task)
    assert first.try_start(task.id) is not None

    assert first.update_progress(task.id, 40).progress == 40
    assert second.update_progress(task.id, 10).progress == 40
    assert second.require(task.id).progress == 40

    cancelled = second.request_cancel(task.id)
    assert first.update_progress(task.id, 80) == cancelled
    assert first.require(task.id).progress == 40

    with pytest.raises(ValueError, match="between 0 and 100"):
        first.update_progress(task.id, 101)


def test_cancelled_finalization_preserves_progress_at_cancel_request(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    worker_store = TaskStore(database_path)
    canceller_store = TaskStore(database_path)
    task = _task()
    worker_store.save(task)
    worker_task = worker_store.try_start(task.id)
    assert worker_task is not None
    worker_store.update_progress(task.id, 40)

    canceller_store.request_cancel(task.id)
    worker_task.status = TaskStatus.SUCCEEDED
    worker_task.progress = 100
    worker_task.finished_at = datetime(2026, 9, 2, 12, 36, tzinfo=UTC)

    finalized = worker_store.finalize(worker_task)

    assert finalized.status == TaskStatus.CANCELLED
    assert finalized.progress == 40


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


def test_restart_recovers_cancel_requested_running_task_as_cancelled(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    worker_store = TaskStore(database_path)
    canceller_store = TaskStore(database_path)
    task = _task()
    worker_store.save(task)
    assert worker_store.try_start(task.id) is not None
    canceller_store.request_cancel(task.id)

    restarted_store = TaskStore(database_path)
    recovered = restarted_store.recover_interrupted_tasks()

    assert [item.id for item in recovered] == [task.id]
    restored = restarted_store.require(task.id)
    assert restored.status == TaskStatus.CANCELLED
    assert restored.error == "cancelled"
    assert restored.finished_at is not None


def test_recovery_and_cancel_race_never_leaves_active_cancelled_task(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    worker_store = TaskStore(database_path)
    canceller_store = TaskStore(database_path)
    task = _task()
    worker_store.save(task)
    assert worker_store.try_start(task.id) is not None

    start = Barrier(2)

    def recover() -> None:
        start.wait()
        worker_store.recover_interrupted_tasks()

    def cancel() -> None:
        start.wait()
        canceller_store.request_cancel_with_transition(task.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery = executor.submit(recover)
        cancellation = executor.submit(cancel)
        recovery.result()
        cancellation.result()

    restored = TaskStore(database_path).require(task.id)
    assert not (
        restored.status == TaskStatus.RUNNING and restored.cancel_requested
    )
    assert restored.status in {TaskStatus.CANCELLED, TaskStatus.FAILED}


def _service(task_store: TaskStore) -> TaskService:
    return TaskService(
        workspace_service=None,
        task_store=task_store,
        log_service=TaskLogService(max_lines=10),
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
    assert (
        service.log_service.history(task.id)[-1].message
        == "task failed: interrupted by process restart"
    )

    await service.shutdown(grace_seconds=0)


@pytest.mark.asyncio
async def test_task_service_startup_cleans_recovered_workspaces_and_continues_failures(
    tmp_path, monkeypatch, caplog
) -> None:
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("source.txt", "source")
    archive.seek(0)
    project = await workspace_service.create_from_archive(
        UploadFile(file=archive, filename="project.zip")
    )
    store = TaskStore(tmp_path / "tasks.sqlite3")
    cleaned = _task()
    cleaned.project_id = project.id
    cleanup_failure = _task()
    cleanup_failure.project_id = project.id
    missing_project = _task()
    for task in (cleaned, cleanup_failure, missing_project):
        store.save(task)
        (project.root_path / "tasks" / str(task.id) / "workspace").mkdir(parents=True)

    service = _service(store)
    service.workspace_service = workspace_service
    original_cleanup = workspace_service.cleanup_task_workspaces

    def fail_one_cleanup(project_id, task_id) -> None:
        if task_id == cleanup_failure.id:
            raise OSError("cleanup failed")
        original_cleanup(project_id, task_id)

    monkeypatch.setattr(workspace_service, "cleanup_task_workspaces", fail_one_cleanup)
    original_append = service.log_service.append

    def fail_one_log(task_id, *args, **kwargs):
        if task_id == cleaned.id:
            raise OSError("log failed")
        return original_append(task_id, *args, **kwargs)

    monkeypatch.setattr(service.log_service, "append", fail_one_log)

    with caplog.at_level("WARNING"):
        await service.startup()

    assert not (project.root_path / "tasks" / str(cleaned.id)).exists()
    assert (project.root_path / "tasks" / str(cleanup_failure.id)).exists()
    assert "cleanup failed" in caplog.text
    assert str(missing_project.id) in caplog.text
    assert "project not found" in caplog.text
    assert "log failed" in caplog.text
    assert store.require(cleaned.id).status == TaskStatus.FAILED
    assert store.require(cleanup_failure.id).status == TaskStatus.FAILED
    assert store.require(missing_project.id).status == TaskStatus.FAILED

    await service.shutdown(grace_seconds=0)
