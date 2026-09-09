import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event
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


def test_artifact_availability_is_written_on_success_and_persists(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    store = TaskStore(database_path)
    task = _task()
    store.save(task)
    running = store.try_start(task.id)
    assert running is not None
    running.status = TaskStatus.SUCCEEDED
    running.finished_at = datetime(2026, 9, 2, 12, 36, tzinfo=UTC)

    finalized, changed = store.finalize_with_transition(
        running,
        artifacts_on_success=True,
    )

    assert changed is True
    assert finalized.status == TaskStatus.SUCCEEDED
    assert store.artifacts_are_available(task.id) is True
    store.close()

    reopened = TaskStore(database_path)
    try:
        assert reopened.artifacts_are_available(task.id) is True
    finally:
        reopened.close()


def test_cancel_winning_finalization_does_not_write_artifact_availability(
    tmp_path,
) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    worker_store = TaskStore(database_path)
    canceller_store = TaskStore(database_path)
    task = _task()
    worker_store.save(task)
    running = worker_store.try_start(task.id)
    assert running is not None
    canceller_store.request_cancel(task.id)
    running.status = TaskStatus.SUCCEEDED
    running.finished_at = datetime(2026, 9, 2, 12, 36, tzinfo=UTC)

    finalized, changed = worker_store.finalize_with_transition(
        running,
        artifacts_on_success=True,
    )

    assert changed is True
    assert finalized.status == TaskStatus.CANCELLED
    assert worker_store.artifacts_are_available(task.id) is False
    assert canceller_store.artifacts_are_available(task.id) is False


def test_legacy_artifact_capabilities_are_backfilled_once(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    store = TaskStore(database_path)
    build = _task()
    build.status = TaskStatus.SUCCEEDED
    code_generation = _task()
    code_generation.module = BackendModuleName.CODE_GENERATION
    code_generation.task_type = TaskType.CODE_GENERATION
    code_generation.status = TaskStatus.SUCCEEDED
    failed_build = _task()
    failed_build.status = TaskStatus.FAILED
    ordinary = _task()
    ordinary.task_type = TaskType.VULNERABILITY_SCAN
    ordinary.status = TaskStatus.SUCCEEDED
    for task in [build, code_generation, failed_build, ordinary]:
        store.save(task)
    store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE task_artifacts_available")

    migrated = TaskStore(database_path)
    try:
        assert migrated.artifacts_are_available(build.id) is True
        assert migrated.artifacts_are_available(code_generation.id) is True
        assert migrated.artifacts_are_available(failed_build.id) is False
        assert migrated.artifacts_are_available(ordinary.id) is False
    finally:
        migrated.close()


def test_existing_artifact_capability_table_is_not_backfilled_on_reopen(
    tmp_path,
) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    store = TaskStore(database_path)
    task = _task()
    task.status = TaskStatus.SUCCEEDED
    store.save(task)
    store.close()

    reopened = TaskStore(database_path)
    try:
        assert reopened.artifacts_are_available(task.id) is False
    finally:
        reopened.close()


def test_concurrent_task_store_initialization_serializes_legacy_migrations(
    tmp_path,
) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    seed = TaskStore(database_path)
    held_task = _task()
    held_task.module = BackendModuleName.CODE_GENERATION
    held_task.task_type = TaskType.CODE_GENERATION
    held_task.status = TaskStatus.RUNNING
    held_task.metadata = {"cleanup_pending": True}
    artifact_task = _task()
    artifact_task.status = TaskStatus.SUCCEEDED
    seed.save(held_task)
    seed.save(artifact_task)
    seed.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE task_workspace_holds")
        connection.execute("DROP TABLE task_artifacts_available")

    ready = [Event(), Event()]
    start = Event()

    def open_store(index: int) -> tuple[bool, bool]:
        ready[index].set()
        if not start.wait(timeout=2):
            raise AssertionError("concurrent migration start was not released")
        store = TaskStore(database_path)
        try:
            return (
                store.workspaces_are_held(held_task.id),
                store.artifacts_are_available(artifact_task.id),
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(open_store, index) for index in range(2)]
        assert all(event.wait(timeout=2) for event in ready)
        start.set()
        results = [future.result(timeout=5) for future in futures]

    assert results == [(True, True), (True, True)]
    reopened = TaskStore(database_path)
    try:
        assert reopened.workspaces_are_held(held_task.id) is True
        assert reopened.artifacts_are_available(artifact_task.id) is True
    finally:
        reopened.close()


def test_task_store_initialization_failure_releases_database_lock(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    original_table_exists = TaskStore._table_exists
    migration_entered = Event()

    def fail_migration(_store, _table_name: str) -> bool:
        migration_entered.set()
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(TaskStore, "_table_exists", fail_migration)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        TaskStore(database_path)
    assert migration_entered.wait(timeout=0.1)
    monkeypatch.setattr(TaskStore, "_table_exists", original_table_exists)

    with sqlite3.connect(database_path, timeout=0.2) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()

    reopened = TaskStore(database_path)
    reopened.close()


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


def test_command_update_requires_running_task_without_cancellation(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    worker_store = TaskStore(database_path)
    canceller_store = TaskStore(database_path)
    task = _task()
    task.command = []
    worker_store.save(task)

    assert worker_store.update_command_if_running(task.id, ["prepared"]) is None
    assert worker_store.try_start(task.id) is not None
    canceller_store.request_cancel(task.id)

    assert worker_store.update_command_if_running(task.id, ["prepared"]) is None
    latest = worker_store.require(task.id)
    assert latest.command == []
    assert latest.cancel_requested is True
    assert latest.revision == 2


def test_metadata_merge_preserves_concurrent_cancellation(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    worker_store = TaskStore(database_path)
    task = _task()
    worker_store.save(task)
    assert worker_store.try_start(task.id) is not None

    worker_store.request_cancel(task.id)
    merged = worker_store.merge_metadata(
        task.id,
        {"naturalcc_run_id": "run-1", "cleanup_pending": True},
    )

    assert merged.metadata == {
        "owner": "test",
        "naturalcc_run_id": "run-1",
        "cleanup_pending": True,
    }
    latest = worker_store.require(task.id)
    assert latest.status == TaskStatus.RUNNING
    assert latest.cancel_requested is True


def test_workspace_hold_is_persisted_independently_until_release(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    first = TaskStore(database_path)
    task = _task()
    first.save(task)
    first.hold_workspaces(task.id, {"cleanup_pending": True})

    second = TaskStore(database_path)
    try:
        assert second.workspaces_are_held(task.id) is True
        assert second.list_held_workspaces() == [task.id]
        assert second.require(task.id).metadata == {
            "owner": "test",
            "cleanup_pending": True,
        }

        released = second.release_workspaces(task.id, {"workspace_released": True})

        assert released.metadata["workspace_released"] is True
        assert first.workspaces_are_held(task.id) is False
        assert first.list_held_workspaces() == []
    finally:
        first.close()
        second.close()


def test_legacy_codegen_cleanup_pending_tasks_gain_workspace_holds_once(
    tmp_path,
) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    store = TaskStore(database_path)
    pending = _task()
    pending.module = BackendModuleName.CODE_GENERATION
    pending.task_type = TaskType.CODE_GENERATION
    pending.metadata = {"cleanup_pending": True}
    running = _task()
    running.module = BackendModuleName.CODE_GENERATION
    running.task_type = TaskType.CODE_GENERATION
    running.status = TaskStatus.RUNNING
    running.metadata = {"cleanup_pending": True}
    ordinary = _task()
    ordinary.metadata = {"cleanup_pending": True}
    safe_codegen = _task()
    safe_codegen.module = BackendModuleName.CODE_GENERATION
    safe_codegen.task_type = TaskType.CODE_GENERATION
    safe_codegen.metadata = {"cleanup_pending": False}
    for task in [pending, running, ordinary, safe_codegen]:
        store.save(task)
    store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE task_workspace_holds")

    migrated = TaskStore(database_path)
    assert migrated.workspaces_are_held(pending.id) is True
    assert migrated.workspaces_are_held(running.id) is True
    assert migrated.workspaces_are_held(ordinary.id) is False
    assert migrated.workspaces_are_held(safe_codegen.id) is False
    migrated.release_workspaces(pending.id)
    migrated.close()

    reopened = TaskStore(database_path)
    try:
        assert reopened.workspaces_are_held(pending.id) is False
        assert reopened.workspaces_are_held(running.id) is True
    finally:
        reopened.close()


def test_workspace_hold_rolls_back_when_metadata_serialization_fails(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task = _task()
    store.save(task)

    with pytest.raises(TypeError):
        store.hold_workspaces(task.id, {"invalid": object()})

    assert store.workspaces_are_held(task.id) is False
    assert store.require(task.id).metadata == {"owner": "test"}


def test_workspace_release_rolls_back_when_metadata_serialization_fails(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task = _task()
    store.save(task)
    store.hold_workspaces(task.id, {"cleanup_pending": True})

    with pytest.raises(TypeError):
        store.release_workspaces(
            task.id,
            {"cleanup_pending": False, "invalid": object()},
        )

    assert store.workspaces_are_held(task.id) is True
    assert store.require(task.id).metadata["cleanup_pending"] is True


def test_workspace_cleanup_completion_is_persisted_and_applied_atomically(
    tmp_path,
) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    first = TaskStore(database_path)
    task = _task()
    first.save(task)
    first.hold_workspaces(task.id, {"cleanup_pending": True})

    queued = first.queue_workspace_cleanup_and_release_hold(
        task.id,
        {"cleanup_pending": False, "cleanup_completed": True},
    )

    assert first.workspaces_are_held(task.id) is False
    assert first.list_workspace_cleanup_pending() == [task.id]
    assert queued.metadata["cleanup_pending"] is True
    assert "cleanup_completed" not in queued.metadata
    first.close()

    reopened = TaskStore(database_path)
    try:
        assert reopened.list_workspace_cleanup_pending() == [task.id]
        assert reopened.require(task.id).metadata["cleanup_pending"] is True

        completed = reopened.complete_workspace_cleanup(task.id)

        assert reopened.list_workspace_cleanup_pending() == []
        assert completed.metadata["cleanup_pending"] is False
        assert completed.metadata["cleanup_completed"] is True
    finally:
        reopened.close()


def test_repeated_workspace_cleanup_queue_merges_completion_metadata(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task = _task()
    store.save(task)
    store.hold_workspaces(task.id)

    store.queue_workspace_cleanup_and_release_hold(
        task.id,
        {"first": 1, "nested": {"left": True}},
    )
    store.queue_workspace_cleanup_and_release_hold(
        task.id,
        {"second": 2, "nested": {"right": True}},
    )
    completed = store.complete_workspace_cleanup(task.id)

    assert completed.metadata == {
        "owner": "test",
        "first": 1,
        "second": 2,
        "nested": {"left": True, "right": True},
    }
    assert store.list_workspace_cleanup_pending() == []


def test_workspace_cleanup_completion_failure_keeps_intent(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    store = TaskStore(database_path)
    task = _task()
    store.save(task)
    store.queue_workspace_cleanup_and_release_hold(
        task.id,
        {"cleanup_pending": False},
    )
    store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE task_workspace_cleanup
            SET completion_metadata_json = ?
            WHERE task_id = ?
            """,
            ("{invalid", str(task.id)),
        )

    reopened = TaskStore(database_path)
    try:
        with pytest.raises(json.JSONDecodeError):
            reopened.complete_workspace_cleanup(task.id)

        assert reopened.list_workspace_cleanup_pending() == [task.id]
        assert reopened.require(task.id).metadata == {"owner": "test"}
    finally:
        reopened.close()


def test_workspace_cleanup_schema_migrates_existing_database(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE task_workspace_cleanup (task_id TEXT PRIMARY KEY)"
        )

    store = TaskStore(database_path)
    store.close()
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(task_workspace_cleanup)"
            ).fetchall()
        }

    assert "completion_metadata_json" in columns


def test_finalization_and_cleanup_intent_commit_atomically_across_reopen(
    tmp_path,
) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    worker_store = TaskStore(database_path)
    canceller_store = TaskStore(database_path)
    task = _task()
    worker_store.save(task)
    running = worker_store.try_start(task.id)
    assert running is not None
    canceller_store.request_cancel(task.id)
    running.status = TaskStatus.SUCCEEDED

    finalized, changed = worker_store.finalize_with_transition(
        running,
        queue_workspace_cleanup_on_finalize=True,
        preserve_workspace_on_success=True,
    )

    assert changed is True
    assert finalized.status == TaskStatus.CANCELLED
    assert worker_store.list_workspace_cleanup_pending() == [task.id]
    worker_store.close()
    canceller_store.close()

    reopened = TaskStore(database_path)
    try:
        assert reopened.require(task.id).status == TaskStatus.CANCELLED
        assert reopened.list_workspace_cleanup_pending() == [task.id]
    finally:
        reopened.close()


def test_cleanup_intent_failure_rolls_back_managed_finalization(tmp_path) -> None:
    database_path = tmp_path / "tasks.sqlite3"
    store = TaskStore(database_path)
    task = _task()
    store.save(task)
    running = store.try_start(task.id)
    assert running is not None
    running.status = TaskStatus.SUCCEEDED
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_workspace_cleanup_intent
            BEFORE INSERT ON task_workspace_cleanup
            BEGIN
                SELECT RAISE(ABORT, 'cleanup intent failed');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="cleanup intent failed"):
        store.finalize_with_transition(
            running,
            artifacts_on_success=True,
            queue_workspace_cleanup_on_finalize=True,
        )

    assert store.require(task.id).status == TaskStatus.RUNNING
    assert store.list_workspace_cleanup_pending() == []
    assert store.artifacts_are_available(task.id) is False
    store.close()

    reopened = TaskStore(database_path)
    try:
        assert reopened.require(task.id).status == TaskStatus.RUNNING
        assert reopened.list_workspace_cleanup_pending() == []
        assert reopened.artifacts_are_available(task.id) is False
    finally:
        reopened.close()


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
