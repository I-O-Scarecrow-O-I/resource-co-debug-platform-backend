import sys
from uuid import uuid4

import pytest

from app.modules.co_debug.scheduler.contracts import TaskContext
from app.modules.co_debug.scheduler.scheduler import plan_tasks
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.schedule_execution_service import ScheduleExecutionService


def _context() -> TaskContext:
    return TaskContext(
        task_id=uuid4(),
        log=lambda message, stream="co_debug.scheduler": None,
        progress=lambda percent, message: None,
        is_cancelled=lambda: False,
    )


def _sleep_task(name: str, seconds: float, estimated_ms: int) -> TaskExecutionSpec:
    return TaskExecutionSpec(
        name=name,
        command=[sys.executable, "-c", f"import time; time.sleep({seconds})"],
        estimated_ms=estimated_ms,
    )


@pytest.mark.asyncio
async def test_executor_runs_different_core_queues_concurrently(tmp_path) -> None:
    tasks = [
        _sleep_task("left", seconds=0.15, estimated_ms=150),
        _sleep_task("right", seconds=0.15, estimated_ms=150),
    ]
    plan = plan_tasks(
        strategy=SchedulerStrategy.FIFO_BASELINE,
        tasks=tasks,
        context=_context(),
        core_ids=[0, 1],
    )
    service = ScheduleExecutionService(process_runner=ProcessRunner())

    result = await service.execute(
        plan=plan,
        tasks=tasks,
        cwd=tmp_path,
        timeout_seconds=5,
        on_log=lambda message, stream: None,
        on_progress=lambda percent, message: None,
        is_cancelled=lambda: False,
    )

    assert result.all_succeeded is True
    assert len(result.task_results) == 2
    latest_start = max(item.started_offset_ms for item in result.task_results)
    earliest_finish = min(item.finished_offset_ms for item in result.task_results)
    assert latest_start < earliest_finish


@pytest.mark.asyncio
async def test_executor_keeps_tasks_on_one_core_in_fifo_order(tmp_path) -> None:
    tasks = [
        _sleep_task("first", seconds=0.05, estimated_ms=50),
        _sleep_task("second", seconds=0.05, estimated_ms=50),
    ]
    plan = plan_tasks(
        strategy=SchedulerStrategy.FIFO_BASELINE,
        tasks=tasks,
        context=_context(),
        core_ids=[0],
    )
    service = ScheduleExecutionService(process_runner=ProcessRunner())

    result = await service.execute(
        plan=plan,
        tasks=tasks,
        cwd=tmp_path,
        timeout_seconds=5,
        on_log=lambda message, stream: None,
        on_progress=lambda percent, message: None,
        is_cancelled=lambda: False,
    )

    first, second = result.task_results
    assert first.task_name == "first"
    assert second.task_name == "second"
    assert second.started_offset_ms >= first.finished_offset_ms
    assert result.actual_makespan_ms >= second.finished_offset_ms
