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


# 同一负载先跑 FIFO 再跑资源感知策略，分别保存计划和实测结果。
# 对比使用同一组任务和核心，优化轮仅以基线测得的时长更新任务估值。
# 两轮按顺序运行，不让不同策略的进程同时竞争同一组核心。
# 不假设第二轮必然更快；若资源条件或测量波动导致更慢，保留负改进率。
# 整轮时间只统计执行阶段，工作区创建和规划不纳入策略耗时。
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
        # 保存工作负载的提交顺序，结果可与测试清单逐组核对。
        workload_results: list[WorkloadComparisonResult] = []
        # 每个样本有两轮运行；按轮而非按单个子任务分配总体进度。
        # 样本内部任务数可以不同，不会挤占其他样本的进度区间。
        total_runs = len(workloads) * 2
        run_index = 0

        for workload in workloads:
            # 若调用方在两组之间取消，避免为下一组再创建工作区。
            if is_cancelled():
                raise CancellationRequested()

            on_log(f"comparison workload started: {workload.name}", "co_debug.comparison")
            # 每轮单独取工作区；FIFO 的中间文件不作为优化轮输入。
            # 两次解析同一个相对目录，但相对于各自的工作区根目录。
            fifo_cwd = self._resolve_work_dir(workspace_factory(), workload.work_dir)
            # 不在对比服务中写死 CPU 编号；两轮使用同一个核心配置参数。
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
            # 推进的是进度分段下标，不是已成功的子任务数。
            run_index += 1
            # 初始估值只供基线计划展示；优化分配依据该组 FIFO 的实测任务耗时。
            # 复制负载而不改写原输入，便于保留两轮各自使用的估算来源。
            profiled_workload = self._apply_fifo_measurements(workload, fifo)
            on_log(
                f"FIFO timings applied as optimized estimates for {workload.name}",
                "co_debug.comparison",
            )
            # 从新副本执行同一负载，让写文件等副作用只影响当前策略。
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
            run_index += 1

            # 差异率验证该组任务是否长短分明，取的是 FIFO 的逐任务耗时。
            # 改进率比较两轮的整轮完工时间，不能换成逐任务耗时的总和。
            # 不使用优化轮的任务耗时来反过来判定测试负载是否合格。
            duration_spread_rate = self.metric_service.duration_spread_rate(
                [result.elapsed_ms for result in fifo.execution.task_results]
            )
            improvement_rate = self.metric_service.improvement_rate(
                fifo.execution.actual_makespan_ms,
                optimized.execution.actual_makespan_ms,
            )
            # 每组单独保留两轮结果及门槛判断，便于回查某个工程为何未达标。
            # 单组改进率仍供展示，最终判定使用全部样本的平均改进率。
            # 成本来源随结果序列化，便于区分本对比和只使用估时的单策略实验。
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
            on_log(f"comparison workload finished: {workload.name}", "co_debug.comparison")

        # 各组改进率等权汇总，不直接汇总两轮毫秒数再计算比例。
        # 调试/开发可以少跑样本，正式目标仍要求指定数量的样本。
        average_improvement_rate = self.metric_service.average_improvement_rate(
            [result.improvement_rate for result in workload_results]
        )
        has_required_workload_count = (
            len(workload_results) == self.metric_service.REQUIRED_WORKLOAD_COUNT
        )
        # 负载差异是逐组门槛，不能用一组特别大的差异抵消另一组不足。
        all_duration_spreads_eligible = all(
            result.meets_duration_spread_requirement for result in workload_results
        )
        # 两种策略都必须跑完同一批任务；失败的短运行不能当成有效提速。
        all_tasks_succeeded = all(
            result.fifo.execution.all_succeeded and result.optimized.execution.all_succeeded
            for result in workload_results
        )
        # 平均改进率过线不代表整体达标；还要合并样本数、差异率与成功条件。
        # 判定使用指标服务返回的百分比值，与响应中显示的计算口径一致。
        meets_average_improvement_requirement = (
            average_improvement_rate >= self.metric_service.REQUIRED_IMPROVEMENT_RATE
        )
        # 分项条件和总判定同时返回，让前端说明缺的是样本、差异、成功状态还是改进率。
        # 比较结果不丢弃未达标样本，否则平均值会偏高。
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
        # 工作负载可来自不同系统；同时识别宿主路径和 Windows 盘符/根路径。
        # 禁止绝对目录，保证两轮都从各自的项目副本取输入。
        candidate = Path(work_dir)
        windows_path = PureWindowsPath(work_dir)
        if not work_dir or candidate.is_absolute() or windows_path.anchor:
            raise AppError("work_dir must be relative to the project workspace")
        # 解析符号链接和父目录后再比较根路径，不能只检查字符串前缀。
        # 同时校验目录存在，避免把路径错误混进策略运行的时间数据。
        # 允许相对目录指向根目录本身；工作负载不要求工程必须有子目录。
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
        # 基线可能因多核并行而异步完成，必须按任务名而不是按返回顺序对齐。
        # 毫秒取整后可能为零；用 1 毫秒下限保持调度输入的估值为正。
        # 下限只影响优化计划的估值，不改写基线结果中的原始实测数据。
        measured_durations = {
            result.task_name: max(result.elapsed_ms, 1)
            for result in fifo.execution.task_results
        }
        # 仅替换估值，保持命令、参数、任务名和原始顺序不变。
        # 优化策略自行按新估值排序，不提前改变提交顺序。
        # 不回退到调用方的旧估值：两轮必须对齐同一批已测量作业。
        profiled_tasks = [
            task.model_copy(update={"estimated_ms": measured_durations[task.name]})
            for task in workload.tasks
        ]
        # 工作负载名和工作目录也沿用，避免把成本校准变成另一个测试样本。
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
        # 把 5–95 的区间划给各轮，前后留给平台初始化和结果收尾。
        # 根据轮次计算边界，连续轮次共享端点，避免逐轮累加的取整误差。
        # 两种策略共用这套进度逻辑，便于在日志里对照同一负载的两次运行。
        segment_start = 5 + round(run_index * 90 / total_runs)
        segment_end = 5 + round((run_index + 1) * 90 / total_runs)
        # 轮内约五分之一给规划，其余给实际执行；规划至少占一个百分点。
        plan_end = segment_start + max(1, round((segment_end - segment_start) * 0.2))
        # 每轮开头报告样本和策略，即使规划出错也可定位失败轮次。
        on_progress(
            segment_start,
            f"planning {workload.name} with {strategy.value}",
        )
        # 规划和执行共用取消状态，规划一旦失败就不会启动该轮子进程。
        # 规划器内部的百分比由服务层缩放到本轮的规划子区间。
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
        # 将规划结果原样交给执行器，便于按核心与队列位置比对计划和实测。
        # 传入本轮工作目录，避免不同样本的相对路径混用。
        # 规划终点同时是执行起点，避免同一轮的进度退回初始值。
        # 子进程超时由执行服务使用；对比任务的整体时限仍由平台管理。
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
        # 将计划和执行一起交回调用方，不能仅凭实测时间重建原分配。
        return StrategyRunResult(plan=plan, execution=execution)
