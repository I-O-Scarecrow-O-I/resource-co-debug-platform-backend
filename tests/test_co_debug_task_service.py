import asyncio
import io
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from fastapi import UploadFile

from app.core.errors import AppError
from app.core.time import utc_now
from app.modules.co_debug.schemas.scheduler import ScheduleExecutionResult
from app.modules.co_debug.services.metric_service import AcceptanceMetricService
from app.modules.co_debug.services.schedule_comparison_service import ScheduleComparisonService
from app.modules.co_debug.services.schedule_execution_service import ScheduleExecutionService
from app.modules.co_debug.services.scheduler_service import SchedulerService
from app.modules.co_debug.services.task_service import CoDebugTaskService
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.schemas.tasks import (
    BuildTaskRequest,
    DebugTaskRequest,
    ScheduleComparisonRequest,
    ScheduleExperimentRequest,
    ScheduleWorkloadSpec,
    TaskExecutionSpec,
)
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessResult
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


class BuildDebugProcessRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, int]] = []
        self.debug_executable_contents: str | None = None

    async def run(
        self,
        command,
        cwd,
        timeout_seconds,
        on_log,
        is_cancelled,
        on_process_started=None,
        cpu_core=None,
    ):
        self.calls.append((command, cwd, timeout_seconds))
        if on_process_started is not None:
            on_process_started(object())
        if command == ["build-tool"]:
            (cwd / "bin").mkdir()
            (cwd / "bin/program").write_text("built executable", encoding="utf-8")
        if command[0] == "gdb":
            self.debug_executable_contents = Path(command[2]).read_text(encoding="utf-8")
        return ProcessResult(
            exit_code=1 if command == ["fail-tool"] else 0,
            elapsed_ms=1,
        )


def _services(tmp_path) -> tuple[CoDebugTaskService, TaskService, WorkspaceService]:
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    runner = BuildDebugProcessRunner()
    task_service = TaskService(
        workspace_service=workspace_service,
        task_store=TaskStore(tmp_path / "tasks.sqlite3"),
        log_service=TaskLogService(100, tmp_path / "logs.sqlite3"),
        process_runner=runner,
        default_timeout_seconds=10,
    )
    scheduler_service = SchedulerService()
    schedule_execution_service = ScheduleExecutionService(process_runner=runner)
    schedule_comparison_service = ScheduleComparisonService(
        scheduler_service=scheduler_service,
        execution_service=schedule_execution_service,
        metric_service=AcceptanceMetricService(),
    )
    return (
        CoDebugTaskService(
            task_service=task_service,
            scheduler_service=scheduler_service,
            schedule_execution_service=schedule_execution_service,
            schedule_comparison_service=schedule_comparison_service,
        ),
        task_service,
        workspace_service,
    )


async def _project(workspace_service: WorkspaceService):
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("README.txt", "source")
    archive.seek(0)
    return await workspace_service.create_from_archive(
        UploadFile(file=archive, filename="project.zip")
    )


async def _wait_for_terminal(task_service: TaskService, task_id: UUID) -> TaskRecord:
    for _ in range(100):
        task = task_service.require_task(task_id)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("task did not finish")


@pytest.mark.asyncio
async def test_debug_accepts_absolute_successful_build_workspace_executable_path(
    tmp_path,
) -> None:
    co_debug, task_service, workspace_service = _services(tmp_path)
    project = await _project(workspace_service)
    runner = task_service.process_runner
    assert isinstance(runner, BuildDebugProcessRunner)

    try:
        build = await co_debug.create_build_task(
            BuildTaskRequest(project_id=project.id, command=["build-tool"])
        )
        assert (await _wait_for_terminal(task_service, build.id)).status == TaskStatus.SUCCEEDED
        build_workspace = workspace_service.resolve_task_workspace(project.id, build.id)
        assert (build_workspace / "bin/program").is_file()

        debug = await co_debug.create_debug_task(
            DebugTaskRequest(
                project_id=project.id,
                build_task_id=build.id,
                executable_path=str(build_workspace / "bin/program"),
                args=["--batch"],
                work_dir="bin",
            )
        )
        logical_command = [
            "gdb",
            "--interpreter=mi2",
            str(Path("bin/program")),
            "--batch",
        ]
        assert debug.command == logical_command
        completed = await _wait_for_terminal(task_service, debug.id)

        debug_command, debug_cwd, _ = runner.calls[1]
        debug_workspace = project.root_path / "tasks" / str(debug.id) / "workspace"
        debug_executable = Path(debug_command[2])
        assert completed.status == TaskStatus.SUCCEEDED
        assert completed.command == logical_command
        assert not Path(completed.command[2]).is_absolute()
        assert debug_command[:2] == ["gdb", "--interpreter=mi2"]
        assert debug_executable.is_absolute()
        assert debug_executable == debug_workspace / "bin/program"
        assert debug_executable != build_workspace / "bin/program"
        assert debug_cwd == debug_workspace / "bin"
        assert runner.debug_executable_contents == "built executable"
    finally:
        await task_service.shutdown(grace_seconds=0)
        task_service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_schedule_experiment_uses_managed_runtime_and_interprets_failure(
    tmp_path,
) -> None:
    co_debug, task_service, workspace_service = _services(tmp_path)
    project = await _project(workspace_service)
    runner = task_service.process_runner
    assert isinstance(runner, BuildDebugProcessRunner)

    try:
        created = await co_debug.create_schedule_experiment(
            ScheduleExperimentRequest(
                project_id=project.id,
                tasks=[
                    TaskExecutionSpec(
                        name="failure",
                        command=["fail-tool"],
                        estimated_ms=5,
                    )
                ],
                timeout_seconds=4,
                metadata={"case": "experiment"},
            )
        )
        completed = await _wait_for_terminal(task_service, created.id)

        assert created.command == ["app.modules.co_debug.scheduler.scheduler.plan_tasks"]
        assert completed.task_type == TaskType.SCHEDULE_EXPERIMENT
        assert completed.status == TaskStatus.FAILED
        assert completed.exit_code == 1
        assert completed.elapsed_ms == completed.result["execution"]["actual_makespan_ms"]
        assert completed.elapsed_ms >= 0
        assert completed.error == "one or more scheduled tasks failed"
        assert completed.metadata == {"case": "experiment"}
        assert completed.result["execution"]["all_succeeded"] is False
        assert runner.calls[0][2] == 4
    finally:
        await task_service.shutdown(grace_seconds=0)
        task_service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_schedule_experiment_does_not_apply_operation_timeout_to_whole_run(
    tmp_path,
) -> None:
    co_debug, task_service, workspace_service = _services(tmp_path)
    project = await _project(workspace_service)
    operation_timeout = 0.2
    observed_timeouts = []

    class SequentialExecutionService:
        async def execute(self, *, tasks, timeout_seconds, **_kwargs):
            observed_timeouts.append(timeout_seconds)
            for _ in tasks:
                async with asyncio.timeout(timeout_seconds):
                    await asyncio.sleep(0.12)
            return ScheduleExecutionResult(
                task_results=[],
                actual_makespan_ms=240,
                all_succeeded=True,
            )

    co_debug.schedule_execution_service = SequentialExecutionService()
    task_service.default_timeout_seconds = operation_timeout

    try:
        created = await co_debug.create_schedule_experiment(
            ScheduleExperimentRequest(
                project_id=project.id,
                tasks=[
                    TaskExecutionSpec(name="first", command=["first-tool"]),
                    TaskExecutionSpec(name="second", command=["second-tool"]),
                ],
            )
        )
        completed = await _wait_for_terminal(task_service, created.id)

        assert completed.status == TaskStatus.SUCCEEDED
        assert completed.elapsed_ms == 240
        assert observed_timeouts == [operation_timeout]
    finally:
        await task_service.shutdown(grace_seconds=0)
        task_service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_schedule_comparison_uses_managed_workspaces_and_interprets_success(
    tmp_path,
) -> None:
    co_debug, task_service, workspace_service = _services(tmp_path)
    project = await _project(workspace_service)
    runner = task_service.process_runner
    assert isinstance(runner, BuildDebugProcessRunner)

    try:
        created = await co_debug.create_schedule_comparison(
            ScheduleComparisonRequest(
                project_id=project.id,
                workloads=[
                    ScheduleWorkloadSpec(
                        name="comparison",
                        tasks=[
                            TaskExecutionSpec(
                                name="success",
                                command=["success-tool"],
                                estimated_ms=5,
                            )
                        ],
                    )
                ],
                timeout_seconds=4,
            )
        )
        completed = await _wait_for_terminal(task_service, created.id)

        assert created.command == [
            "app.modules.co_debug.services.schedule_comparison_service.compare"
        ]
        assert completed.task_type == TaskType.SCHEDULE_COMPARISON
        assert completed.status == TaskStatus.SUCCEEDED
        assert completed.exit_code == 0
        assert completed.error is None
        assert completed.result["workload_count"] == 1
        assert completed.result["all_tasks_succeeded"] is True
        workload_result = completed.result["workload_results"][0]
        assert completed.elapsed_ms == sum(
            workload_result[strategy]["execution"]["actual_makespan_ms"]
            for strategy in ("fifo", "optimized")
        )
        assert len(runner.calls) == 2
        assert runner.calls[0][1] != runner.calls[1][1]
        assert {call[2] for call in runner.calls} == {4}
    finally:
        await task_service.shutdown(grace_seconds=0)
        task_service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_debug_accepts_absolute_project_source_executable_path(tmp_path) -> None:
    co_debug, task_service, workspace_service = _services(tmp_path)
    project = await _project(workspace_service)
    source_executable = project.source_path / "bin/program"
    source_executable.parent.mkdir()
    source_executable.write_text("source executable", encoding="utf-8")
    runner = task_service.process_runner
    assert isinstance(runner, BuildDebugProcessRunner)

    try:
        debug = await co_debug.create_debug_task(
            DebugTaskRequest(
                project_id=project.id,
                executable_path=str(source_executable),
                args=["--batch"],
            )
        )
        logical_command = [
            "gdb",
            "--interpreter=mi2",
            str(Path("bin/program")),
            "--batch",
        ]
        assert debug.command == logical_command
        completed = await _wait_for_terminal(task_service, debug.id)

        debug_command, debug_cwd, _ = runner.calls[0]
        debug_workspace = project.root_path / "tasks" / str(debug.id) / "workspace"
        debug_executable = Path(debug_command[2])
        assert completed.status == TaskStatus.SUCCEEDED
        assert completed.command == logical_command
        assert not Path(completed.command[2]).is_absolute()
        assert debug_executable.is_absolute()
        assert debug_executable == debug_workspace / "bin/program"
        assert debug_executable != source_executable
        assert debug_cwd == debug_workspace
        assert runner.debug_executable_contents == "source executable"
    finally:
        await task_service.shutdown(grace_seconds=0)
        task_service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_debug_validation_errors_remain_synchronous(tmp_path) -> None:
    co_debug, task_service, workspace_service = _services(tmp_path)
    project = await _project(workspace_service)
    wrong_task = TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CO_DEBUG,
        project_id=project.id,
        task_type=TaskType.DEBUG,
        status=TaskStatus.SUCCEEDED,
        command=["test"],
        created_at=utc_now(),
    )
    task_service.task_store.save(wrong_task)
    failed_build = TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CO_DEBUG,
        project_id=project.id,
        task_type=TaskType.BUILD,
        status=TaskStatus.FAILED,
        command=["test"],
        created_at=utc_now(),
    )
    task_service.task_store.save(failed_build)
    other_project_build = TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CO_DEBUG,
        project_id=uuid4(),
        task_type=TaskType.BUILD,
        status=TaskStatus.SUCCEEDED,
        command=["test"],
        created_at=utc_now(),
    )
    task_service.task_store.save(other_project_build)

    try:
        with pytest.raises(
            AppError,
            match="build_task_id must reference a build task in the same project",
        ):
            await co_debug.create_debug_task(
                DebugTaskRequest(
                    project_id=project.id,
                    build_task_id=wrong_task.id,
                    executable_path="missing",
                )
            )

        with pytest.raises(
            AppError,
            match="build_task_id must reference a build task in the same project",
        ):
            await co_debug.create_debug_task(
                DebugTaskRequest(
                    project_id=project.id,
                    build_task_id=other_project_build.id,
                    executable_path="missing",
                )
            )

        with pytest.raises(AppError, match="build task must succeed before starting debug"):
            await co_debug.create_debug_task(
                DebugTaskRequest(
                    project_id=project.id,
                    build_task_id=failed_build.id,
                    executable_path="missing",
                )
            )

        with pytest.raises(AppError, match="debug executable does not exist: missing"):
            await co_debug.create_debug_task(
                DebugTaskRequest(project_id=project.id, executable_path="missing")
            )

        with pytest.raises(AppError, match="project path must stay inside"):
            await co_debug.create_debug_task(
                DebugTaskRequest(project_id=project.id, executable_path="../outside")
            )
    finally:
        await task_service.shutdown(grace_seconds=0)
        task_service.close_resources_when_idle()
