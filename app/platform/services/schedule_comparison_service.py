from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from app.core.errors import CancellationRequested
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.scheduler import (
    ScheduleComparisonSummary,
    StrategyRunResult,
    WorkloadComparisonResult,
)
from app.platform.schemas.tasks import ScheduleWorkloadSpec
from app.platform.services.metric_service import AcceptanceMetricService
from app.platform.services.schedule_execution_service import ScheduleExecutionService
from app.platform.services.scheduler_service import SchedulerService

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

    async def compare(
        self,
        task_id: UUID,
        workloads: list[ScheduleWorkloadSpec],
        core_ids: list[int] | None,
        cwd: Path,
        timeout_seconds: int,
        on_log: LogCallback,
        on_progress: ProgressCallback,
        is_cancelled: CancelCheck,
        workspace_factory: Callable[[], Path] | None = None,
    ) -> ScheduleComparisonSummary:
        workload_results: list[WorkloadComparisonResult] = []
        total_runs = len(workloads) * 2
        run_index = 0

        for workload in workloads:
            if is_cancelled():
                raise CancellationRequested()

            on_log(f"comparison workload started: {workload.name}", "co_debug.comparison")
            fifo_cwd = workspace_factory() if workspace_factory else cwd
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
            run_index += 1
            profiled_workload = self._apply_fifo_measurements(workload, fifo)
            on_log(
                f"FIFO timings applied as optimized estimates for {workload.name}",
                "co_debug.comparison",
            )
            optimized_cwd = (
                workspace_factory() if workspace_factory else cwd
            )
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

            duration_spread_rate = self.metric_service.duration_spread_rate(
                [result.elapsed_ms for result in fifo.execution.task_results]
            )
            improvement_rate = self.metric_service.improvement_rate(
                fifo.execution.actual_makespan_ms,
                optimized.execution.actual_makespan_ms,
            )
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

        average_improvement_rate = self.metric_service.average_improvement_rate(
            [result.improvement_rate for result in workload_results]
        )
        has_required_workload_count = (
            len(workload_results) == self.metric_service.REQUIRED_WORKLOAD_COUNT
        )
        all_duration_spreads_eligible = all(
            result.meets_duration_spread_requirement for result in workload_results
        )
        all_tasks_succeeded = all(
            result.fifo.execution.all_succeeded and result.optimized.execution.all_succeeded
            for result in workload_results
        )
        meets_average_improvement_requirement = (
            average_improvement_rate >= self.metric_service.REQUIRED_IMPROVEMENT_RATE
        )
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

    def _apply_fifo_measurements(
        self,
        workload: ScheduleWorkloadSpec,
        fifo: StrategyRunResult,
    ) -> ScheduleWorkloadSpec:
        measured_durations = {
            result.task_name: max(result.elapsed_ms, 1)
            for result in fifo.execution.task_results
        }
        profiled_tasks = [
            task.model_copy(update={"estimated_ms": measured_durations[task.name]})
            for task in workload.tasks
        ]
        return ScheduleWorkloadSpec(name=workload.name, tasks=profiled_tasks)

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
        segment_start = 5 + round(run_index * 90 / total_runs)
        segment_end = 5 + round((run_index + 1) * 90 / total_runs)
        plan_end = segment_start + max(1, round((segment_end - segment_start) * 0.2))
        on_progress(
            segment_start,
            f"planning {workload.name} with {strategy.value}",
        )
        plan = self.scheduler_service.create_plan(
            task_id=task_id,
            strategy=strategy,
            tasks=workload.tasks,
            is_cancelled=is_cancelled,
            core_ids=core_ids,
            progress_start=segment_start,
            progress_end=plan_end,
        )
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
        return StrategyRunResult(plan=plan, execution=execution)
