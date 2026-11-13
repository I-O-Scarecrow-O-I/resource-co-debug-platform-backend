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


class ScheduleExecutionService:
    def __init__(self, process_runner: ProcessRunner) -> None:
        self.process_runner = process_runner

    # 执行阶段使用既有计划和工作区，不根据实时耗时迁移任务。
    # 每条命令的超时和取消由进程运行器执行。
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
        # 计划只保存任务名和分配信息，实际命令仍取自同批任务规格。
        # 各核队列共享同一单调时钟起点，便于横向对齐时间线。
        tasks_by_name = {task.name: task for task in tasks}
        # 队列依据计划恢复，与 tasks 的原始顺序无关。
        # 当前任务名与计划由同一服务创建，运行器不重新匹配近似名称。
        queues = self._build_core_queues(plan)
        task_results: list[ExecutedTaskResult] = []
        # 锁只覆盖收集结果和报告进度，不持锁等待进程。
        # 不同核的输出与运行可继续并行。
        result_lock = asyncio.Lock()
        # 完成计数只覆盖实际返回的进程结果，抛异常的命令不会占一个已完成位置。
        completed_count = 0
        experiment_started = time.perf_counter()

        # 同一核心内串行等待前一任务退出，不同核心的队列可以交叠。
        # 计划估计的开始时刻只用于展示，执行时不为它额外等待。
        async def run_core_queue(
            core_id: int,
            assignments: list[TaskAssignment],
        ) -> None:
            nonlocal completed_count
            # 为每核 worker 顺序消费其全部分配；一个子任务非零退出仍产生可比较的结果。
            for assignment in assignments:
                # 队列边界是启动新进程前的取消检查点。
                if is_cancelled():
                    raise CancellationRequested()

                # 起始偏移在实际调用运行器前测量，和计划的估计时刻分别保留。
                task = tasks_by_name[assignment.task_name]
                # 记录的是将要调用进程运行器的时刻。
                # 和进程自身开始执行某条指令的时间不是同一口径。
                started_offset_ms = round((time.perf_counter() - experiment_started) * 1000)
                # 计划核心在启动日志中可见；是否真正绑定必须查询执行记录。
                on_log(f"starting task {task.name} on core {core_id}", "co_debug.executor")

                # 核心编号是给运行器的亲和性请求；最终是否生效由结果记录。
                # 日志携带任务名，多个队列交织输出时仍能关联来源。
                # 超时预算对每条命令独立生效，不等同于整场实验的总时限。
                # 同一超时值为各命令独立使用；它不含本条命令在核队列中等待的时间。
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
                # 结束偏移来自同一实验时钟，可以和别的核心的起点放在一张时间线上。
                finished_offset_ms = round((time.perf_counter() - experiment_started) * 1000)

                # 起止偏移来自整场实验时钟，elapsed 来自单个运行器。
                # 前者可画并行时间线，后者可供后续策略估计。
                # 成功启动并结束的进程都记录退出码，不能只汇总成功样本。
                # 执行记录同时保存两个时钟视角：运行器自身的时长，以及整个实验中的开始和结束偏移。
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
                # 结果写入和完成数推进放在同一临界区，保证进度与已记录结果一致。
                async with result_lock:
                    task_results.append(result)
                    completed_count += 1
                    # 进度按全局已完成任务数缩放；较慢的核心不会被快核心重复计权。
                    percent = progress_start + round(
                        completed_count * (progress_end - progress_start) / max(len(tasks), 1)
                    )
                    on_progress(percent, f"completed {completed_count}/{len(tasks)} tasks")

                # 退出日志携带任务名和核编号；非零退出不伪装成调度器自身异常。
                on_log(
                    f"finished task {task.name} on core {core_id} "
                    f"with exit code {process_result.exit_code}",
                    "co_debug.executor",
                )

        # 空队列不创建 worker，核心仍保留在原计划中供展示。
        workers = [
            asyncio.create_task(run_core_queue(core_id, assignments))
            for core_id, assignments in queues.items()
            if assignments
        ]
        # 协程异常或外层取消时不返回不完整结果。
        # 其他 worker 先被取消，再等待各自运行器回收。
        try:
            await asyncio.gather(*workers)
        # 等待任一队列异常时也收尾其余 worker，避免后台残留进程。
        # 收尾后继续传播原异常，保留上层区分取消与运行失败的机会。
        except BaseException:
            # 取消兄弟 worker 后等待它们退出，避免对比的下一轮开始时上一轮仍占用资源。
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        # 墙钟区间含创建队列 worker 和等待其完成的开销，不能用估计最大负载替代。
        actual_makespan_ms = round((time.perf_counter() - experiment_started) * 1000)
        # 按计划分配顺序交付结果，避免进程先后退出造成展示顺序漂移。
        # 总历时是墙钟时间，不能用各任务耗时相加代替。
        assignment_order = {
            assignment.task_name: index for index, assignment in enumerate(plan.assignments)
        }
        # 只恢复输出列表的确定性；排序不会修改实际时间偏移或各核执行先后。
        task_results.sort(key=lambda item: assignment_order[item.task_name])
        # 命令失败通过 all_succeeded 表示，调用方仍可查看各项实际数据。
        # 这一布尔值只由已记录命令的退出码决定。
        # 绑核失败不等同于命令失败，需另看 affinity_applied。
        return ScheduleExecutionResult(
            task_results=task_results,
            actual_makespan_ms=actual_makespan_ms,
            all_succeeded=all(result.exit_code == 0 for result in task_results),
        )

    # 按核心和队列位置还原计划；全局分配列表并不等价于每核执行队列。
    def _build_core_queues(self, plan: SchedulePlan) -> dict[int, list[TaskAssignment]]:
        # 先建立所有计划核的桶；计划包含空核时不因缺任务而丢失该核。
        queues = {core_id: [] for core_id in plan.core_ids}
        for assignment in plan.assignments:
            queues[assignment.core_id].append(assignment)
        # 核内按 queue_position 排序，确保每核输入来自同一份明确计划。
        # 结果列表的最终顺序另由原计划索引恢复，不影响实际执行。
        for assignments in queues.values():
            assignments.sort(key=lambda item: item.queue_position)
        return queues
