import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from app.core.errors import CancellationRequested
from app.modules.co_debug.schemas.scheduler import (
    ExecutedTaskResult,
    ScheduleExecutionResult,
    SchedulePlan,
    TaskAssignment,
)
from app.platform.schemas.tasks import TaskExecutionSpec
from app.platform.services.process_runner import ProcessRunner

LogCallback = Callable[[str, str], None]
ProgressCallback = Callable[[int, str], None]
CancelCheck = Callable[[], bool]


# 执行层消费已生成的分配计划，不在运行中重排或迁移任务。
# 进程创建、绑核和子进程回收均交给平台的 ProcessRunner。
class ScheduleExecutionService:
    def __init__(self, process_runner: ProcessRunner) -> None:
        self.process_runner = process_runner

    async def execute(
        self,
        plan: SchedulePlan,
        tasks: list[TaskExecutionSpec],
        cwd: Path,
        timeout_seconds: int,
        on_log: LogCallback,
        on_progress: ProgressCallback,
        is_cancelled: CancelCheck,
        progress_start: int = 40,
        progress_end: int = 95,
    ) -> ScheduleExecutionResult:
        # 计划记录名称和队列位置，运行命令仍从原工作负载取出。
        # 同名任务在规划时已被拒绝，因此名称可作为两者的关联键。
        tasks_by_name = {task.name: task for task in tasks}
        queues = self._build_core_queues(plan)
        # 各核心并发完成，结果列表和总进度由所有队列共同更新。
        # 先建立同一时钟起点，使不同核心的相对时间可直接比较。
        # 使用单调时钟，避免校时或时区变化影响耗时差。
        task_results: list[ExecutedTaskResult] = []
        result_lock = asyncio.Lock()
        completed_count = 0
        experiment_started = time.perf_counter()

        async def run_core_queue(
            core_id: int,
            assignments: list[TaskAssignment],
        ) -> None:
            nonlocal completed_count
            # 单核队列逐项等待；只有不同核心之间允许并行。
            # 下一项启动前检查取消，防止取消后继续消费队列。
            for assignment in assignments:
                if is_cancelled():
                    raise CancellationRequested()

                task = tasks_by_name[assignment.task_name]
                # 偏移描述任务在整轮时间线的位置，不等同于子进程自身的耗时。
                started_offset_ms = round((time.perf_counter() - experiment_started) * 1000)
                on_log(f"starting task {task.name} on core {core_id}", "co_debug.executor")

                # 由运行器接收同一取消回调，使取消也能作用于正在执行的进程。
                # 日志带任务名；多个核心的输出交错时仍能定位来源。
                # 回调以默认参数绑定任务名，避免依赖后续循环的取值。
                # 这里传的是单次进程超时，整项托管任务的总时限由平台控制。
                process_result = await self.process_runner.run(
                    command=task.command,
                    cwd=cwd,
                    timeout_seconds=timeout_seconds,
                    on_log=lambda message, stream, task_name=task.name: on_log(
                        f"[{task_name}] {message}",
                        f"co_debug.executor.{stream}",
                    ),
                    is_cancelled=is_cancelled,
                    cpu_core=core_id,
                )
                finished_offset_ms = round((time.perf_counter() - experiment_started) * 1000)

                # 计划核心编号和实际绑核状态分别保存；前者不能证明后者成功。
                # 非零退出码作为该作业结果保留，队列仍可收集余下作业的结果。
                result = ExecutedTaskResult(
                    task_name=task.name,
                    core_id=core_id,
                    queue_position=assignment.queue_position,
                    exit_code=process_result.exit_code,
                    elapsed_ms=process_result.elapsed_ms,
                    started_offset_ms=started_offset_ms,
                    finished_offset_ms=finished_offset_ms,
                    affinity_applied=process_result.affinity_applied,
                )
                # 结果提交和进度计算放在同一临界区，按全局完成数推进。
                # 调用方可以为不同策略分配不同进度区间，但这里不关心策略。
                async with result_lock:
                    task_results.append(result)
                    completed_count += 1
                    percent = progress_start + round(
                        completed_count * (progress_end - progress_start) / max(len(tasks), 1)
                    )
                    on_progress(percent, f"completed {completed_count}/{len(tasks)} tasks")

                on_log(
                    f"finished task {task.name} on core {core_id} "
                    f"with exit code {process_result.exit_code}",
                    "co_debug.executor",
                )

        # 只为非空队列建立协程；未分配任务的核心不占用运行资源。
        # 每个队列独立运行，慢队列不会阻止其他核心接着执行。
        workers = [
            asyncio.create_task(run_core_queue(core_id, assignments))
            for core_id, assignments in queues.items()
            if assignments
        ]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            # gather 遇到异常不保证回收其余队列，因此主动取消并等待它们退出。
            # 同样处理外部取消，防止下一轮开始后仍有上一轮的进程活动。
            # 回收过程中收集异常，最终保留触发清理的原异常。
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        # 整轮完工耗时是所有队列完成前的墙钟时间，不是任务耗时之和。
        # 结果按计划顺序返回，不把并发完成的先后当成调度优先级。
        actual_makespan_ms = round((time.perf_counter() - experiment_started) * 1000)
        assignment_order = {
            assignment.task_name: index for index, assignment in enumerate(plan.assignments)
        }
        task_results.sort(key=lambda item: assignment_order[item.task_name])
        # 成功标志依据进程退出码；绑核状态单独保留供测试记录核对。
        return ScheduleExecutionResult(
            task_results=task_results,
            actual_makespan_ms=actual_makespan_ms,
            all_succeeded=all(result.exit_code == 0 for result in task_results),
        )

    def _build_core_queues(self, plan: SchedulePlan) -> dict[int, list[TaskAssignment]]:
        # 先按核心分组，再按核内序号排序，避免依赖分配列表的全局顺序。
        # 这里不使用预计起始时间做延迟：前一项真实结束后即可执行下一项。
        # 先初始化全部核心，输出仍能表示未分到任务的空队列。
        queues = {core_id: [] for core_id in plan.core_ids}
        for assignment in plan.assignments:
            queues[assignment.core_id].append(assignment)
        for assignments in queues.values():
            assignments.sort(key=lambda item: item.queue_position)
        return queues
