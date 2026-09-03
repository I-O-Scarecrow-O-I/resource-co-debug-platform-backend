import asyncio
import io
import json
import sys
import time
from datetime import timedelta
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi import UploadFile, WebSocketDisconnect

from app.core.time import utc_now
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


def _project_manifest(project_id, **overrides) -> dict[str, object]:
    return {
        "version": 1,
        "id": str(project_id),
        "name": "saved project",
        "status": "READY",
        "created_at": "2026-09-03T12:00:00+00:00",
        **overrides,
    }


def _write_manifest(project_dir, manifest: dict[str, object]) -> None:
    project_dir.mkdir()
    (project_dir / ".project.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.asyncio
async def test_task_workspaces_are_independent(tmp_path) -> None:
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("source.c", "int main(void) { return 0; }")
    archive.seek(0)

    service = WorkspaceService(storage_root=tmp_path)
    project = await service.create_from_archive(
        archive=UploadFile(file=archive, filename="project.zip"),
        display_name="test-project",
    )
    task_id = uuid4()

    first = service.create_task_workspace(project.id, task_id)
    second = service.create_task_workspace(project.id, task_id)
    (first / "result.txt").write_text("first", encoding="utf-8")

    assert first != second
    assert (first / "source.c").is_file()
    assert not (second / "result.txt").exists()


@pytest.mark.asyncio
async def test_project_workspace_is_restored_from_manifest(tmp_path) -> None:
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("source.c", "int main(void) { return 0; }")
    archive.seek(0)

    created = await WorkspaceService(tmp_path).create_from_archive(
        archive=UploadFile(file=archive, filename="project.zip"),
        display_name="\u4e2d\u6587\u9879\u76ee",
    )

    manifest_path = created.root_path / ".project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored = WorkspaceService(tmp_path).require_project(created.id)

    assert set(manifest) == {"version", "id", "name", "status", "created_at"}
    assert "\u4e2d\u6587\u9879\u76ee" in manifest_path.read_text(encoding="utf-8")
    assert restored == created
    assert restored.created_at.utcoffset() == timedelta(0)


def test_invalid_project_manifests_are_skipped(tmp_path) -> None:
    corrupt_dir = tmp_path / str(uuid4())
    corrupt_dir.mkdir()
    (corrupt_dir / ".project.json").write_text("{not json", encoding="utf-8")

    mismatch_dir = tmp_path / str(uuid4())
    _write_manifest(mismatch_dir, _project_manifest(uuid4()))
    (mismatch_dir / "source").mkdir()

    missing_source_dir = tmp_path / str(uuid4())
    _write_manifest(missing_source_dir, _project_manifest(missing_source_dir.name))

    unsupported_dir = tmp_path / str(uuid4())
    _write_manifest(unsupported_dir, _project_manifest(unsupported_dir.name, version=2))
    (unsupported_dir / "source").mkdir()

    service = WorkspaceService(tmp_path)

    assert service.list_projects() == []


def test_bad_id_manifest_does_not_block_valid_project_loading(tmp_path) -> None:
    invalid_dir = tmp_path / str(uuid4())
    _write_manifest(invalid_dir, _project_manifest(uuid4(), id=[]))
    (invalid_dir / "source").mkdir()

    valid_id = uuid4()
    valid_dir = tmp_path / str(valid_id)
    _write_manifest(valid_dir, _project_manifest(valid_id))
    (valid_dir / "source").mkdir()

    service = WorkspaceService(tmp_path)

    assert service.list_projects() == [service.require_project(valid_id)]


@pytest.mark.parametrize("status", ["UPLOADED", "INVALID", "ARCHIVED"])
def test_non_ready_project_manifests_are_skipped(tmp_path, status) -> None:
    project_id = uuid4()
    project_dir = tmp_path / str(project_id)
    _write_manifest(project_dir, _project_manifest(project_id, status=status))
    (project_dir / "source").mkdir()

    assert WorkspaceService(tmp_path).list_projects() == []


def test_manifest_paths_are_not_adopted(tmp_path) -> None:
    project_id = uuid4()
    project_dir = tmp_path / str(project_id)
    _write_manifest(
        project_dir,
        _project_manifest(
            project_id,
            root_path=str(tmp_path.parent),
            source_path=str(tmp_path.parent),
        ),
    )
    (project_dir / "source").mkdir()

    restored = WorkspaceService(tmp_path).require_project(project_id)

    assert restored.root_path == project_dir.resolve()
    assert restored.source_path == (project_dir / "source").resolve()


def test_project_with_internal_symlink_is_skipped(tmp_path) -> None:
    project_id = uuid4()
    project_dir = tmp_path / str(project_id)
    _write_manifest(project_dir, _project_manifest(project_id))
    source_dir = project_dir / "source"
    source_dir.mkdir()
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    try:
        (source_dir / "external").symlink_to(external_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable in this environment")

    assert WorkspaceService(tmp_path).list_projects() == []


@pytest.mark.asyncio
async def test_manifest_write_failure_removes_new_project_directory(tmp_path, monkeypatch) -> None:
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("source.c", "int main(void) { return 0; }")
    archive.seek(0)

    service = WorkspaceService(tmp_path)

    def fail_manifest_write(_) -> None:
        raise OSError("manifest write failed")

    monkeypatch.setattr(service, "_write_manifest", fail_manifest_write)

    with pytest.raises(OSError, match="manifest write failed"):
        await service.create_from_archive(UploadFile(file=archive, filename="project.zip"))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_log_events_can_be_appended_from_another_thread() -> None:
    service = TaskLogService(max_lines=10)
    task_id = uuid4()
    queue = service.subscribe(task_id)

    await asyncio.to_thread(service.append, task_id, "worker event")
    event = await asyncio.wait_for(queue.get(), timeout=1)

    assert event.message == "worker event"
    service.unsubscribe(task_id, queue)


@pytest.mark.asyncio
async def test_log_history_and_subscription_keep_sequence_order() -> None:
    service = TaskLogService(max_lines=20)
    task_id = uuid4()
    queue = service.subscribe(task_id)

    await asyncio.gather(
        *(asyncio.to_thread(service.append, task_id, f"event-{index}") for index in range(10))
    )

    history = service.history(task_id)
    streamed = [await queue.get() for _ in history]
    assert [event.sequence for event in streamed] == [event.sequence for event in history]
    assert [event.message for event in streamed] == [event.message for event in history]
    service.unsubscribe(task_id, queue)


@pytest.mark.asyncio
async def test_log_history_to_realtime_switch_has_no_gaps_or_duplicates(monkeypatch) -> None:
    import app.main as main

    service = TaskLogService(max_lines=20)
    task_id = uuid4()
    service.append(task_id, "history")

    class SwitchingWebSocket:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        async def accept(self) -> None:
            pass

        async def send_json(self, event: dict[str, object]) -> None:
            self.events.append(event)
            if len(self.events) == 1:
                await asyncio.to_thread(service.append, task_id, "realtime")
            else:
                raise WebSocketDisconnect()

    websocket = SwitchingWebSocket()
    endpoint = next(
        route.endpoint
        for route in main.app.routes
        if getattr(route, "path", None) == "/ws/v1/tasks/{task_id}/logs"
    )
    monkeypatch.setattr(main, "get_log_service", lambda: service)

    await asyncio.wait_for(endpoint(websocket, str(task_id)), timeout=1)

    assert [event["sequence"] for event in websocket.events] == [1, 2]
    assert [event["message"] for event in websocket.events] == ["history", "realtime"]


@pytest.mark.asyncio
async def test_log_history_send_failure_unsubscribes(monkeypatch) -> None:
    import app.main as main

    service = TaskLogService(max_lines=20)
    task_id = uuid4()
    service.append(task_id, "history")

    class FailingWebSocket:
        async def accept(self) -> None:
            pass

        async def send_json(self, _: dict[str, object]) -> None:
            raise RuntimeError("connection failed")

    endpoint = next(
        route.endpoint
        for route in main.app.routes
        if getattr(route, "path", None) == "/ws/v1/tasks/{task_id}/logs"
    )
    monkeypatch.setattr(main, "get_log_service", lambda: service)

    with pytest.raises(RuntimeError, match="connection failed"):
        await endpoint(FailingWebSocket(), str(task_id))

    assert not service._subscribers[str(task_id)]


def test_cancelled_pending_task_cannot_start() -> None:
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

    store.request_cancel(task.id)

    assert store.try_start(task.id) is None
    assert store.require(task.id).status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_process_runner_timeout_is_bounded(tmp_path) -> None:
    started = time.perf_counter()

    with pytest.raises(TimeoutError):
        await ProcessRunner().run(
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout_seconds=1,
            on_log=lambda _message, _stream: None,
            is_cancelled=lambda: False,
        )

    assert time.perf_counter() - started < 3
