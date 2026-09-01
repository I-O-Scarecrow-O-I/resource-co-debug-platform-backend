from uuid import uuid4

from app.modules.co_debug.scheduler.contracts import TaskContext
from app.modules.co_debug.scheduler.scheduler import plan_tasks
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec


def _context(
    logs: list[str] | None = None,
    progress_events: list[int] | None = None,
) -> TaskContext:
    captured_logs = logs if logs is not None else []
    captured_progress = progress_events if progress_events is not None else []
    return TaskContext(
        task_id=uuid4(),
        log=lambda message, stream="co_debug.scheduler": captured_logs.append(
            f"{stream}:{message}"
        ),
        progress=lambda percent, message: captured_progress.append(percent),
        is_cancelled=lambda: False,
    )


def test_resource_aware_plan_orders_long_tasks_first() -> None:
    logs: list[str] = []
    progress_events: list[int] = []

    plan = plan_tasks(
        strategy=SchedulerStrategy.RESOURCE_AWARE,
        tasks=[
            TaskExecutionSpec(name="short", command=["echo", "short"], estimated_ms=1000),
            TaskExecutionSpec(name="long", command=["echo", "long"], estimated_ms=3000),
        ],
        context=_context(logs, progress_events),
        core_ids=[0, 1],
    )

    assert [task.name for task in plan.ordered_tasks] == ["long", "short"]
    assert [(item.task_name, item.core_id) for item in plan.assignments] == [
        ("long", 0),
        ("short", 1),
    ]
    assert plan.core_loads_ms == {0: 3000, 1: 1000}
    assert plan.estimated_makespan_ms == 3000
    assert progress_events[-1] == 100
    assert logs


def test_fifo_uses_static_round_robin_core_queues() -> None:
    plan = plan_tasks(
        strategy=SchedulerStrategy.FIFO_BASELINE,
        tasks=[
            TaskExecutionSpec(name="long-a", command=["long-a"], estimated_ms=9000),
            TaskExecutionSpec(name="short-a", command=["short-a"], estimated_ms=1000),
            TaskExecutionSpec(name="long-b", command=["long-b"], estimated_ms=9000),
            TaskExecutionSpec(name="short-b", command=["short-b"], estimated_ms=1000),
        ],
        context=_context(),
        core_ids=[0, 1],
    )

    assert [(item.task_name, item.core_id) for item in plan.assignments] == [
        ("long-a", 0),
        ("short-a", 1),
        ("long-b", 0),
        ("short-b", 1),
    ]
    assert plan.core_loads_ms == {0: 18000, 1: 2000}
    assert plan.estimated_makespan_ms == 18000


def test_resource_aware_lpt_balances_estimated_core_loads() -> None:
    plan = plan_tasks(
        strategy=SchedulerStrategy.RESOURCE_AWARE,
        tasks=[
            TaskExecutionSpec(name="long-a", command=["long-a"], estimated_ms=9000),
            TaskExecutionSpec(name="short-a", command=["short-a"], estimated_ms=1000),
            TaskExecutionSpec(name="long-b", command=["long-b"], estimated_ms=9000),
            TaskExecutionSpec(name="short-b", command=["short-b"], estimated_ms=1000),
        ],
        context=_context(),
        core_ids=[0, 1],
    )

    assert [task.name for task in plan.ordered_tasks] == [
        "long-a",
        "long-b",
        "short-a",
        "short-b",
    ]
    assert plan.core_loads_ms == {0: 10000, 1: 10000}
    assert plan.estimated_makespan_ms == 10000


def test_plan_rejects_duplicate_core_ids() -> None:
    try:
        plan_tasks(
            strategy=SchedulerStrategy.FIFO_BASELINE,
            tasks=[],
            context=_context(),
            core_ids=[0, 0],
        )
    except ValueError as exc:
        assert str(exc) == "core_ids must not contain duplicate CPU core IDs"
    else:
        raise AssertionError("duplicate core IDs should be rejected")


def test_plan_rejects_duplicate_task_names_with_generic_message() -> None:
    try:
        plan_tasks(
            strategy=SchedulerStrategy.FIFO_BASELINE,
            tasks=[
                TaskExecutionSpec(name="duplicate", command=["first"]),
                TaskExecutionSpec(name="duplicate", command=["second"]),
            ],
            context=_context(),
            core_ids=[0],
        )
    except ValueError as exc:
        assert str(exc) == "task names must be unique within a scheduler workload"
    else:
        raise AssertionError("duplicate task names should be rejected")


def test_plan_rejects_dependencies_until_dependency_scheduling_is_implemented() -> None:
    try:
        plan_tasks(
            strategy=SchedulerStrategy.RESOURCE_AWARE,
            tasks=[
                TaskExecutionSpec(name="first", command=["first"]),
                TaskExecutionSpec(name="second", command=["second"], depends_on=["first"]),
            ],
            context=_context(),
            core_ids=[0, 1],
        )
    except ValueError as exc:
        assert str(exc) == "task dependencies are not supported by the minimal scheduler"
    else:
        raise AssertionError("unsupported dependencies should be rejected")

