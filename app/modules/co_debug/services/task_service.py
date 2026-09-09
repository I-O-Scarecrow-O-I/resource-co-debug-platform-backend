from app.core.errors import AppError
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
        )

    async def create_schedule_comparison(
        self,
        request: ScheduleComparisonRequest,
    ) -> TaskRecord:
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
        )

    @staticmethod
    def _gdb_command(executable: str, args: list[str]) -> list[str]:
        return ["gdb", "--interpreter=mi2", executable, *args]
