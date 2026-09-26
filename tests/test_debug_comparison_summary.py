from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.errors import AppError, NotFoundError
from app.main import create_app
from app.modules.co_debug.schemas.scheduler import ScheduleComparisonSummary
from app.modules.co_debug.services.debug_comparison_summary_service import (
    DebugComparisonSummaryService,
)
from app.modules.co_debug.services.metric_service import AcceptanceMetricService
from app.platform.api.deps import get_debug_comparison_summary_service
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord


class FakeTaskService:
    def __init__(self, tasks: list[TaskRecord]) -> None:
        self.tasks = {task.id: task for task in tasks}

    def require_task(self, task_id: UUID) -> TaskRecord:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        return task


def _summary(
    improvement_rate: float = 20.0,
    *,
    duration_eligible: bool = True,
    tasks_succeeded: bool = True,
) -> dict:
    strategy = {
        "plan": {
            "strategy": "FIFO_BASELINE",
            "core_ids": [0, 1],
            "ordered_tasks": [],
            "assignments": [],
            "core_loads_ms": {},
            "estimated_total_ms": 0,
            "estimated_makespan_ms": 0,
            "notes": [],
        },
        "execution": {
            "task_results": [],
            "actual_makespan_ms": 100,
            "all_succeeded": tasks_succeeded,
        },
    }
    result = ScheduleComparisonSummary(
        workload_results=[
            {
                "workload_name": "sample",
                "cost_estimation_source": "FIFO_ACTUAL_DURATION",
                "fifo": strategy,
                "optimized": {
                    **strategy,
                    "plan": {**strategy["plan"], "strategy": "RESOURCE_AWARE"},
                },
                "duration_spread_rate": 200.0 if duration_eligible else 100.0,
                "improvement_rate": improvement_rate,
                "meets_duration_spread_requirement": duration_eligible,
                "meets_improvement_requirement": improvement_rate >= 15.0,
            }
        ],
        workload_count=1,
        average_improvement_rate=improvement_rate,
        has_required_workload_count=False,
        all_duration_spreads_eligible=duration_eligible,
        all_tasks_succeeded=tasks_succeeded,
        meets_average_improvement_requirement=improvement_rate >= 15.0,
        meets_contract_target=False,
    )
    return result.model_dump(mode="json")


def _task(
    *,
    result: dict | None = None,
    status: TaskStatus = TaskStatus.SUCCEEDED,
    task_type: TaskType = TaskType.SCHEDULE_COMPARISON,
    metadata: dict | None = None,
) -> TaskRecord:
    return TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CO_DEBUG,
        project_id=uuid4(),
        task_type=task_type,
        status=status,
        command=["comparison"],
        created_at=datetime.now(UTC),
        result=_summary() if result is None else result,
        metadata=(
            {
                "comparison_kind": "debug-batch",
                "debug_workload_manifest": "debug-workloads.json",
            }
            if metadata is None
            else metadata
        ),
    )


def _service(tasks: list[TaskRecord]) -> DebugComparisonSummaryService:
    return DebugComparisonSummaryService(
        task_service=FakeTaskService(tasks),  # type: ignore[arg-type]
        metric_service=AcceptanceMetricService(),
    )


def test_summary_supports_one_two_three_and_four_samples_in_request_order() -> None:
    tasks = [_task(result=_summary(rate)) for rate in [10.0, 20.0, 30.0, 40.0]]
    for sample_count in range(1, 5):
        selected = tasks[:sample_count]
        summary = _service(tasks).summarize([task.id for task in selected])
        assert summary.sample_count == sample_count
        assert [item.comparison_task_id for item in summary.comparison_results] == [
            task.id for task in selected
        ]
        assert summary.average_improvement_rate == sum(
            task.result["average_improvement_rate"] for task in selected
        ) / sample_count


def test_summary_aggregates_eligibility_success_and_threshold() -> None:
    first = _task(result=_summary(20.0))
    second = _task(result=_summary(10.0, duration_eligible=False, tasks_succeeded=False))
    summary = _service([first, second]).summarize([first.id, second.id])
    assert summary.average_improvement_rate == 15.0
    assert summary.required_improvement_rate == 15.0
    assert summary.meets_average_improvement_requirement is True
    assert summary.all_duration_spreads_eligible is False
    assert summary.all_tasks_succeeded is False


def test_summary_reads_build_id_and_accepts_legacy_debug_metadata() -> None:
    build_task_id = uuid4()
    current = _task(
        metadata={
            "comparison_kind": "debug-batch",
            "debug_workload_manifest": "debug-workloads.json",
            "build_task_id": str(build_task_id),
        }
    )
    legacy = _task(metadata={"debug_workload_manifest": "debug-workloads.json"})
    summary = _service([current, legacy]).summarize([current.id, legacy.id])
    assert summary.comparison_results[0].build_task_id == build_task_id
    assert summary.comparison_results[1].build_task_id is None


def test_summary_rejects_duplicate_ids() -> None:
    task = _task()
    with pytest.raises(AppError, match="must not contain duplicates"):
        _service([task]).summarize([task.id, task.id])


@pytest.mark.parametrize(
    ("task", "message"),
    [
        (_task(task_type=TaskType.BUILD), "not a schedule comparison"),
        (_task(metadata={}), "not a debug comparison"),
        (_task(metadata={"comparison_kind": "other"}), "not a debug comparison"),
        (_task(status=TaskStatus.PENDING), "must be succeeded"),
        (_task(status=TaskStatus.RUNNING), "must be succeeded"),
        (_task(status=TaskStatus.FAILED), "must be succeeded"),
        (_task(status=TaskStatus.CANCELLED), "must be succeeded"),
        (_task(result={}), "result is invalid"),
        (_task(result={"workload_count": 1}), "result is invalid"),
    ],
)
def test_summary_rejects_ineligible_or_invalid_tasks(task: TaskRecord, message: str) -> None:
    with pytest.raises(AppError, match=message):
        _service([task]).summarize([task.id])


def test_summary_uses_persisted_result_instead_of_client_metrics() -> None:
    task = _task(result=_summary(37.5))
    summary = _service([task]).summarize([task.id])
    assert summary.average_improvement_rate == 37.5


@pytest.mark.asyncio
async def test_summary_route_validates_empty_input_and_preserves_not_found_semantics() -> None:
    app = create_app()

    async def override_summary_service() -> DebugComparisonSummaryService:
        return _service([])

    app.dependency_overrides[get_debug_comparison_summary_service] = override_summary_service
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            empty = await client.post(
                "/api/v1/modules/co-debug/metrics/debug-comparison-summary",
                json={"comparison_task_ids": []},
            )
            missing = await client.post(
                "/api/v1/modules/co-debug/metrics/debug-comparison-summary",
                json={"comparison_task_ids": [str(uuid4())]},
            )
        assert empty.status_code == 422
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_summary_route_returns_enveloped_persisted_aggregate() -> None:
    task = _task(result=_summary(22.5))
    app = create_app()

    async def override_summary_service() -> DebugComparisonSummaryService:
        return _service([task])

    app.dependency_overrides[get_debug_comparison_summary_service] = override_summary_service
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/modules/co-debug/metrics/debug-comparison-summary",
                json={"comparison_task_ids": [str(task.id)]},
            )
            duplicate = await client.post(
                "/api/v1/modules/co-debug/metrics/debug-comparison-summary",
                json={"comparison_task_ids": [str(task.id), str(task.id)]},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["data"]["sample_count"] == 1
        assert payload["data"]["average_improvement_rate"] == 22.5
        assert payload["data"]["comparison_results"][0]["comparison_task_id"] == str(
            task.id
        )
        assert duplicate.status_code == 400
    finally:
        app.dependency_overrides.clear()
