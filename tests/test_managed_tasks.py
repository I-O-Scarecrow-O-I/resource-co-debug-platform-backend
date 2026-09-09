import asyncio
import io
import sqlite3
import threading
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi import UploadFile

from app.core.errors import AppError
from app.core.time import utc_now
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.services.log_service import TaskLogService
from app.platform.services.task_execution import ManagedTaskContext, ManagedTaskResult
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


def _service(tmp_path) -> tuple[TaskService, WorkspaceService]:
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    service = TaskService(
        workspace_service=workspace_service,
        task_store=TaskStore(tmp_path / "tasks.sqlite3"),
        log_service=TaskLogService(100, tmp_path / "logs.sqlite3"),
        process_runner=None,
        default_timeout_seconds=10,
    )
    return service, workspace_service


async def _create_project(workspace_service: WorkspaceService):
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("input.txt", "source")
    archive.seek(0)
    return await workspace_service.create_from_archive(
        UploadFile(file=archive, filename="project.zip")
    )


async def _wait_for_terminal(service: TaskService, task_id: UUID):
    for _ in range(200):
        task = service.require_task(task_id)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("managed task did not finish")


async def _wait_for_cleanup(task_root: Path) -> None:
    for _ in range(200):
        if not task_root.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"managed task workspace was not cleaned: {task_root}")


async def _wait_for_cleanup_pending(service: TaskService, task_id: UUID):
    for _ in range(200):
        if task_id in service.task_store.list_workspace_cleanup_pending():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("managed task cleanup failure was not persisted")


async def _close(service: TaskService) -> None:
    await service.shutdown(grace_seconds=1)
    service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_managed_task_reports_progress_and_cleans_multiple_workspaces(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    workspaces = []

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        assert context.project_id == project.id
        assert context.timeout_seconds == 3
        first = context.create_workspace("first")
        second = context.create_workspace("second")
        workspaces.extend([first, second])
        assert (first / "input.txt").read_text() == "source"
        assert (second / "input.txt").read_text() == "source"
        context.log("managed executor started", stream="managed.test")
        context.report_progress(45, "managed executor progressed")
        return ManagedTaskResult(
            status=TaskStatus.SUCCEEDED,
            result={"workspaces": 2},
            exit_code=0,
            elapsed_ms=12,
        )

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_COMPARISON,
            command=["managed", "comparison"],
            execute=execute,
            metadata={"source": "test"},
            timeout_seconds=3,
        )
        completed = await _wait_for_terminal(service, created.id)

        assert completed.status == TaskStatus.SUCCEEDED
        assert completed.command == ["managed", "comparison"]
        assert completed.metadata == {"source": "test"}
        assert completed.result == {"workspaces": 2}
        assert completed.exit_code == 0
        assert completed.elapsed_ms == 12
        assert completed.progress == 100
        assert completed.finished_at is not None
        messages = [event.message for event in service.log_service.history(created.id)]
        assert "managed executor started" in messages
        assert "managed executor progressed" in messages
        assert messages[-1] == "task succeeded"
        assert len(workspaces) == 2
        await _wait_for_cleanup(project.root_path / "tasks" / str(created.id))
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_managed_task_executor_exception_fails_and_cleans_workspace(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        context.create_workspace("failed")
        raise RuntimeError("executor failed")

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "failure"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, created.id)

        assert completed.status == TaskStatus.FAILED
        assert completed.error == "executor failed"
        assert completed.progress == 100
        assert completed.finished_at is not None
        await _wait_for_cleanup(project.root_path / "tasks" / str(created.id))
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_managed_task_uses_one_total_timeout_and_cleans_workspace(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        context.create_workspace("timed-out")
        await asyncio.sleep(1)
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "timeout"],
            execute=execute,
            total_timeout_seconds=0.02,
        )
        completed = await _wait_for_terminal(service, created.id)

        assert completed.status == TaskStatus.FAILED
        assert completed.error == "managed task timed out after 0.02 seconds"
        assert completed.progress == 100
        await _wait_for_cleanup(project.root_path / "tasks" / str(created.id))
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_running_managed_task_cooperatively_cancels_without_leaking_workspace(
    tmp_path,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    started = threading.Event()

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        context.create_workspace("cancelled")
        started.set()
        while not context.is_cancelled():
            await asyncio.sleep(0.01)
        context.raise_if_cancelled()
        raise AssertionError("raise_if_cancelled must stop execution")

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "cancel"],
            execute=execute,
        )
        assert await asyncio.to_thread(started.wait, 1)

        cancellation = await service.cancel_task(created.id)
        completed = await _wait_for_terminal(service, created.id)

        assert cancellation.cancel_requested is True
        assert completed.status == TaskStatus.CANCELLED
        assert completed.error == "cancelled"
        assert completed.progress < 100
        await _wait_for_cleanup(project.root_path / "tasks" / str(created.id))
    finally:
        await _close(service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned", "expected_error"),
    [
        (object(), "execute must return ManagedTaskResult"),
        (
            ManagedTaskResult(status=TaskStatus.RUNNING),
            "managed task result must have a terminal status",
        ),
    ],
)
async def test_managed_task_rejects_invalid_result(tmp_path, returned, expected_error) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)

    async def execute(_: ManagedTaskContext):
        return returned

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "invalid"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, created.id)

        assert completed.status == TaskStatus.FAILED
        assert completed.error == expected_error
        assert completed.progress == 100
    finally:
        await _close(service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned", "expected_error"),
    [
        (
            ManagedTaskResult(status=TaskStatus.SUCCEEDED, result=[]),
            "managed task result must be a dict",
        ),
        (
            ManagedTaskResult(status=TaskStatus.SUCCEEDED, result={"value": object()}),
            "managed task result must be JSON serializable",
        ),
        (
            ManagedTaskResult(status=TaskStatus.SUCCEEDED, exit_code=True),
            "managed task exit_code must be an int or None",
        ),
        (
            ManagedTaskResult(status=TaskStatus.SUCCEEDED, elapsed_ms=-1),
            "managed task elapsed_ms must be a non-negative int or None",
        ),
        (
            ManagedTaskResult(status=TaskStatus.SUCCEEDED, elapsed_ms=True),
            "managed task elapsed_ms must be a non-negative int or None",
        ),
        (
            ManagedTaskResult(status=TaskStatus.SUCCEEDED, error=1),
            "managed task error must be a string or None",
        ),
    ],
)
async def test_managed_task_rejects_unpersistable_result_fields(
    tmp_path,
    returned,
    expected_error,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)

    async def execute(_: ManagedTaskContext):
        return returned

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "invalid-fields"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, created.id)

        assert completed.status == TaskStatus.FAILED
        assert completed.error == expected_error
        assert completed.progress == 100
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_managed_workspace_cleanup_retries_then_succeeds(tmp_path, monkeypatch) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    original_cleanup = workspace_service.cleanup_task_workspaces
    attempts = 0

    def flaky_cleanup(project_id, task_id) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("cleanup failed once")
        original_cleanup(project_id, task_id)

    monkeypatch.setattr(workspace_service, "cleanup_task_workspaces", flaky_cleanup)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        context.create_workspace("retry-cleanup")
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "retry-cleanup"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, created.id)
        await _wait_for_cleanup(project.root_path / "tasks" / str(created.id))

        assert completed.status == TaskStatus.SUCCEEDED
        assert attempts == 2
        assert created.id not in service.task_store.list_workspace_cleanup_pending()
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_managed_cleanup_intent_is_persisted_after_finalization_before_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    original_finalize = service.task_store.finalize_with_transition
    original_cleanup = workspace_service.cleanup_task_workspaces
    observed = []

    def finalize(task, **kwargs):
        assert task.id not in service.task_store.list_workspace_cleanup_pending()
        observed.append("finalize")
        return original_finalize(task, **kwargs)

    def cleanup(project_id, task_id) -> None:
        assert task_id in service.task_store.list_workspace_cleanup_pending()
        observed.append("cleanup")
        original_cleanup(project_id, task_id)

    monkeypatch.setattr(service.task_store, "finalize_with_transition", finalize)
    monkeypatch.setattr(workspace_service, "cleanup_task_workspaces", cleanup)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        context.create_workspace("intent-before-finalize")
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "cleanup-intent"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, created.id)
        await _wait_for_cleanup(project.root_path / "tasks" / str(created.id))

        assert completed.status == TaskStatus.SUCCEEDED
        assert observed == ["finalize", "cleanup"]
        assert created.id not in service.task_store.list_workspace_cleanup_pending()
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_concurrent_managed_workspace_cleanup_is_idempotent(
    tmp_path,
    monkeypatch,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    task = service._new_task(
        module=BackendModuleName.CO_DEBUG,
        project_id=project.id,
        task_type=TaskType.SCHEDULE_EXPERIMENT,
        command=["managed", "concurrent-cleanup"],
        metadata={},
    )
    workspace = workspace_service.create_task_workspace(
        project.id,
        task.id,
        workspace_name="concurrent-cleanup",
    )
    service.task_store.mark_workspace_cleanup_pending(task.id)
    original_cleanup = workspace_service.cleanup_task_workspaces
    cleanup_ready = threading.Barrier(2)

    def synchronized_cleanup(project_id, task_id) -> None:
        cleanup_ready.wait(timeout=2)
        original_cleanup(project_id, task_id)

    monkeypatch.setattr(
        workspace_service,
        "cleanup_task_workspaces",
        synchronized_cleanup,
    )
    try:
        await asyncio.gather(
            asyncio.to_thread(
                service._cleanup_managed_task_workspaces,
                project.id,
                task.id,
            ),
            asyncio.to_thread(
                service._cleanup_managed_task_workspaces,
                project.id,
                task.id,
            ),
        )

        assert not workspace.parent.exists()
        assert task.id not in service.task_store.list_workspace_cleanup_pending()
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_cleanup_intent_failure_rolls_back_finalization_and_skips_cleanup(
    tmp_path,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("intent-failure")
        (workspace / "must-remain.txt").write_text("data", encoding="utf-8")
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        with sqlite3.connect(tmp_path / "tasks.sqlite3") as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_managed_cleanup_intent
                BEFORE INSERT ON task_workspace_cleanup
                BEGIN
                    SELECT RAISE(ABORT, 'cleanup intent failed');
                END
                """
            )
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "intent-failure"],
            execute=execute,
        )
        for _ in range(200):
            with service._background_lock:
                if created.id not in service._background_futures:
                    break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("managed finalization failure did not finish")

        task_root = project.root_path / "tasks" / str(created.id)
        assert service.require_task(created.id).status == TaskStatus.RUNNING
        assert service.task_store.list_workspace_cleanup_pending() == []
        assert (task_root / "intent-failure" / "must-remain.txt").is_file()
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_startup_recovers_persistently_failed_managed_workspace_cleanup(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    attempts = 0
    service_closed = False
    restored_service = None

    def fail_cleanup(_project_id, _task_id) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("cleanup remains unavailable")

    monkeypatch.setattr(workspace_service, "cleanup_task_workspaces", fail_cleanup)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        context.create_workspace("pending-cleanup")
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        with caplog.at_level("WARNING"):
            created = await service.create_managed_task(
                module=BackendModuleName.CO_DEBUG,
                project_id=project.id,
                task_type=TaskType.SCHEDULE_EXPERIMENT,
                command=["managed", "pending-cleanup"],
                execute=execute,
            )
            completed = await _wait_for_terminal(service, created.id)
            await _wait_for_cleanup_pending(service, created.id)
            for _ in range(100):
                if attempts == 3:
                    break
                await asyncio.sleep(0.01)

        task_root = project.root_path / "tasks" / str(created.id)
        assert completed.status == TaskStatus.SUCCEEDED
        assert created.id in service.task_store.list_workspace_cleanup_pending()
        assert attempts == 3
        assert task_root.exists()
        assert "failed to clean managed task workspace" in caplog.text

        await _close(service)
        service_closed = True
        restored_service, _ = _service(tmp_path)
        await restored_service.startup()

        restored = restored_service.require_task(created.id)
        assert restored.status == TaskStatus.SUCCEEDED
        assert created.id not in restored_service.task_store.list_workspace_cleanup_pending()
        assert not task_root.exists()
    finally:
        if not service_closed:
            await _close(service)
        if restored_service is not None:
            await _close(restored_service)


@pytest.mark.asyncio
async def test_startup_recovers_queue_when_cleanup_succeeds_but_completion_failed(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    service_closed = False
    restored_service = None

    def fail_completion(_task_id) -> None:
        raise OSError("cleanup queue unavailable")

    monkeypatch.setattr(
        service.task_store,
        "complete_workspace_cleanup",
        fail_completion,
    )

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        context.create_workspace("cleared-workspace")
        service.task_store.mark_workspace_cleanup_pending(context.task_id)
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        with caplog.at_level("WARNING"):
            created = await service.create_managed_task(
                module=BackendModuleName.CO_DEBUG,
                project_id=project.id,
                task_type=TaskType.SCHEDULE_EXPERIMENT,
                command=["managed", "clear-failure"],
                execute=execute,
            )
            completed = await _wait_for_terminal(service, created.id)
            task_root = project.root_path / "tasks" / str(created.id)
            await _wait_for_cleanup(task_root)

        assert completed.status == TaskStatus.SUCCEEDED
        assert created.id in service.task_store.list_workspace_cleanup_pending()
        assert "failed to complete managed workspace cleanup state" in caplog.text

        await _close(service)
        service_closed = True
        restored_service, _ = _service(tmp_path)
        await restored_service.startup()

        assert created.id not in restored_service.task_store.list_workspace_cleanup_pending()
        assert not task_root.exists()
    finally:
        if not service_closed:
            await _close(service)
        if restored_service is not None:
            await _close(restored_service)


@pytest.mark.asyncio
async def test_startup_ignores_public_metadata_cleanup_flag_for_ordinary_build(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    task = TaskRecord(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        module=BackendModuleName.CO_DEBUG,
        project_id=project.id,
        task_type=TaskType.BUILD,
        status=TaskStatus.SUCCEEDED,
        command=["build"],
        created_at=utc_now(),
        finished_at=utc_now(),
        metadata={"workspace_cleanup_pending": True},
    )
    service.task_store.save(task)
    workspace = workspace_service.create_task_workspace(
        project.id,
        task.id,
        workspace_name="workspace",
    )

    try:
        assert service.task_store.list_workspace_cleanup_pending() == []

        await service.startup()

        assert workspace.exists()
        assert service.task_store.list_workspace_cleanup_pending() == []
        assert service.require_task(task.id).metadata == {"workspace_cleanup_pending": True}
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_managed_context_passes_metadata_to_atomic_hold_and_release(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    observed: dict[str, object] = {}

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        context.create_workspace("metadata")
        context.hold_workspaces({"cleanup_pending": True})
        observed["held"] = service.task_store.workspaces_are_held(context.task_id)
        observed["held_metadata"] = service.require_task(context.task_id).metadata
        context.release_workspaces(
            completion_metadata={
                "cleanup_pending": False,
                "remote_terminal": True,
            }
        )
        observed["released"] = service.task_store.workspaces_are_held(context.task_id)
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "metadata"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, created.id)
        await _wait_for_cleanup(project.root_path / "tasks" / str(created.id))

        assert completed.status == TaskStatus.SUCCEEDED
        assert observed == {
            "held": True,
            "held_metadata": {"cleanup_pending": True},
            "released": False,
        }
        assert service.require_task(created.id).metadata == {
            "cleanup_pending": False,
            "remote_terminal": True,
        }
        assert created.id not in service.task_store.list_workspace_cleanup_pending()
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_cleanup_completion_metadata_waits_for_successful_startup_retry(
    tmp_path,
    monkeypatch,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    task = TaskRecord(
        id=UUID("00000000-0000-0000-0000-000000000004"),
        module=BackendModuleName.CO_DEBUG,
        project_id=project.id,
        task_type=TaskType.SCHEDULE_EXPERIMENT,
        status=TaskStatus.FAILED,
        command=["managed", "cleanup-metadata"],
        created_at=utc_now(),
        error="failed",
        metadata={"cleanup_pending": True},
    )
    service.task_store.save(task)
    workspace = workspace_service.create_task_workspace(
        project.id,
        task.id,
        workspace_name="cleanup-metadata",
    )
    service.hold_task_workspaces(task.id)

    def fail_cleanup(_project_id, _task_id) -> None:
        raise OSError("cleanup unavailable")

    monkeypatch.setattr(workspace_service, "cleanup_task_workspaces", fail_cleanup)
    service.release_task_workspaces(
        task.id,
        cleanup=True,
        completion_metadata={"cleanup_pending": False},
    )

    assert workspace.exists()
    assert service.task_store.workspaces_are_held(task.id) is False
    assert task.id in service.task_store.list_workspace_cleanup_pending()
    assert service.require_task(task.id).metadata["cleanup_pending"] is True

    await _close(service)
    restored_service, _ = _service(tmp_path)
    try:
        await restored_service.startup()

        assert not workspace.exists()
        assert task.id not in restored_service.task_store.list_workspace_cleanup_pending()
        assert restored_service.require_task(task.id).metadata["cleanup_pending"] is False
    finally:
        await _close(restored_service)


@pytest.mark.asyncio
async def test_held_failed_managed_task_keeps_workspace_until_explicit_release(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("held-failure")
        (workspace / "remote-state.txt").write_text("pending", encoding="utf-8")
        context.hold_workspaces()
        return ManagedTaskResult(status=TaskStatus.FAILED, error="remote state uncertain")

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "held-failure"],
            execute=execute,
        )
        completed = await _wait_for_terminal(service, created.id)
        task_root = project.root_path / "tasks" / str(created.id)

        assert completed.status == TaskStatus.FAILED
        assert task_root.exists()
        assert service.task_store.workspaces_are_held(created.id) is True
        assert created.id not in service.task_store.list_workspace_cleanup_pending()

        service.release_task_workspaces(created.id, cleanup=True)
        await _wait_for_cleanup(task_root)
        assert service.task_store.workspaces_are_held(created.id) is False
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_held_cancelled_managed_task_keeps_workspace_and_isolates_handler_error(
    tmp_path,
    caplog,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    started = threading.Event()
    cancellation_calls: list[tuple[UUID, float | None]] = []

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        context.create_workspace("held-cancel")
        context.hold_workspaces()
        started.set()
        while not context.is_cancelled():
            await asyncio.sleep(0.01)
        context.raise_if_cancelled()
        raise AssertionError("cancellation must stop execution")

    async def cancellation_handler(task_id: UUID, deadline: float | None) -> None:
        cancellation_calls.append((task_id, deadline))
        raise RuntimeError("remote cancellation failed")

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "held-cancel"],
            execute=execute,
            cancellation_handler=cancellation_handler,
        )
        assert await asyncio.to_thread(started.wait, 1)

        with caplog.at_level("WARNING"):
            cancelled = await service.cancel_task(created.id, cancel_deadline=123.0)
        completed = await _wait_for_terminal(service, created.id)
        for _ in range(100):
            if created.id not in service._managed_cancellation_handlers:
                break
            await asyncio.sleep(0.01)

        task_root = project.root_path / "tasks" / str(created.id)
        assert cancelled.cancel_requested is True
        assert completed.status == TaskStatus.CANCELLED
        assert cancellation_calls == [(created.id, 123.0)]
        assert "remote cancellation failed" in caplog.text
        assert task_root.exists()
        assert service.task_store.workspaces_are_held(created.id) is True
        assert created.id not in service.task_store.list_workspace_cleanup_pending()
        assert created.id not in service._managed_cancellation_handlers

        service.release_task_workspaces(created.id, cleanup=True)
        await _wait_for_cleanup(task_root)
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_successful_preserved_managed_task_releases_hold_and_keeps_artifact(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("preserved")
        (workspace / "artifact.txt").write_text("result", encoding="utf-8")
        context.hold_workspaces(
            {"remote_run_id": "run-1", "cleanup_pending": True}
        )
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED, result={"artifact": True})

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "preserve"],
            execute=execute,
            preserve_workspace_on_success=True,
            workspace_completion_metadata_on_success={"cleanup_pending": False},
        )
        completed = await _wait_for_terminal(service, created.id)
        artifact = (
            project.root_path / "tasks" / str(created.id) / "preserved" / "artifact.txt"
        )

        assert completed.status == TaskStatus.SUCCEEDED
        assert artifact.read_text(encoding="utf-8") == "result"
        for _ in range(200):
            if not service.task_store.workspaces_are_held(created.id):
                break
            await asyncio.sleep(0.01)
        assert service.require_task(created.id).metadata == {
            "remote_run_id": "run-1",
            "cleanup_pending": False,
        }
        assert service.task_store.workspaces_are_held(created.id) is False
        assert created.id not in service.task_store.list_workspace_cleanup_pending()
        with pytest.raises(AppError, match="artifacts are not available"):
            service.list_task_artifacts(created.id)
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_cancel_during_managed_finalization_uses_actual_status_for_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    original_finalize = service.task_store.finalize_with_transition

    def cancel_then_finalize(task, **kwargs):
        service.task_store.request_cancel(task.id)
        return original_finalize(task, **kwargs)

    monkeypatch.setattr(
        service.task_store,
        "finalize_with_transition",
        cancel_then_finalize,
    )

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        workspace = context.create_workspace("cancel-race")
        (workspace / "artifact.txt").write_text("must be removed", encoding="utf-8")
        context.hold_workspaces({"cleanup_pending": True})
        context.release_workspaces(
            completion_metadata={"remote_terminal_confirmed": True},
        )
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED, result={"artifact": True})

    try:
        created = await service.create_managed_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["managed", "cancel-race"],
            execute=execute,
            preserve_workspace_on_success=True,
            workspace_completion_metadata_on_success={"cleanup_pending": False},
            artifacts_on_success=True,
        )
        completed = await _wait_for_terminal(service, created.id)
        task_root = project.root_path / "tasks" / str(created.id)
        await _wait_for_cleanup(task_root)

        assert completed.status == TaskStatus.CANCELLED
        assert service.task_store.workspaces_are_held(created.id) is False
        assert created.id not in service.task_store.list_workspace_cleanup_pending()
        assert service.require_task(created.id).metadata == {
            "cleanup_pending": True,
            "remote_terminal_confirmed": True,
        }
        assert service.task_store.artifacts_are_available(created.id) is False
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_startup_does_not_delete_held_interrupted_workspace(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    task = TaskRecord(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        module=BackendModuleName.CO_DEBUG,
        project_id=project.id,
        task_type=TaskType.SCHEDULE_EXPERIMENT,
        status=TaskStatus.RUNNING,
        command=["managed", "interrupted"],
        created_at=utc_now(),
    )
    service.task_store.save(task)
    workspace = workspace_service.create_task_workspace(
        project.id,
        task.id,
        workspace_name="held",
    )
    service.hold_task_workspaces(task.id)

    try:
        await service.startup()

        assert service.require_task(task.id).status == TaskStatus.FAILED
        assert workspace.exists()
        assert service.task_store.workspaces_are_held(task.id) is True
        assert task.id not in service.task_store.list_workspace_cleanup_pending()

        service.release_task_workspaces(task.id, cleanup=True)
        await _wait_for_cleanup(project.root_path / "tasks" / str(task.id))
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_startup_fails_closed_when_workspace_hold_state_cannot_be_read(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    task = TaskRecord(
        id=UUID("00000000-0000-0000-0000-000000000003"),
        module=BackendModuleName.CO_DEBUG,
        project_id=project.id,
        task_type=TaskType.SCHEDULE_EXPERIMENT,
        status=TaskStatus.RUNNING,
        command=["managed", "interrupted"],
        created_at=utc_now(),
    )
    service.task_store.save(task)
    workspace = workspace_service.create_task_workspace(project.id, task.id)

    def fail_hold_read(_task_id: UUID) -> bool:
        raise OSError("hold table unavailable")

    monkeypatch.setattr(service.task_store, "workspaces_are_held", fail_hold_read)
    try:
        with caplog.at_level("WARNING"):
            await service.startup()

        assert workspace.exists()
        assert "skipping cleanup" in caplog.text
    finally:
        await _close(service)


@pytest.mark.asyncio
async def test_shutdown_invokes_managed_cancellation_handler(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)
    started = threading.Event()
    cancellation_calls: list[UUID] = []

    async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
        started.set()
        while not context.is_cancelled():
            await asyncio.sleep(0.01)
        context.raise_if_cancelled()
        raise AssertionError("cancellation must stop execution")

    async def cancellation_handler(task_id: UUID, _deadline: float | None) -> None:
        cancellation_calls.append(task_id)

    created = await service.create_managed_task(
        module=BackendModuleName.CO_DEBUG,
        project_id=project.id,
        task_type=TaskType.SCHEDULE_EXPERIMENT,
        command=["managed", "shutdown"],
        execute=execute,
        cancellation_handler=cancellation_handler,
    )
    assert await asyncio.to_thread(started.wait, 1)

    await service.shutdown(grace_seconds=1)

    assert cancellation_calls == [created.id]
    assert service.require_task(created.id).status == TaskStatus.CANCELLED
    assert created.id not in service._managed_cancellation_handlers
    service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_create_managed_task_rejects_sync_executor_before_task_creation(tmp_path) -> None:
    service, workspace_service = _service(tmp_path)
    project = await _create_project(workspace_service)

    def execute(_: ManagedTaskContext) -> ManagedTaskResult:
        return ManagedTaskResult(status=TaskStatus.SUCCEEDED)

    try:
        with pytest.raises(AppError, match="execute must be async"):
            await service.create_managed_task(
                module=BackendModuleName.CO_DEBUG,
                project_id=project.id,
                task_type=TaskType.SCHEDULE_EXPERIMENT,
                command=["managed", "sync"],
                execute=execute,
            )

        assert service.list_tasks() == []
    finally:
        await _close(service)
