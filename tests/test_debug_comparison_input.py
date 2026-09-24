import asyncio
import io
import json
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.core.errors import AppError
from app.main import create_app
from app.modules.co_debug.schemas.debug_workloads import DebugComparisonRequest
from app.modules.co_debug.services.metric_service import AcceptanceMetricService
from app.modules.co_debug.services.schedule_comparison_service import ScheduleComparisonService
from app.modules.co_debug.services.schedule_execution_service import ScheduleExecutionService
from app.modules.co_debug.services.scheduler_service import SchedulerService
from app.modules.co_debug.services.task_service import CoDebugTaskService
from app.platform.api.deps import get_co_debug_task_service
from app.platform.domain.enums import TaskStatus
from app.platform.schemas.tasks import BuildTaskRequest
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessResult
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    async def run(
        self,
        command,
        cwd,
        timeout_seconds,
        on_log,
        is_cancelled,
        on_process_started=None,
        cpu_core=None,
    ) -> ProcessResult:
        self.calls.append((command, cwd))
        if command == ["build-all"]:
            for index in range(1, 4):
                (cwd / f"case-{index}" / "app").write_text("binary", encoding="utf-8")
        return ProcessResult(exit_code=0, elapsed_ms=10)


def _manifest() -> dict:
    return {
        "version": 1,
        "workloads": [
            {
                "name": f"case-{index}",
                "work_dir": f"case-{index}",
                "jobs": [
                    {
                        "name": f"case-{index}-job-{job}",
                        "executable_path": "app",
                        "breakpoint": "main",
                        "args": [str(job)],
                    }
                    for job in range(1, 3)
                ],
            }
            for index in range(1, 4)
        ],
    }


async def _project(workspaces: WorkspaceService, manifest: dict):
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("debug-workloads.json", json.dumps(manifest))
        for index in range(1, 4):
            zip_file.writestr(f"case-{index}/source.c", "int main(void) { return 0; }")
    archive.seek(0)
    return await workspaces.create_from_archive(
        UploadFile(file=archive, filename="three-cases.zip")
    )


async def _wait_for_terminal(service: TaskService, task_id: UUID):
    for _ in range(100):
        task = service.require_task(task_id)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("task did not finish")


def _services(tmp_path):
    workspaces = WorkspaceService(tmp_path / "workspaces")
    runner = FakeRunner()
    service = TaskService(
        workspace_service=workspaces,
        task_store=TaskStore(tmp_path / "tasks.sqlite3"),
        log_service=TaskLogService(100, tmp_path / "logs.sqlite3"),
        process_runner=runner,
        default_timeout_seconds=10,
    )
    scheduler = SchedulerService()
    co_debug = CoDebugTaskService(
        task_service=service,
        scheduler_service=scheduler,
        schedule_execution_service=ScheduleExecutionService(process_runner=runner),
        schedule_comparison_service=ScheduleComparisonService(
            scheduler_service=scheduler,
            execution_service=ScheduleExecutionService(process_runner=runner),
            metric_service=AcceptanceMetricService(),
        ),
    )
    return co_debug, service, workspaces, runner


@pytest.mark.asyncio
async def test_three_debug_workloads_use_successful_build_snapshot(tmp_path) -> None:
    co_debug, service, workspaces, runner = _services(tmp_path)
    project = await _project(workspaces, _manifest())
    try:
        build = await co_debug.create_build_task(
            BuildTaskRequest(project_id=project.id, command=["build-all"])
        )
        assert (await _wait_for_terminal(service, build.id)).status == TaskStatus.SUCCEEDED
        created = await co_debug.create_debug_schedule_comparison(
            DebugComparisonRequest(project_id=project.id, build_task_id=build.id, core_ids=[0, 1])
        )
        completed = await _wait_for_terminal(service, created.id)
        assert completed.status == TaskStatus.SUCCEEDED
        assert completed.result["workload_count"] == 3
        assert completed.metadata["debug_workload_manifest"] == "debug-workloads.json"
        assert len(runner.calls) == 13
        commands = runner.calls[1:]
        assert {cwd.name for _, cwd in commands} == {"case-1", "case-2", "case-3"}
        for command, cwd in commands:
            assert command[:5] == ["gdb", "--nx", "--quiet", "--batch", "--return-child-result"]
            assert ["-ex", "break main"] == command[7:9]
            assert command[-3] == "--args"
            assert command[-2] == "./app"
            assert (cwd / "app").read_text(encoding="utf-8") == "binary"
    finally:
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_debug_comparison_rejects_missing_build_output(tmp_path) -> None:
    co_debug, service, workspaces, _ = _services(tmp_path)
    project = await _project(workspaces, _manifest())
    try:
        with pytest.raises(AppError, match="debug executable does not exist"):
            await co_debug.create_debug_schedule_comparison(
                DebugComparisonRequest(project_id=project.id)
            )
        assert service.list_tasks() == []
    finally:
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_debug_comparison_rejects_path_escape(tmp_path) -> None:
    co_debug, service, workspaces, _ = _services(tmp_path)
    manifest = _manifest()
    manifest["workloads"][0]["jobs"][0]["executable_path"] = "../outside"
    project = await _project(workspaces, manifest)
    try:
        with pytest.raises(AppError, match="paths must be relative"):
            await co_debug.create_debug_schedule_comparison(
                DebugComparisonRequest(project_id=project.id)
            )
    finally:
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_debug_comparison_route_accepts_project_and_build_ids(tmp_path) -> None:
    co_debug, service, workspaces, _ = _services(tmp_path)
    project = await _project(workspaces, _manifest())
    app = create_app()
    app.dependency_overrides[get_co_debug_task_service] = lambda: co_debug
    try:
        build = await co_debug.create_build_task(
            BuildTaskRequest(project_id=project.id, command=["build-all"])
        )
        assert (await _wait_for_terminal(service, build.id)).status == TaskStatus.SUCCEEDED

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/modules/co-debug/debug/comparisons",
                json={
                    "project_id": str(project.id),
                    "build_task_id": str(build.id),
                    "core_ids": [0, 1],
                },
            )
        assert response.status_code == 200
        task_id = UUID(response.json()["data"]["id"])
        assert (await _wait_for_terminal(service, task_id)).status == TaskStatus.SUCCEEDED
    finally:
        await service.shutdown(grace_seconds=0)
        service.close_resources_when_idle()
