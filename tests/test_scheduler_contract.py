from uuid import uuid4

from app.modules.co_debug.scheduler.contracts import TaskContext
from app.modules.co_debug.scheduler.scheduler import plan_tasks
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec


def test_resource_aware_plan_orders_long_tasks_first() -> None:
    logs: list[str] = []
    progress_events: list[int] = []
    context = TaskContext(
        task_id=uuid4(),
        log=lambda message, stream="co_debug.scheduler": logs.append(f"{stream}:{message}"),
        progress=lambda percent, message: progress_events.append(percent),
        is_cancelled=lambda: False,
    )

    plan = plan_tasks(
        strategy=SchedulerStrategy.RESOURCE_AWARE,
        tasks=[
            TaskExecutionSpec(name="short", command=["echo", "short"], estimated_ms=1000),
            TaskExecutionSpec(name="long", command=["echo", "long"], estimated_ms=3000),
        ],
        context=context,
    )

    assert [task.name for task in plan.ordered_tasks] == ["long", "short"]
    assert progress_events[-1] == 100
    assert logs

