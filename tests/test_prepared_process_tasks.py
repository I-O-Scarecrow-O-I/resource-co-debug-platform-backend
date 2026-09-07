import asyncio
import io
import threading
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi import UploadFile

from app.core.errors import AppError
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessResult
from app.platform.services.task_execution import PreparedProcess, TaskPreparationContext
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


class RecordingProcessRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, int]] = []

    async def run(
        self,
        command,
        cwd,
        timeout_seconds,
        on_log,
        is_cancelled,
        on_process_started=None,
    ):
        self.calls.append((command, cwd, timeout_seconds))
        if on_process_started is not None:
            on_process_started(object())
        on_log("prepared process ran", "stdout")
        return ProcessResult(exit_code=0, elapsed_ms=1)


class LaunchBoundaryProcessRunner:
    def __init__(self) -> None:
        self.launch_started = threading.Event()
        self.allow_registration = threading.Event()
        self.cancelled_after_registration = threading.Event()

    async def run(
        self,
        command,
        cwd,
        timeout_seconds,
        on_log,
        is_cancelled,
        on_process_started=None,
    ):
        self.launch_started.set()
        while not self.allow_registration.is_set():
            await asyncio.sleep(0.01)
        assert on_process_started is not None
        on_process_started(object())
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled_after_registration.set()
            raise


def _service(
    tmp_path, process_runner: RecordingProcessRunner
) -> tuple[TaskService, WorkspaceService]:
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    return (
        TaskService(
            workspace_service=workspace_service,
            task_store=TaskStore(tmp_path / "tasks.sqlite3"),
            log_service=TaskLogService(100, tmp_path / "logs.sqlite3"),
            process_runner=process_runner,
            scheduler_service=None,
            schedule_execution_service=None,
            schedule_comparison_service=None,
            default_timeout_seconds=10,
        ),
        workspace_service,
    )


async def _project(workspace_service: WorkspaceService):
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("input.txt", "source")
        zip_file.writestr("nested/.keep", "")
    archive.seek(0)
    return await workspace_service.create_from_archive(
        UploadFile(file=archive, filename="project.zip")
    )


async def _wait_for_terminal(service: TaskService, task_id: UUID):
    for _ in range(100):
        task = service.require_task(task_id)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("task did not finish")


async def _wait_for_path_removal(path: Path) -> None:
    for _ in range(100):
        if not path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"path was not removed: {path}")


@pytest.mark.asyncio
async def test_prepared_process_uses_isolated_workspace_and_persists_command(tmp_path) -> None:
    runner = RecordingProcessRunner()
    service, workspace_service = _service(tmp_path, runner)
    project = await _project(workspace_service)
    received: list[TaskPreparationContext] = []

    async def prepare(context: TaskPreparationContext) -> PreparedProcess:
        received.append(context)
        assert context.workspace != project.source_path
        assert (context.workspace / "input.txt").read_text() == "source"
        context.log("preparing command")
        context.report_progress(40, "command prepared")
        return PreparedProcess(command=["prepared-tool", "--flag"], work_dir="nested")

    try:
        task = await service.create_prepared_process_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.BUILD,
            prepare=prepare,
            timeout_seconds=7,
        )
        completed = await _wait_for_terminal(service, task.id)

        assert completed.status == TaskStatus.SUCCEEDED
        assert service.require_task(task.id).command == ["prepared-tool", "--flag"]
        assert len(received) == 1
        assert runner.calls == [(["prepared-tool", "--flag"], received[0].workspace / "nested", 7)]
        assert (received[0].workspace / "input.txt").is_file()
    finally:
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_sync_prepare_is_rejected(tmp_path) -> None:
    runner = RecordingProcessRunner()
    service, workspace_service = _service(tmp_path, runner)
    project = await _project(workspace_service)

    def prepare(_: TaskPreparationContext) -> PreparedProcess:
        return PreparedProcess(command=["prepared-tool"])

    try:
        with pytest.raises(AppError, match="prepare must be async"):
            await service.create_prepared_process_task(
                module=BackendModuleName.CO_DEBUG,
                project_id=project.id,
                task_type=TaskType.DEPENDENCY_ANALYSIS,
                prepare=prepare,
            )
        assert runner.calls == []
    finally:
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_prepare_failure_marks_task_failed_without_starting_process(tmp_path) -> None:
    runner = RecordingProcessRunner()
    service, workspace_service = _service(tmp_path, runner)
    project = await _project(workspace_service)

    async def prepare(_: TaskPreparationContext) -> PreparedProcess:
        raise RuntimeError("preparation failed")

    try:
        task = await service.create_prepared_process_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.DEPENDENCY_ANALYSIS,
            prepare=prepare,
        )
        completed = await _wait_for_terminal(service, task.id)

        assert completed.status == TaskStatus.FAILED
        assert completed.error == "preparation failed"
        assert runner.calls == []
    finally:
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_cancellation_before_prepared_command_is_persisted_does_not_start_process(
    tmp_path, monkeypatch
) -> None:
    runner = RecordingProcessRunner()
    service, workspace_service = _service(tmp_path, runner)
    project = await _project(workspace_service)
    original_update = service.task_store.update_command_if_running

    def cancel_before_update(task_id: UUID, command: list[str]):
        service.task_store.request_cancel(task_id)
        return original_update(task_id, command)

    monkeypatch.setattr(service.task_store, "update_command_if_running", cancel_before_update)

    try:
        task = await service.create_prepared_process_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.DEPENDENCY_ANALYSIS,
            prepare=_prepared_tool,
        )
        completed = await _wait_for_terminal(service, task.id)

        assert completed.status == TaskStatus.CANCELLED
        assert completed.command == []
        assert runner.calls == []
        await _wait_for_path_removal(project.root_path / "tasks" / str(task.id))
    finally:
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_cancellation_during_prepare_does_not_start_process(tmp_path) -> None:
    runner = RecordingProcessRunner()
    service, workspace_service = _service(tmp_path, runner)
    project = await _project(workspace_service)
    started = threading.Event()
    release = threading.Event()

    async def prepare(context: TaskPreparationContext) -> PreparedProcess:
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        context.raise_if_cancelled()
        return PreparedProcess(command=["prepared-tool"])

    try:
        task = await service.create_prepared_process_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.DEPENDENCY_ANALYSIS,
            prepare=prepare,
        )
        assert await asyncio.to_thread(started.wait, 1)
        await service.cancel_task(task.id)
        release.set()
        completed = await _wait_for_terminal(service, task.id)

        assert completed.status == TaskStatus.CANCELLED
        assert runner.calls == []
    finally:
        release.set()
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_async_prepare_timeout_marks_task_failed_and_cleans_workspace(tmp_path) -> None:
    runner = RecordingProcessRunner()
    service, workspace_service = _service(tmp_path, runner)
    project = await _project(workspace_service)

    async def prepare(_: TaskPreparationContext) -> PreparedProcess:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    try:
        task = await service.create_prepared_process_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.DEPENDENCY_ANALYSIS,
            prepare=prepare,
            timeout_seconds=0.05,
        )
        completed = await _wait_for_terminal(service, task.id)

        assert completed.status == TaskStatus.FAILED
        assert completed.error == "task timed out after 0.05 seconds"
        assert runner.calls == []
        await _wait_for_path_removal(project.root_path / "tasks" / str(task.id))
    finally:
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_async_prepare_cancelled_error_marks_task_cancelled_and_cleans_workspace(
    tmp_path,
) -> None:
    runner = RecordingProcessRunner()
    service, workspace_service = _service(tmp_path, runner)
    project = await _project(workspace_service)

    async def prepare(_: TaskPreparationContext) -> PreparedProcess:
        raise asyncio.CancelledError

    try:
        task = await service.create_prepared_process_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.DEPENDENCY_ANALYSIS,
            prepare=prepare,
        )
        completed = await _wait_for_terminal(service, task.id)

        assert completed.status == TaskStatus.CANCELLED
        assert runner.calls == []
        await _wait_for_path_removal(project.root_path / "tasks" / str(task.id))
    finally:
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_cancellation_during_launch_cancels_registered_process(tmp_path) -> None:
    runner = LaunchBoundaryProcessRunner()
    service, workspace_service = _service(tmp_path, runner)
    project = await _project(workspace_service)

    try:
        task = await service.create_prepared_process_task(
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=TaskType.DEPENDENCY_ANALYSIS,
            prepare=_prepared_tool,
        )
        assert await asyncio.to_thread(runner.launch_started.wait, 1)

        cancelled = await service.cancel_task(task.id)
        assert cancelled.cancel_requested is True
        assert not runner.cancelled_after_registration.is_set()

        runner.allow_registration.set()
        completed = await _wait_for_terminal(service, task.id)

        assert completed.status == TaskStatus.CANCELLED
        assert runner.cancelled_after_registration.is_set()
        await _wait_for_path_removal(project.root_path / "tasks" / str(task.id))
    finally:
        runner.allow_registration.set()
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


async def _prepared_tool(_: TaskPreparationContext) -> PreparedProcess:
    return PreparedProcess(command=["prepared-tool"])
