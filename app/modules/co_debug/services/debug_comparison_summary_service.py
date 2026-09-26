from uuid import UUID

from pydantic import ValidationError

from app.core.errors import AppError
from app.modules.co_debug.schemas.metrics import (
    DebugComparisonAggregateResponse,
    DebugComparisonHistoryResult,
)
from app.modules.co_debug.schemas.scheduler import ScheduleComparisonSummary
from app.modules.co_debug.services.metric_service import AcceptanceMetricService
from app.platform.domain.enums import TaskStatus, TaskType
from app.platform.services.task_service import TaskService


class DebugComparisonSummaryService:
    def __init__(
        self,
        task_service: TaskService,
        metric_service: AcceptanceMetricService,
    ) -> None:
        self.task_service = task_service
        self.metric_service = metric_service

    def summarize(
        self,
        comparison_task_ids: list[UUID],
    ) -> DebugComparisonAggregateResponse:
        if len(comparison_task_ids) != len(set(comparison_task_ids)):
            raise AppError("comparison_task_ids must not contain duplicates")

        comparison_results: list[DebugComparisonHistoryResult] = []
        improvement_rates: list[float] = []

        for task_id in comparison_task_ids:
            task = self.task_service.require_task(task_id)
            if task.task_type != TaskType.SCHEDULE_COMPARISON:
                raise AppError(f"task is not a schedule comparison: {task_id}")
            if not self._is_debug_comparison(task.metadata):
                raise AppError(f"task is not a debug comparison: {task_id}")
            if task.status != TaskStatus.SUCCEEDED:
                raise AppError(f"debug comparison task must be succeeded: {task_id}")

            try:
                summary = ScheduleComparisonSummary.model_validate(task.result)
            except ValidationError as exc:
                raise AppError(f"debug comparison result is invalid: {task_id}") from exc

            build_task_id = self._build_task_id(task.metadata, task_id)
            improvement_rates.append(summary.average_improvement_rate)
            comparison_results.append(
                DebugComparisonHistoryResult(
                    comparison_task_id=task.id,
                    project_id=task.project_id,
                    build_task_id=build_task_id,
                    created_at=task.created_at,
                    average_improvement_rate=summary.average_improvement_rate,
                    all_duration_spreads_eligible=summary.all_duration_spreads_eligible,
                    all_tasks_succeeded=summary.all_tasks_succeeded,
                )
            )

        average_improvement_rate = self.metric_service.average_improvement_rate(
            improvement_rates
        )
        required_improvement_rate = self.metric_service.REQUIRED_IMPROVEMENT_RATE
        return DebugComparisonAggregateResponse(
            sample_count=len(comparison_results),
            comparison_results=comparison_results,
            all_duration_spreads_eligible=all(
                result.all_duration_spreads_eligible for result in comparison_results
            ),
            all_tasks_succeeded=all(
                result.all_tasks_succeeded for result in comparison_results
            ),
            average_improvement_rate=average_improvement_rate,
            required_improvement_rate=required_improvement_rate,
            meets_average_improvement_requirement=(
                average_improvement_rate >= required_improvement_rate
            ),
        )

    @staticmethod
    def _is_debug_comparison(metadata: dict) -> bool:
        comparison_kind = metadata.get("comparison_kind")
        if comparison_kind is not None:
            return comparison_kind == "debug-batch"
        return "debug_workload_manifest" in metadata

    @staticmethod
    def _build_task_id(metadata: dict, comparison_task_id: UUID) -> UUID | None:
        value = metadata.get("build_task_id")
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise AppError(
                f"debug comparison build_task_id is invalid: {comparison_task_id}"
            ) from exc
