import asyncio
import io
import sys
import time
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
