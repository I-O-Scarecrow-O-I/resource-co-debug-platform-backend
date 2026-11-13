from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from uuid import UUID

from app.core.errors import AppError, CancellationRequested
from app.modules.co_debug.schemas.scheduler import (
    ScheduleComparisonSummary,
    StrategyRunResult,
    WorkloadComparisonResult,
)
from app.modules.co_debug.services.metric_service import AcceptanceMetricService
from app.modules.co_debug.services.schedule_execution_service import ScheduleExecutionService
from app.modules.co_debug.services.scheduler_service import SchedulerService
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import ScheduleWorkloadSpec

LogCallback = Callable[[str, str], None]
ProgressCallback = Callable[[int, str], None]
CancelCheck = Callable[[], bool]


class ScheduleComparisonService:
    def __init__(
        self,
        scheduler_service: SchedulerService,
        execution_service: ScheduleExecutionService,
        metric_service: AcceptanceMetricService,
    ) -> None:
        self.scheduler_service = scheduler_service
        self.execution_service = execution_service
        self.metric_service = metric_service

    # 每份负载用两份工作区依次跑基线和优化，后者依赖前者的观测耗时。
    # 工作区工厂由调用方注入，目录生命周期由上层管理。
    async def compare(
        self,
        task_id: UUID,
        workloads: list[ScheduleWorkloadSpec],
        core_ids: list[int] | None,
        timeout_seconds: int,
        on_log: LogCallback,
        on_progress: ProgressCallback,
        is_cancelled: CancelCheck,
        workspace_factory: Callable[[], Path],
    ) -> ScheduleComparisonSummary:
        # 对比为每份负载固定安排两轮，进度分段以实际轮次而不是任务数计算。
        workload_results: list[WorkloadComparisonResult] = []
        total_runs = len(workloads) * 2
        run_index = 0

        # 对比以负载为单位推进；取消后不能启动下一份基线。
        # 每份负载内部必须先有基线观测再规划优化轮。
        # 不同负载也在循环中逐份推进，不让它们同时争用核心而污染比较。
        for workload in workloads:
            # 当前轮的进程取消另由执行器检查；这里防止取消穿过负载边界后启动新的副本。
            if is_cancelled():
                raise CancellationRequested()

            # 每个工作目录相对于该轮新副本解析，不能把基线生成的文件传给优化轮。
            on_log(f"comparison workload started: {workload.name}", "co_debug.comparison")
            # 基线先执行，保留原始提交顺序和原始估时供计划展示。
            fifo_cwd = self._resolve_work_dir(workspace_factory(), workload.work_dir)
            # FIFO 先使用原估计值出计划，真实执行产生优化轮需要的观测数据。
            # 两轮沿用同一个平台 task_id 以统一日志和取消。
            # 轮次自身通过策略、负载名及进度区段区分。
            fifo = await self._run_strategy(
                task_id=task_id,
                workload=workload,
                strategy=SchedulerStrategy.FIFO_BASELINE,
                core_ids=core_ids,
                cwd=fifo_cwd,
                timeout_seconds=timeout_seconds,
                run_index=run_index,
                total_runs=total_runs,
                on_log=on_log,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
            )
            # 只有 FIFO 执行返回后才推进轮次；抛错或取消不会进入重排。
            run_index += 1
            # 实际耗时只替换优化输入的估计值，命令和工作目录保持一致。
            # 重新创建工作区，避免读取基线命令在副本里生成的文件。
            profiled_workload = self._apply_fifo_measurements(workload, fifo)
            # 这条日志说明估计成本已更新；真实优化结果仍需要下一轮完成才可判断。
            on_log(
                f"FIFO timings applied as optimized estimates for {workload.name}",
                "co_debug.comparison",
            )
            # 优化轮获得同样结构的独立副本和同一组核心，减少与策略无关的文件状态差异。
            optimized_cwd = self._resolve_work_dir(workspace_factory(), workload.work_dir)
            optimized = await self._run_strategy(
                task_id=task_id,
                workload=profiled_workload,
                strategy=SchedulerStrategy.RESOURCE_AWARE,
                core_ids=core_ids,
                cwd=optimized_cwd,
                timeout_seconds=timeout_seconds,
                run_index=run_index,
                total_runs=total_runs,
                on_log=on_log,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
            )
            # 优化轮结束后同一份负载才具有完整的基线与对照证据。
            run_index += 1

            # 耗时离散度来自 FIFO 的单任务观测；收益来自两个阶段的实际总历时。
            # 不要将计划中的估算总历时混入验收统计。
            duration_spread_rate = self.metric_service.duration_spread_rate(
                [result.elapsed_ms for result in fifo.execution.task_results]
            )
            # 改进率以 FIFO 实测墙钟为分母；优化较慢时结果可为负。
            improvement_rate = self.metric_service.improvement_rate(
                fifo.execution.actual_makespan_ms,
                optimized.execution.actual_makespan_ms,
            )
            # 保留单样本指标和门槛标志，汇总失败时还能定位不合格的负载。
            # 离散度和改进率分别写门槛标志，未达到收益并不说明工作负载本身不合格。
            workload_results.append(
                WorkloadComparisonResult(
                    workload_name=workload.name,
                    cost_estimation_source="FIFO_ACTUAL_DURATION",
                    fifo=fifo,
                    optimized=optimized,
                    duration_spread_rate=duration_spread_rate,
                    improvement_rate=improvement_rate,
                    meets_duration_spread_requirement=(
                        duration_spread_rate
                        >= self.metric_service.REQUIRED_DURATION_SPREAD_RATE
                    ),
                    meets_improvement_requirement=(
                        improvement_rate >= self.metric_service.REQUIRED_IMPROVEMENT_RATE
                    ),
                )
            )
            # 单份负载完成日志在保存成对结果后发出。
            # 它表示两轮已经返回，不能等同于所有命令零退出或达到门槛。
            on_log(f"comparison workload finished: {workload.name}", "co_debug.comparison")

        # 汇总门槛分别检查样本数、负载离散度、执行成功和平均收益。
        # 单个负载的收益标记仍保留给调用方定位偏差。
        average_improvement_rate = self.metric_service.average_improvement_rate(
            [result.improvement_rate for result in workload_results]
        )
        # 样本数需要恰好等于合同数量；接口允许少量负载做功能观察。
        has_required_workload_count = (
            len(workload_results) == self.metric_service.REQUIRED_WORKLOAD_COUNT
        )
        # 耗时差异是先决条件，不由观察到的加速幅度反推。
        all_duration_spreads_eligible = all(
            result.meets_duration_spread_requirement for result in workload_results
        )
        # 即使非零退出使命令更快，也不能把这组对比当作合格验收数据。
        all_tasks_succeeded = all(
            result.fifo.execution.all_succeeded and result.optimized.execution.all_succeeded
            for result in workload_results
        )
        # 合同结论看每份负载等权的平均改进，并另外要求其他门槛成立。
        meets_average_improvement_requirement = (
            average_improvement_rate >= self.metric_service.REQUIRED_IMPROVEMENT_RATE
        )
        # 汇总同时保留原始轮次证据，调用方无需由最终布尔值推断失败原因。
        # 合同布尔值由四项独立条件合取，不要求逐份达到平均收益阈值。
        # 若需定位单份退化，读取每份 workload 的改进率。
        return ScheduleComparisonSummary(
            workload_results=workload_results,
            workload_count=len(workload_results),
            average_improvement_rate=average_improvement_rate,
            has_required_workload_count=has_required_workload_count,
            all_duration_spreads_eligible=all_duration_spreads_eligible,
            all_tasks_succeeded=all_tasks_succeeded,
            meets_average_improvement_requirement=meets_average_improvement_requirement,
            meets_contract_target=(
                has_required_workload_count
                and all_duration_spreads_eligible
                and all_tasks_succeeded
                and meets_average_improvement_requirement
            ),
        )

    @staticmethod
    def _resolve_work_dir(workspace: Path, work_dir: str) -> Path:
        # 同时识别 Windows 锚点和当前平台的绝对路径，避免路径在迁移后变成外部目录。
        # 解析后的路径必须在本次副本内且已存在，防止链接或父目录跳转逃逸。
        # 相对语法验证后还需 resolve，避免目录链接把子进程引到副本外。
        candidate = Path(work_dir)
        windows_path = PureWindowsPath(work_dir)
        # 同时识别 Windows 路径锚点，避免跨平台传输的盘符路径被当作普通相对目录。
        if not work_dir or candidate.is_absolute() or windows_path.anchor:
            raise AppError("work_dir must be relative to the project workspace")
        # 解析路径后再检查目录类型，避免把不存在或普通文件的位置当作有效 cwd。
        # 报错发生在对应策略启动之前。
        root = workspace.resolve()
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_dir():
            raise AppError(f"work_dir is outside the project or does not exist: {work_dir}")
        return resolved

    def _apply_fifo_measurements(
        self,
        workload: ScheduleWorkloadSpec,
        fifo: StrategyRunResult,
    ) -> ScheduleWorkloadSpec:
        # 名称是计划和执行结果之间的连接键，按同批任务逐一替换估时。
        # 毫秒取整可能得到零，用 1 毫秒作为最小规划输入。
        # 毫秒尺度下极短命令可能量化成零，估时只对此做下限保护。
        # 实际比较指标仍读原执行结果，不用修正后的成本覆盖观测。
        measured_durations = {
            result.task_name: max(result.elapsed_ms, 1)
            for result in fifo.execution.task_results
        }
        # 用副本替换估时，不修改原请求对象。
        # 基线计划继续保留原估时供比对。
        profiled_tasks = [
            task.model_copy(update={"estimated_ms": measured_durations[task.name]})
            for task in workload.tasks
        ]
        # 只生成用于优化轮的新任务规格。
        # 工作目录和任务命令来自原负载，以免把修改命令本身算成策略收益。
        return ScheduleWorkloadSpec(
            name=workload.name,
            work_dir=workload.work_dir,
            tasks=profiled_tasks,
        )

    async def _run_strategy(
        self,
        task_id: UUID,
        workload: ScheduleWorkloadSpec,
        strategy: SchedulerStrategy,
        core_ids: list[int] | None,
        cwd: Path,
        timeout_seconds: int,
        run_index: int,
        total_runs: int,
        on_log: LogCallback,
        on_progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> StrategyRunResult:
        # 为两种策略分别分配父任务进度区间，执行占该段的主要部分。
        # 保留头尾进度给比较任务本身的准备和完成。
        # 分段进度仅用于展示，不决定过程是否提前停止。
        # 每轮都按同一规则划分规划与执行区间。
        segment_start = 5 + round(run_index * 90 / total_runs)
        segment_end = 5 + round((run_index + 1) * 90 / total_runs)
        # 规划占该轮进度的一小段，预留其余区间给按实际完成数报告的执行器。
        plan_end = segment_start + max(1, round((segment_end - segment_start) * 0.2))
        on_progress(
            segment_start,
            f"planning {workload.name} with {strategy.value}",
        )
        # 规划器只接收任务规格；真实路径由执行阶段使用。
        plan = self.scheduler_service.create_plan(
            task_id=task_id,
            strategy=strategy,
            tasks=workload.tasks,
            on_log=on_log,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
            core_ids=core_ids,
            progress_start=segment_start,
            progress_end=plan_end,
        )
        # 执行器复用刚生成的分配并沿用同一核心集合，不再对命令排序。
        # 运行器消费计划内保存的核映射和队列顺序。
        # 调用方拿到执行结果后可以区分计划预测与实际亲和性，不把两者混写。
        execution = await self.execution_service.execute(
            plan=plan,
            tasks=workload.tasks,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            on_log=on_log,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
            progress_start=plan_end,
            progress_end=segment_end,
        )
        # 成对返回计划与执行，使结果能分辨估计失准和实际排队开销。
        return StrategyRunResult(plan=plan, execution=execution)
