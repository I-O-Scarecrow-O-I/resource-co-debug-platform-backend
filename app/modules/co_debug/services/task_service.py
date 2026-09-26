from pathlib import PurePosixPath
from uuid import UUID

from pydantic import ValidationError

from app.core.errors import AppError
from app.modules.co_debug.schemas.debug_workloads import (
    DebugComparisonRequest,
    DebugWorkloadManifest,
)
from app.modules.co_debug.services.schedule_comparison_service import ScheduleComparisonService
from app.modules.co_debug.services.schedule_execution_service import ScheduleExecutionService
from app.modules.co_debug.services.scheduler_service import SchedulerService
from app.platform.domain.enums import TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.schemas.tasks import (
    BuildTaskRequest,
    DebugTaskRequest,
    ScheduleComparisonRequest,
    ScheduleExperimentRequest,
    ScheduleWorkloadSpec,
    TaskExecutionSpec,
)
from app.platform.services.task_execution import (
    ManagedTaskContext,
    ManagedTaskResult,
    PreparedProcess,
    TaskPreparationContext,
)
from app.platform.services.task_service import TaskService


class CoDebugTaskService:
    """Own co_debug build and GDB task orchestration."""

    def __init__(
        self,
        task_service: TaskService,
        scheduler_service: SchedulerService,
        schedule_execution_service: ScheduleExecutionService,
        schedule_comparison_service: ScheduleComparisonService,
    ) -> None:
        self.task_service = task_service
        self.scheduler_service = scheduler_service
        self.schedule_execution_service = schedule_execution_service
        self.schedule_comparison_service = schedule_comparison_service

    async def create_build_task(self, request: BuildTaskRequest) -> TaskRecord:
        return await self.task_service.create_process_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.BUILD,
            command=request.command,
            work_dir=request.work_dir,
            timeout_seconds=request.timeout_seconds,
            metadata=request.metadata,
            artifacts_on_success=True,
        )

    async def create_debug_task(self, request: DebugTaskRequest) -> TaskRecord:
        if request.build_task_id is not None:
            build_task = self.task_service.require_task(request.build_task_id)
            if (
                build_task.project_id != request.project_id
                or build_task.task_type != TaskType.BUILD
            ):
                raise AppError("build_task_id must reference a build task in the same project")
            if build_task.status != TaskStatus.SUCCEEDED:
                raise AppError("build task must succeed before starting debug")

        logical_executable = self.task_service.find_process_source_file(
            project_id=request.project_id,
            path=request.executable_path,
            source_task_id=request.build_task_id,
        )
        if logical_executable is None:
            raise AppError(f"debug executable does not exist: {request.executable_path}")
        logical_command = self._gdb_command(logical_executable, request.args)

        async def prepare(context: TaskPreparationContext) -> PreparedProcess:
            executable = context.resolve_path(logical_executable)
            if not executable.is_file():
                raise AppError(f"debug executable does not exist: {request.executable_path}")
            return PreparedProcess(
                command=self._gdb_command(str(executable), request.args),
                work_dir=request.work_dir,
                recorded_command=logical_command,
            )

        return await self.task_service.create_prepared_process_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.DEBUG,
            prepare=prepare,
            metadata=request.metadata,
            timeout_seconds=request.timeout_seconds,
            source_task_id=request.build_task_id,
            initial_command=logical_command,
        )

    async def create_schedule_experiment(
        self,
        request: ScheduleExperimentRequest,
    ) -> TaskRecord:
        self._require_build_task(request.project_id, request.build_task_id)

        async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
            context.report_progress(10, "schedule experiment started")
            plan = self.scheduler_service.create_plan(
                task_id=context.task_id,
                strategy=request.strategy,
                tasks=request.tasks,
                on_log=context.log,
                on_progress=context.report_progress,
                is_cancelled=context.is_cancelled,
                core_ids=request.core_ids,
            )
            execution = await self.schedule_execution_service.execute(
                plan=plan,
                tasks=request.tasks,
                cwd=context.create_workspace(),
                timeout_seconds=context.timeout_seconds,
                on_log=context.log,
                on_progress=context.report_progress,
                is_cancelled=context.is_cancelled,
            )
            succeeded = execution.all_succeeded
            return ManagedTaskResult(
                status=TaskStatus.SUCCEEDED if succeeded else TaskStatus.FAILED,
                result={
                    **plan.model_dump(mode="json"),
                    "execution": execution.model_dump(mode="json"),
                },
                exit_code=0 if succeeded else 1,
                elapsed_ms=execution.actual_makespan_ms,
                error=None if succeeded else "one or more scheduled tasks failed",
            )

        return await self.task_service.create_managed_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.SCHEDULE_EXPERIMENT,
            command=["app.modules.co_debug.scheduler.scheduler.plan_tasks"],
            execute=execute,
            metadata=request.metadata,
            timeout_seconds=request.timeout_seconds,
            source_task_id=request.build_task_id,
        )

    async def create_schedule_comparison(
        self,
        request: ScheduleComparisonRequest,
        *,
        _allow_debug_metadata: bool = False,
    ) -> TaskRecord:
        debug_metadata_keys = {
            "comparison_kind",
            "debug_workload_manifest",
            "build_task_id",
        }
        if not _allow_debug_metadata and debug_metadata_keys.intersection(request.metadata):
            raise AppError("debug comparison metadata is reserved")
        self._require_build_task(request.project_id, request.build_task_id)

        async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
            context.report_progress(5, "schedule comparison started")
            summary = await self.schedule_comparison_service.compare(
                task_id=context.task_id,
                workloads=request.workloads,
                core_ids=request.core_ids,
                timeout_seconds=context.timeout_seconds,
                on_log=context.log,
                on_progress=context.report_progress,
                is_cancelled=context.is_cancelled,
                workspace_factory=context.create_workspace,
            )
            succeeded = summary.all_tasks_succeeded
            return ManagedTaskResult(
                status=TaskStatus.SUCCEEDED if succeeded else TaskStatus.FAILED,
                result=summary.model_dump(mode="json"),
                exit_code=0 if succeeded else 1,
                elapsed_ms=sum(
                    result.fifo.execution.actual_makespan_ms
                    + result.optimized.execution.actual_makespan_ms
                    for result in summary.workload_results
                ),
                error=None if succeeded else "one or more comparison tasks failed",
            )

        return await self.task_service.create_managed_task(
            module=request.module,
            project_id=request.project_id,
            task_type=TaskType.SCHEDULE_COMPARISON,
            command=["app.modules.co_debug.services.schedule_comparison_service.compare"],
            execute=execute,
            metadata=request.metadata,
            timeout_seconds=request.timeout_seconds,
            source_task_id=request.build_task_id,
        )

    async def create_debug_schedule_comparison(
        self, request: DebugComparisonRequest
    ) -> TaskRecord:
        manifest_path = self._safe_relative_path(request.manifest_path)
        raw_manifest = self.task_service.read_project_source_text(
            project_id=request.project_id, path=manifest_path
        )
        try:
            manifest = DebugWorkloadManifest.model_validate_json(raw_manifest)
        except ValidationError as exc:
            raise AppError("invalid debug workload manifest") from exc

        self._require_build_task(request.project_id, request.build_task_id)
        workloads: list[ScheduleWorkloadSpec] = []
        for workload in manifest.workloads:
            names = [job.name for job in workload.jobs]
            if len(names) != len(set(names)):
                raise AppError(f"debug job names must be unique in {workload.name}")
            work_dir = self._safe_relative_path(workload.work_dir)
            tasks: list[TaskExecutionSpec] = []
            for job in workload.jobs:
                executable = self._safe_relative_path(job.executable_path)
                project_path = str(PurePosixPath(work_dir) / executable)
                found = self.task_service.find_process_source_file(
                    project_id=request.project_id,
                    path=project_path,
                    source_task_id=request.build_task_id,
                )
                if found is None:
                    raise AppError(f"debug executable does not exist: {project_path}")
                tasks.append(
                    TaskExecutionSpec(
                        name=job.name,
                        command=self._batch_gdb_command(executable, job.breakpoint, job.args),
                        estimated_ms=job.estimated_ms,
                        metadata={"debug_workload": workload.name},
                    )
                )
            workloads.append(
                ScheduleWorkloadSpec(name=workload.name, work_dir=work_dir, tasks=tasks)
            )

        return await self.create_schedule_comparison(
            ScheduleComparisonRequest(
                project_id=request.project_id,
                build_task_id=request.build_task_id,
                workloads=workloads,
                core_ids=request.core_ids,
                timeout_seconds=request.timeout_seconds,
                metadata={
                    "comparison_kind": "debug-batch",
                    "debug_workload_manifest": manifest_path,
                    "build_task_id": (
                        str(request.build_task_id)
                        if request.build_task_id is not None
                        else None
                    ),
                },
            ),
            _allow_debug_metadata=True,
        )

    @staticmethod
    def _safe_relative_path(value: str) -> str:
        path = PurePosixPath(value)
        if not value or "\\" in value or ":" in value or path.is_absolute() or ".." in path.parts:
            raise AppError("debug workload paths must be relative to the project")
        return str(path)

    @staticmethod
    def _batch_gdb_command(executable: str, breakpoint: str, args: list[str]) -> list[str]:
        return [
            "gdb", "--nx", "--quiet", "--batch", "--return-child-result",
            "-ex", "set pagination off",
            "-ex", f"break {breakpoint}",
            "-ex", "run",
            "-ex", "disable breakpoints",
            "-ex", "continue",
            "--args", f"./{executable}", *args,
        ]

    def _require_build_task(self, project_id: UUID, build_task_id: UUID | None) -> None:
        if build_task_id is None:
            return
        task = self.task_service.require_task(build_task_id)
        if task.project_id != project_id or task.task_type != TaskType.BUILD:
            raise AppError("build_task_id must reference a build task in the same project")
        if task.status != TaskStatus.SUCCEEDED:
            raise AppError("build task must succeed before scheduling")

    @staticmethod
    def _gdb_command(executable: str, args: list[str]) -> list[str]:
        return ["gdb", "--interpreter=mi2", executable, *args]
