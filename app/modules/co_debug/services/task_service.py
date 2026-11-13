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
        # 此门面负责把业务请求转成三类平台执行契约；不直接持有用户进程句柄。
        self.task_service = task_service
        self.scheduler_service = scheduler_service
        self.schedule_execution_service = schedule_execution_service
        self.schedule_comparison_service = schedule_comparison_service

    # 普通构建只提交平台进程任务，副本、进程、超时和产物发布使用通用流程。
    # 普通构建交给平台运行进程；成功时登记工作区产物，供调试或调度作为来源。
    async def create_build_task(self, request: BuildTaskRequest) -> TaskRecord:
        # 普通构建使用直接进程任务，成功时才发布固定工作区内的产物。
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

    # 此处是一次性调试任务入口；交互式会话另由专门服务维护。
    # 这个入口准备 GDB 进程任务；交互会话及其命令路由由独立服务持有。
    async def create_debug_task(self, request: DebugTaskRequest) -> TaskRecord:
        # 引用构建结果时先核对任务类型、工程和终态，避免使用其他任务的副本。
        # 选填构建来源时，后续查找和执行都以该构建副本为基准。
        if request.build_task_id is not None:
            build_task = self.task_service.require_task(request.build_task_id)
            # 项目归属与任务类型同时检查；同名文件不能跨项目提供构建来源。
            if (
                build_task.project_id != request.project_id
                or build_task.task_type != TaskType.BUILD
            ):
                raise AppError("build_task_id must reference a build task in the same project")
            # 只有成功构建才能作为调试输入；失败留下的半成品不作为可执行候选。
            if build_task.status != TaskStatus.SUCCEEDED:
                raise AppError("build task must succeed before starting debug")

        # 提交前检查逻辑路径，执行前再在新副本中确认可执行文件仍存在。
        # 提交前按项目或显式构建来源确认逻辑目标，任务记录无需暴露宿主路径。
        # 先以来源快照确认逻辑文件名，供任务记录与后续副本解析复用。
        logical_executable = self.task_service.find_process_source_file(
            project_id=request.project_id,
            path=request.executable_path,
            source_task_id=request.build_task_id,
        )
        # 提交阶段没有定位到文件时立即拒绝，不启动 GDB 试探宿主路径。
        if logical_executable is None:
            raise AppError(f"debug executable does not exist: {request.executable_path}")
        # 记录逻辑命令供前端显示，实际运行时由准备器绑定当前任务副本。
        logical_command = self._gdb_command(logical_executable, request.args)

        # 进程启动前在平台准备的副本中重新解析可执行文件。
        # 记录的命令保留逻辑路径，与实际执行 argv 分开。
        # 准备函数在平台创建私有副本后才运行；它不直接读取旧构建目录作为执行位置。
        async def prepare(context: TaskPreparationContext) -> PreparedProcess:
            # 将提交时得到的逻辑名重新绑定本任务副本，避免用原目录的绝对路径运行。
            executable = context.resolve_path(logical_executable)
            if not executable.is_file():
                raise AppError(f"debug executable does not exist: {request.executable_path}")
            # 保留独立的逻辑命令用于展示；实际参数使用副本里的绝对可执行文件位置。
            return PreparedProcess(
                command=self._gdb_command(str(executable), request.args),
                work_dir=request.work_dir,
                recorded_command=logical_command,
            )

        # 提交准备式 DEBUG 任务并带上来源 ID，平台决定副本复制和日志收尾。
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

    # 单策略实验把规划与执行结果放进同一条平台记录，方便按任务关联。
    async def create_schedule_experiment(
        self,
        request: ScheduleExperimentRequest,
    ) -> TaskRecord:
        # 可选构建产物决定执行工作区的来源，不改变调度算法所接收的工作项。
        # 构建引用验证先于创建实验任务，避免后台再发现跨项目或失败构建。
        self._require_build_task(request.project_id, request.build_task_id)

        async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
            # 实验先运行纯规划阶段，规划失败时不会创建用于子进程执行的工作区。
            context.report_progress(10, "schedule experiment started")
            # 先生成队列分配，再把计划交给执行器；两个阶段共用任务取消状态。
            # 先形成策略计划，再在隔离副本中运行计划。
            # 估算信息用于分配，实际耗时由执行器独立记录。
            plan = self.scheduler_service.create_plan(
                task_id=context.task_id,
                strategy=request.strategy,
                tasks=request.tasks,
                on_log=context.log,
                on_progress=context.report_progress,
                is_cancelled=context.is_cancelled,
                core_ids=request.core_ids,
            )
            # 运行使用独立副本；调度策略不会改变工程源文件。
            # 计划与命令规格同时交给执行器；执行器按计划重建每核队列。
            execution = await self.schedule_execution_service.execute(
                plan=plan,
                tasks=request.tasks,
                cwd=context.create_workspace(),
                timeout_seconds=context.timeout_seconds,
                on_log=context.log,
                on_progress=context.report_progress,
                is_cancelled=context.is_cancelled,
            )
            # 只要有子进程非零退出，实验任务即失败，但保留各分配和执行证据。
            # 单策略结果同时保留计划和实际执行；任一子任务失败则平台任务失败。
            # 失败仍返回执行证据，便于查看具体工作项。
            # 所有真实进程的退出结果决定实验成败，计划是否产生只表示第一阶段完成。
            succeeded = execution.all_succeeded
            # 任务结果中保留计划字段和实测数据，即使某个命令以非零退出仍可诊断。
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

        # 记录用命令是逻辑入口名称；调度器由 Python 调用，不会作为独立工具执行。
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

    # 普通比较与调试比较共用执行流程，来源标签仅由调试入口生成。
    async def create_schedule_comparison(
        self,
        request: ScheduleComparisonRequest,
        *,
        _allow_debug_metadata: bool = False,
    ) -> TaskRecord:
        # 调试来源的元数据只由已校验 manifest 的入口构造。
        # 普通比较不能靠自定义 metadata 冒充可纳入调试历史汇总的任务。
        # 这些元数据属于平台确认过的批量调试来源，普通请求不能伪造后混入历史汇总。
        debug_metadata_keys = {
            "comparison_kind",
            "debug_workload_manifest",
            "build_task_id",
        }
        # 拒绝普通请求伪造调试标签，历史汇总可以据此区分样本来源。
        # 校验保留键时允许其余自定义元数据；只有内部构造的调试比较可写这些字段。
        if not _allow_debug_metadata and debug_metadata_keys.intersection(request.metadata):
            raise AppError("debug comparison metadata is reserved")
        self._require_build_task(request.project_id, request.build_task_id)

        # 通过平台副本工厂为每轮策略创建独立输入，避免前一轮文件写入影响对照。
        # 比较服务通过工作区工厂获得平台副本。
        # 同一请求的各轮执行不直接改动项目或构建来源。
        # 比较服务只接收受管执行上下文提供的能力，实际副本由每轮按需申请。
        async def execute(context: ManagedTaskContext) -> ManagedTaskResult:
            context.report_progress(5, "schedule comparison started")
            # 进度、日志和取消都绑定同一任务；各轮结果累积在同一个比较响应。
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
            # 平台任务成功表示各命令运行成功；收益和样本是否满足门槛是独立结论。
            # 任务成功取决于执行是否都成功，提升阈值和耗时分布另外表达。
            # 任务执行成功只要求各轮真实命令成功。
            # 改进率未达门槛仍可交付有效实验结果。
            succeeded = summary.all_tasks_succeeded
            # 把全部轮次与门槛检查写进任务结果，让调用方区分实验失败和收益不足。
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

        # 与独立实验一样，平台记录描述逻辑入口，而非待传给系统 shell 的命令行。
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

    # 调试比较从工程清单产生受控的批处理 GDB 负载，再交给通用对比。
    # 调试批处理比较由项目声明的作业生成 GDB 命令。
    # 入口不接受任意调度比较任务来替代调试样本。
    async def create_debug_schedule_comparison(
        self, request: DebugComparisonRequest
    ) -> TaskRecord:
        # 清单来自导入源树，构建副本只作为可执行文件来源。
        # manifest 从项目源读取；build_task_id 若存在只影响随后查找和执行二进制的来源。
        # 调试工作负载从上传工程读取。
        # 请求引用相对清单位置，不能直接提供任意 GDB 脚本。
        manifest_path = self._safe_relative_path(request.manifest_path)
        # 读取由平台限制大小和编码；本模块仅解释返回的 JSON 文本。
        raw_manifest = self.task_service.read_project_source_text(
            project_id=request.project_id, path=manifest_path
        )
        # 先按版本化模型验证 manifest，再解析文件位置，防止任意字段绕过作业约束。
        # 先校验完整清单结构，再从每条作业推导真实子进程规格。
        try:
            manifest = DebugWorkloadManifest.model_validate_json(raw_manifest)
        except ValidationError as exc:
            raise AppError("invalid debug workload manifest") from exc

        # 可执行文件选择仍受同项目成功构建的约束，与清单读取来源分开。
        self._require_build_task(request.project_id, request.build_task_id)
        workloads: list[ScheduleWorkloadSpec] = []
        # 按工作负载检查任务名唯一性；后续按名称对应 FIFO 实测估时。
        # 作业名称在同一负载中作为标识，重复名称会使执行结果无法可靠对应。
        # 每份工作负载单独校验任务名，避免计划与执行结果之间出现歧义连接。
        for workload in manifest.workloads:
            names = [job.name for job in workload.jobs]
            if len(names) != len(set(names)):
                raise AppError(f"debug job names must be unique in {workload.name}")
            # 逐个约束工作目录和相对可执行路径。
            # 查找时组合项目相对路径，运行时由负载工作目录和相对命令共同定位。
            # 先规范化工作目录，再把每个可执行路径拼到相同相对根下校验。
            work_dir = self._safe_relative_path(workload.work_dir)
            tasks: list[TaskExecutionSpec] = []
            # 逐项校验目录和程序路径，并提前确认构建副本里确实有对应文件。
            for job in workload.jobs:
                # 清单里的文件名必须在对应构建或工程来源能定位；不能让 GDB 自行搜索 PATH 中的同名目标。
                executable = self._safe_relative_path(job.executable_path)
                project_path = str(PurePosixPath(work_dir) / executable)
                # 用于存在性校验的路径包含工作目录；实际命令在该目录中使用原来的相对文件名。
                found = self.task_service.find_process_source_file(
                    project_id=request.project_id,
                    path=project_path,
                    source_task_id=request.build_task_id,
                )
                # 任一作业缺少可执行文件时拒绝整个请求，避免出现少于清单定义的对比负载。
                if found is None:
                    raise AppError(f"debug executable does not exist: {project_path}")
                # 将清单任务转成固定 GDB 命令规格；估时和负载身份随任务保留。
                # 估时传给调度器用于分配，最终结果仍以真实子进程执行为准。
                # 批处理 GDB 命令由平台定义，调用方只提供经过校验的断点和目标参数。
                tasks.append(
                    TaskExecutionSpec(
                        name=job.name,
                        command=self._batch_gdb_command(executable, job.breakpoint, job.args),
                        estimated_ms=job.estimated_ms,
                        metadata={"debug_workload": workload.name},
                    )
                )
            # 调试作业复用通用调度规格，从而使用同样的 FIFO 和优化测量口径。
            workloads.append(
                ScheduleWorkloadSpec(name=workload.name, work_dir=work_dir, tasks=tasks)
            )

        # 内部入口允许写入受保留的来源字段，历史聚合可回溯构建与清单。
        # 复用通用比较生命周期，并写入服务产生的调试来源标识。
        # 历史汇总据此区分真实 GDB 作业与一般命令比较。
        # 内部入口填充可信来源标签，再把已经构造的任务交给通用比较器。
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
    # 调试清单统一使用相对 POSIX 路径，拒绝跨平台锚点和父目录回退。
    # 先限制跨平台的路径语法；真实归属与存在性还需平台解析。
    def _safe_relative_path(value: str) -> str:
        # 使用 POSIX 路径语义解析交付物，即使后端运行在 Windows。
        # 拒绝驱动器和反斜杠，避免宿主重新解释路径。
        path = PurePosixPath(value)
        # 拒绝路径中的父级组件，防止清单位置或工作目录跨过所属工程。
        if not value or "\\" in value or ":" in value or path.is_absolute() or ".." in path.parts:
            raise AppError("debug workload paths must be relative to the project")
        return str(path)

    @staticmethod
    # 命令仅用于短批处理调试作业，不产生供后续交互的会话注册。
    def _batch_gdb_command(executable: str, breakpoint: str, args: list[str]) -> list[str]:
        # 忽略本地 GDB 初始化文件并使用批处理模式，固定为断点、运行和继续。
        # 将被调试程序的返回结果交给执行器记录。
        # 禁用用户启动脚本并启用批处理，避免同一负载依赖个人 GDB 配置或人工输入。
        return [
            "gdb", "--nx", "--quiet", "--batch", "--return-child-result",
            "-ex", "set pagination off",
            "-ex", f"break {breakpoint}",
            "-ex", "run",
            "-ex", "disable breakpoints",
            "-ex", "continue",
            "--args", f"./{executable}", *args,
        ]

    # 允许不依赖构建；提供构建 ID 时只能引用本项目成功的构建任务。
    def _require_build_task(self, project_id: UUID, build_task_id: UUID | None) -> None:
        # 没有显式构建任务时默认使用项目工作区；提供时必须是同项目成功构建。
        # 无构建 ID 时沿用工程内容；不会自动选最近一次构建而改变输入基线。
        if build_task_id is None:
            return
        # 指定构建时必须读取真实记录，不能依赖请求元数据中的自报状态。
        task = self.task_service.require_task(build_task_id)
        if task.project_id != project_id or task.task_type != TaskType.BUILD:
            raise AppError("build_task_id must reference a build task in the same project")
        # 终态成功仍不保证文件存在，调用者会按各作业路径再核对来源。
        if task.status != TaskStatus.SUCCEEDED:
            raise AppError("build task must succeed before scheduling")

    @staticmethod
    def _gdb_command(executable: str, args: list[str]) -> list[str]:
        return ["gdb", "--interpreter=mi2", executable, *args]
