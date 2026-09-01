import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from app.core.errors import CancellationRequested
from app.platform.schemas.scheduler import (
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
        tasks_by_name = {task.name: task for task in tasks}
        queues = self._build_core_queues(plan)
        task_results: list[ExecutedTaskResult] = []
        result_lock = asyncio.Lock()
        completed_count = 0
        experiment_started = time.perf_counter()

        async def run_core_queue(
            core_id: int,
            assignments: list[TaskAssignment],
        ) -> None:
            nonlocal completed_count
            for assignment in assignments:
                if is_cancelled():
                    raise CancellationRequested()

                task = tasks_by_name[assignment.task_name]
                started_offset_ms = round((time.perf_counter() - experiment_started) * 1000)
                on_log(f"starting task {task.name} on core {core_id}", "co_debug.executor")

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

        workers = [
            asyncio.create_task(run_core_queue(core_id, assignments))
            for core_id, assignments in queues.items()
            if assignments
        ]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        actual_makespan_ms = round((time.perf_counter() - experiment_started) * 1000)
        assignment_order = {
            assignment.task_name: index for index, assignment in enumerate(plan.assignments)
        }
        task_results.sort(key=lambda item: assignment_order[item.task_name])
        return ScheduleExecutionResult(
            task_results=task_results,
            actual_makespan_ms=actual_makespan_ms,
            all_succeeded=all(result.exit_code == 0 for result in task_results),
        )

    def _build_core_queues(self, plan: SchedulePlan) -> dict[int, list[TaskAssignment]]:
        queues = {core_id: [] for core_id in plan.core_ids}
        for assignment in plan.assignments:
            queues[assignment.core_id].append(assignment)
        for assignments in queues.values():
            assignments.sort(key=lambda item: item.queue_position)
        return queues
