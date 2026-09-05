from app.modules.co_debug.scheduler.contracts import TaskContext
from app.modules.co_debug.schemas.scheduler import SchedulePlan, TaskAssignment
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec


def plan_tasks(
    strategy: SchedulerStrategy,
    tasks: list[TaskExecutionSpec],
    context: TaskContext,
    core_ids: list[int] | None = None,
) -> SchedulePlan:
    normalized_cores = _normalize_core_ids(core_ids)
    _validate_tasks(tasks)

    context.log(f"planning {len(tasks)} tasks with strategy {strategy.value}")
    context.progress(20, "validating tasks and CPU cores")
    context.check_cancelled()

    if strategy == SchedulerStrategy.FIFO_BASELINE:
        ordered = _fifo_plan(tasks)
        assignments, core_loads = _assign_fifo(ordered, normalized_cores)
    else:
        ordered = _resource_aware_plan(tasks)
        assignments, core_loads = _assign_least_loaded(ordered, normalized_cores)

    context.progress(70, "task-to-core assignments generated")
    context.check_cancelled()

    estimated_total = sum(task.estimated_ms for task in ordered)
    estimated_makespan = max(core_loads.values(), default=0)
    notes = [
        "The co_debug scheduler is called as an ordinary Python function by the platform.",
        "The platform owns process execution, cancellation state, and frontend log streaming.",
        "FIFO uses static round-robin core mapping with a FIFO queue on each core.",
        (
            "RESOURCE_AWARE uses longest-processing-time-first and assigns each task "
            "to the least-loaded core."
        ),
    ]
    context.progress(100, "schedule plan ready")
    return SchedulePlan(
        strategy=strategy,
        core_ids=normalized_cores,
        ordered_tasks=ordered,
        assignments=assignments,
        core_loads_ms=core_loads,
        estimated_total_ms=estimated_total,
        estimated_makespan_ms=estimated_makespan,
        notes=notes,
    )


def _fifo_plan(tasks: list[TaskExecutionSpec]) -> list[TaskExecutionSpec]:
    return list(tasks)


def _resource_aware_plan(tasks: list[TaskExecutionSpec]) -> list[TaskExecutionSpec]:
    return sorted(tasks, key=lambda task: task.estimated_ms, reverse=True)


def _normalize_core_ids(core_ids: list[int] | None) -> list[int]:
    normalized = [0] if core_ids is None else list(core_ids)
    if not normalized:
        raise ValueError("core_ids must contain at least one CPU core")
    if any(core_id < 0 for core_id in normalized):
        raise ValueError("core_ids must not contain negative CPU core IDs")
    if len(set(normalized)) != len(normalized):
        raise ValueError("core_ids must not contain duplicate CPU core IDs")
    return normalized


def _validate_tasks(tasks: list[TaskExecutionSpec]) -> None:
    names = [task.name for task in tasks]
    if len(set(names)) != len(names):
        raise ValueError("task names must be unique within a scheduler workload")
    if any(task.depends_on for task in tasks):
        raise ValueError("task dependencies are not supported by the minimal scheduler")


def _assign_fifo(
    tasks: list[TaskExecutionSpec],
    core_ids: list[int],
) -> tuple[list[TaskAssignment], dict[int, int]]:
    core_loads = {core_id: 0 for core_id in core_ids}
    queue_positions = {core_id: 0 for core_id in core_ids}
    assignments: list[TaskAssignment] = []

    for index, task in enumerate(tasks):
        core_id = core_ids[index % len(core_ids)]
        assignments.append(
            _create_assignment(
                task=task,
                core_id=core_id,
                queue_position=queue_positions[core_id],
                estimated_start_ms=core_loads[core_id],
            )
        )
        core_loads[core_id] += task.estimated_ms
        queue_positions[core_id] += 1

    return assignments, core_loads


def _assign_least_loaded(
    tasks: list[TaskExecutionSpec],
    core_ids: list[int],
) -> tuple[list[TaskAssignment], dict[int, int]]:
    core_loads = {core_id: 0 for core_id in core_ids}
    queue_positions = {core_id: 0 for core_id in core_ids}
    assignments: list[TaskAssignment] = []

    for task in tasks:
        core_id = min(core_ids, key=lambda item: (core_loads[item], item))
        assignments.append(
            _create_assignment(
                task=task,
                core_id=core_id,
                queue_position=queue_positions[core_id],
                estimated_start_ms=core_loads[core_id],
            )
        )
        core_loads[core_id] += task.estimated_ms
        queue_positions[core_id] += 1

    return assignments, core_loads


def _create_assignment(
    task: TaskExecutionSpec,
    core_id: int,
    queue_position: int,
    estimated_start_ms: int,
) -> TaskAssignment:
    return TaskAssignment(
        task_name=task.name,
        core_id=core_id,
        queue_position=queue_position,
        estimated_start_ms=estimated_start_ms,
        estimated_finish_ms=estimated_start_ms + task.estimated_ms,
    )

