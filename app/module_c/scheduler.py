from app.domain.enums import SchedulerStrategy
from app.module_c.contracts import TaskContext
from app.schemas.scheduler import SchedulePlan
from app.schemas.tasks import TaskExecutionSpec


def plan_tasks(
    strategy: SchedulerStrategy,
    tasks: list[TaskExecutionSpec],
    context: TaskContext,
) -> SchedulePlan:
    context.log(f"planning {len(tasks)} tasks with strategy {strategy.value}")
    context.progress(20, "validating task dependency graph")
    context.check_cancelled()

    if strategy == SchedulerStrategy.FIFO_BASELINE:
        ordered = _fifo_plan(tasks)
    else:
        ordered = _resource_aware_plan(tasks)

    context.progress(70, "schedule order generated")
    context.check_cancelled()

    estimated_total = sum(task.estimated_ms for task in ordered)
    notes = [
        "Module C is called as an ordinary Python function by module A.",
        "Module A owns process execution, cancellation state, and frontend log streaming.",
    ]
    context.progress(100, "schedule plan ready")
    return SchedulePlan(
        strategy=strategy,
        ordered_tasks=ordered,
        estimated_total_ms=estimated_total,
        notes=notes,
    )


def _fifo_plan(tasks: list[TaskExecutionSpec]) -> list[TaskExecutionSpec]:
    return list(tasks)


def _resource_aware_plan(tasks: list[TaskExecutionSpec]) -> list[TaskExecutionSpec]:
    return sorted(tasks, key=lambda task: task.estimated_ms, reverse=True)
