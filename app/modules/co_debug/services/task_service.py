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

    # 单策略实验返回一次规划及执行结果，不计算相对 FIFO 的改进率。
    async def create_schedule_experiment(
        self,
        request: ScheduleExperimentRequest,
    ) -> TaskRecord:
        # 允许直接使用工程文件，也允许把已保留的构建产物用作运行输入。
        # 有构建编号时在创建任务前校验，避免无效引用进入后台队列。
        self._require_build_task(request.project_id, request.build_task_id)

        async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
            # 平台传入统一日志、进度和取消回调，C 模块只组织本次实验。
            # 规划在后台执行，调用方拿到任务编号后可继续查询进度。
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
            # 工作区由平台从已选输入创建；不直接在上传目录中运行。
            # 执行沿用计划的核内顺序，不再按实时进度重新分配任务。
            execution = await self.schedule_execution_service.execute(
                plan=plan,
                tasks=request.tasks,
                cwd=context.create_workspace(),
                timeout_seconds=context.timeout_seconds,
                on_log=context.log,
                on_progress=context.report_progress,
                is_cancelled=context.is_cancelled,
            )
            # 子任务退出码决定外层成功状态；即使失败也返回完整的执行明细。
            succeeded = execution.all_succeeded
            return ManagedTaskResult(
                status=TaskStatus.SUCCEEDED if succeeded else TaskStatus.FAILED,
                result={
                    # 计划字段保留在顶层，实测结果独立放进 execution。
                    **plan.model_dump(mode="json"),
                    "execution": execution.model_dump(mode="json"),
                },
                exit_code=0 if succeeded else 1,
                # 外层耗时采用整轮实测值，便于和 execution 中的时间线核对。
                elapsed_ms=execution.actual_makespan_ms,
                error=None if succeeded else "one or more scheduled tasks failed",
            )

        # command 字段标记后台入口，实际调度由 execute 回调驱动。
        # 平台负责执行总时限、取消与状态持久化。
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
        # 批处理调试与普通对比共用执行器，但历史汇总需要可靠地区分二者。
        # 关联字段只能由调试入口填写，普通对比调用方不能用自定义 metadata 冒充。
        debug_metadata_keys = {
            "comparison_kind",
            "debug_workload_manifest",
            "build_task_id",
        }
        # 在构建来源和任务登记前就拒绝保留字段，避免留下误标的历史记录。
        if not _allow_debug_metadata and debug_metadata_keys.intersection(request.metadata):
            raise AppError("debug comparison metadata is reserved")
        self._require_build_task(request.project_id, request.build_task_id)

        async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
            context.report_progress(5, "schedule comparison started")
            # 传入工作区工厂而非已创建的目录，使每组两轮均能取得独立副本。
            # 同一对比任务持有取消状态，策略服务无需创建额外的平台任务。
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
            # 执行成功与合同达标分开：正常跑完但提速不足仍是成功的对比记录。
            # 前端从 summary 的分项字段读取指标结论，不使用任务状态替代。
            succeeded = summary.all_tasks_succeeded
            return ManagedTaskResult(
                status=TaskStatus.SUCCEEDED if succeeded else TaskStatus.FAILED,
                result=summary.model_dump(mode="json"),
                exit_code=0 if succeeded else 1,
                # 对比任务耗时累计各组两轮执行时间，不计入准备与规划。
                elapsed_ms=sum(
                    result.fifo.execution.actual_makespan_ms
                    + result.optimized.execution.actual_makespan_ms
                    for result in summary.workload_results
                ),
                error=None if succeeded else "one or more comparison tasks failed",
            )

        # build_task_id 是工作区的来源，不表示对比任务会重新构建工程。
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
        # 清单来自项目源文件；只有可执行产物按需从成功的构建快照读取。
        # 在调试入口把清单转换为统一调度输入，不另写一套双轮执行逻辑。
        # 这样重新构建不会改变测试清单，切换产物也不需要重新上传配置。
        manifest_path = self._safe_relative_path(request.manifest_path)
        raw_manifest = self.task_service.read_project_source_text(
            project_id=request.project_id, path=manifest_path
        )
        try:
            # 先按清单版本和结构校验，再解析相对路径与项目内产物。
            manifest = DebugWorkloadManifest.model_validate_json(raw_manifest)
        except ValidationError as exc:
            raise AppError("invalid debug workload manifest") from exc

        self._require_build_task(request.project_id, request.build_task_id)
        workloads: list[ScheduleWorkloadSpec] = []
        for workload in manifest.workloads:
            # 名称是单组内实测结果与计划的关联键；不同组可以复用名称。
            names = [job.name for job in workload.jobs]
            if len(names) != len(set(names)):
                raise AppError(f"debug job names must be unique in {workload.name}")
            # 工作目录是整组共享的输入，组内命令都以它作为相对路径基准。
            work_dir = self._safe_relative_path(workload.work_dir)
            tasks: list[TaskExecutionSpec] = []
            for job in workload.jobs:
                # 校验时把组目录与可执行路径拼成项目内路径；运行时以组目录为 cwd。
                # 因此传给运行器的可执行路径无需再重复拼接组目录。
                executable = self._safe_relative_path(job.executable_path)
                project_path = str(PurePosixPath(work_dir) / executable)
                found = self.task_service.find_process_source_file(
                    project_id=request.project_id,
                    path=project_path,
                    source_task_id=request.build_task_id,
                )
                # 在创建调度对比前检查产物，避免一轮跑完后才发现后续作业缺文件。
                if found is None:
                    raise AppError(f"debug executable does not exist: {project_path}")
                tasks.append(
                    # 使用结构化命令参数；断点、程序和入参来自通过校验的清单。
                    TaskExecutionSpec(
                        name=job.name,
                        command=self._batch_gdb_command(executable, job.breakpoint, job.args),
                        # 这只是首轮计划的估值；优化轮使用 FIFO 实际耗时。
                        estimated_ms=job.estimated_ms,
                        metadata={"debug_workload": workload.name},
                    )
                )
            # 一组清单对应一次 FIFO/优化双跑，作业顺序仍由清单决定。
            workloads.append(
                ScheduleWorkloadSpec(name=workload.name, work_dir=work_dir, tasks=tasks)
            )

        # 复用通用调度对比任务类型；历史查询通过专属元数据定位调试样本。
        return await self.create_schedule_comparison(
            ScheduleComparisonRequest(
                project_id=request.project_id,
                build_task_id=request.build_task_id,
                workloads=workloads,
                # 调试适配只转换作业输入，核心选择与超时仍由调用请求决定。
                core_ids=request.core_ids,
                timeout_seconds=request.timeout_seconds,
                metadata={
                    "comparison_kind": "debug-batch",
                    "debug_workload_manifest": manifest_path,
                    # 工作区通过请求字段选取；这里的编号供持久化结果回查。
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
        # 清单统一用 POSIX 风格路径，不允许 Windows 盘符或反斜杠。
        # 拒绝父目录跳转，后续仍由平台校验解析后的实际文件位置。
        path = PurePosixPath(value)
        if not value or "\\" in value or ":" in value or path.is_absolute() or ".." in path.parts:
            raise AppError("debug workload paths must be relative to the project")
        # 返回规范化后的拼写；组目录为当前目录时仍保留可用的相对路径。
        return str(path)

    @staticmethod
    def _batch_gdb_command(executable: str, breakpoint: str, args: list[str]) -> list[str]:
        # 两轮使用同一批处理步骤：运行到断点后继续至退出。
        # 关闭用户初始化文件和分页，避免本机交互设置影响重复测量。
        # 关闭断点后继续，避免同一断点再次命中而提前结束该次作业。
        # 参数列表直接交给运行器，不把程序参数拼成 shell 命令。
        # 使用被调试程序的退出结果，运行器据此记录该项作业是否成功。
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
        # 未提供编号时使用项目源目录；不自动选取某次历史构建。
        if build_task_id is None:
            return
        task = self.task_service.require_task(build_task_id)
        # 类型、所属项目和成功状态必须匹配，才能作为本次调度的构建来源。
        if task.project_id != project_id or task.task_type != TaskType.BUILD:
            raise AppError("build_task_id must reference a build task in the same project")
        # 运行中的构建还没有可供两轮一致读取的已完成结果。
        if task.status != TaskStatus.SUCCEEDED:
            raise AppError("build task must succeed before scheduling")

    @staticmethod
    def _gdb_command(executable: str, args: list[str]) -> list[str]:
        return ["gdb", "--interpreter=mi2", executable, *args]
