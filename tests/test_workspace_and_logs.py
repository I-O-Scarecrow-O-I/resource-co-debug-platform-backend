import asyncio
import io
import json
import stat
import sys
import time
from datetime import timedelta
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

import pytest
from fastapi import UploadFile, WebSocketDisconnect
from fastapi.testclient import TestClient

from app.core.errors import AppError, NotFoundError
from app.core.time import utc_now
from app.main import create_app
from app.platform.api.deps import get_workspace_service
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.services.log_service import LogStreamOverflow, TaskLogService
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
async def test_invalid_archive_is_rejected_and_removes_project_directory(tmp_path) -> None:
    service = WorkspaceService(tmp_path)

    with pytest.raises(AppError, match="invalid zip archive"):
        await service.create_from_archive(
            UploadFile(file=io.BytesIO(b"not a zip"), filename="bad.zip")
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_unsafe_archive_entry_removes_project_directory(tmp_path) -> None:
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("../outside.txt", "unsafe")
    archive.seek(0)
    service = WorkspaceService(tmp_path)

    with pytest.raises(AppError, match="unsafe zip entry"):
        await service.create_from_archive(UploadFile(file=archive, filename="unsafe.zip"))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_archive_resource_limits_are_rejected_without_orphans(tmp_path, monkeypatch) -> None:
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("first.txt", "one")
        zip_file.writestr("second.txt", "two")
    archive.seek(0)
    monkeypatch.setattr(WorkspaceService, "_MAX_ZIP_MEMBERS", 1)

    with pytest.raises(AppError, match="too many entries"):
        await WorkspaceService(tmp_path).create_from_archive(
            UploadFile(file=archive, filename="limited.zip")
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_archive_size_limit_is_rejected_without_orphans(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(WorkspaceService, "MAX_ARCHIVE_BYTES", 3, raising=False)
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("source.txt", "source")
    archive.seek(0)

    with pytest.raises(AppError, match="zip archive exceeds compressed size limit"):
        await WorkspaceService(tmp_path).create_from_archive(
            UploadFile(file=archive, filename="limited.zip")
        )

    assert list(tmp_path.iterdir()) == []


def test_project_upload_rejects_excessive_content_length(monkeypatch) -> None:
    monkeypatch.setattr(WorkspaceService, "MAX_PROJECT_UPLOAD_BODY_BYTES", 10, raising=False)
    client = TestClient(create_app())

    try:
        response = client.post("/api/v1/projects", headers={"content-length": "11"})
    finally:
        client.close()

    assert response.status_code == 413
    assert response.json()["success"] is False
    assert response.json()["message"] == "project upload exceeds size limit"


@pytest.mark.asyncio
async def test_project_upload_rejects_chunked_body_exceeding_limit(monkeypatch) -> None:
    first_chunk = (
        b"--test\r\n"
        b'Content-Disposition: form-data; name="archive"; filename="project.zip"\r\n'
        b"Content-Type: application/zip\r\n\r\n"
    )
    monkeypatch.setattr(
        WorkspaceService,
        "MAX_PROJECT_UPLOAD_BODY_BYTES",
        len(first_chunk),
        raising=False,
    )
    messages = iter(
        [
            {"type": "http.request", "body": first_chunk, "more_body": True},
            {"type": "http.request", "body": b"1", "more_body": False},
        ]
    )
    sent = []

    async def receive():
        return next(messages, {"type": "http.disconnect"})

    async def send(message):
        sent.append(message)

    await create_app()(  # type: ignore[operator]
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/projects",
            "raw_path": b"/api/v1/projects",
            "query_string": b"",
            "headers": [(b"content-type", b"multipart/form-data; boundary=test")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )

    assert sent[0]["status"] == 413
    payload = json.loads(sent[1]["body"])
    assert payload["success"] is False
    assert payload["message"] == "project upload exceeds size limit"


@pytest.mark.asyncio
async def test_archive_allows_explicit_directory_and_permission_only_regular_file(tmp_path) -> None:
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        directory = ZipInfo("nested/")
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16
        zip_file.writestr(directory, "")
        member = ZipInfo("nested/source.c")
        member.external_attr = 0o600 << 16
        zip_file.writestr(member, "int main(void) { return 0; }")
    archive.seek(0)

    project = await WorkspaceService(tmp_path).create_from_archive(
        UploadFile(file=archive, filename="project.zip")
    )

    assert (project.source_path / "nested" / "source.c").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_type", [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK])
async def test_archive_rejects_special_entry_disguised_as_directory(tmp_path, entry_type) -> None:
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        member = ZipInfo("unsafe/")
        member.external_attr = (entry_type | 0o777) << 16
        zip_file.writestr(member, "")
    archive.seek(0)

    with pytest.raises(AppError, match="unsupported zip entry"):
        await WorkspaceService(tmp_path).create_from_archive(
            UploadFile(file=archive, filename="unsafe.zip")
        )

    assert list(tmp_path.iterdir()) == []


def test_invalid_archive_api_response_has_no_orphan_directory(tmp_path) -> None:
    workspace_service = WorkspaceService(tmp_path)
    app = create_app()
    app.dependency_overrides[get_workspace_service] = lambda: workspace_service
    client = TestClient(app)

    try:
        response = client.post(
            "/api/v1/projects",
            files={"archive": ("bad.zip", b"not a zip", "application/zip")},
        )
    finally:
        client.close()

    assert response.status_code == 400
    assert response.json()["message"] == "invalid zip archive"
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
async def test_slow_log_subscriber_keeps_latest_events_with_bounded_queue() -> None:
    service = TaskLogService(max_lines=2)
    task_id = uuid4()
    queue = service.subscribe(task_id)

    await asyncio.to_thread(
        lambda: [service.append(task_id, f"event-{index}") for index in range(5)]
    )
    await asyncio.sleep(0)

    assert queue.maxsize == 2
    assert isinstance(queue.get_nowait(), LogStreamOverflow)
    assert [event.message for event in service.history(task_id)] == ["event-3", "event-4"]
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
    monkeypatch.setattr(
        main,
        "get_task_service",
        lambda: type("TaskService", (), {"require_task": lambda _, value: value})(),
    )

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
    monkeypatch.setattr(
        main,
        "get_task_service",
        lambda: type("TaskService", (), {"require_task": lambda _, value: value})(),
    )

    with pytest.raises(RuntimeError, match="connection failed"):
        await endpoint(FailingWebSocket(), str(task_id))

    assert not service._subscribers[str(task_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", ["not-a-uuid", str(uuid4())])
async def test_log_websocket_rejects_invalid_or_missing_task(monkeypatch, task_id) -> None:
    import app.main as main

    service = TaskLogService(max_lines=20)

    class RejectingWebSocket:
        def __init__(self) -> None:
            self.accepted = False
            self.close_code = None

        async def accept(self) -> None:
            self.accepted = True

        async def close(self, code: int) -> None:
            self.close_code = code

    endpoint = next(
        route.endpoint
        for route in main.app.routes
        if getattr(route, "path", None) == "/ws/v1/tasks/{task_id}/logs"
    )
    monkeypatch.setattr(main, "get_log_service", lambda: service)
    monkeypatch.setattr(
        main,
        "get_task_service",
        lambda: type(
            "TaskService",
            (),
            {"require_task": lambda _, value: (_ for _ in ()).throw(NotFoundError(str(value)))},
        )(),
    )
    websocket = RejectingWebSocket()

    await endpoint(websocket, task_id)

    assert not websocket.accepted
    assert websocket.close_code == 1008
    assert not service._subscribers


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
