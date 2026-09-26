from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ImprovementResult(BaseModel):
    fifo_millis: int
    optimized_millis: int
    improvement_rate: float


class DebugComparisonSummaryRequest(BaseModel):
    comparison_task_ids: list[UUID] = Field(min_length=1)


class DebugComparisonHistoryResult(BaseModel):
    comparison_task_id: UUID
    project_id: UUID
    build_task_id: UUID | None
    created_at: datetime
    average_improvement_rate: float
    all_duration_spreads_eligible: bool
    all_tasks_succeeded: bool


class DebugComparisonAggregateResponse(BaseModel):
    sample_count: int
    comparison_results: list[DebugComparisonHistoryResult]
    all_duration_spreads_eligible: bool
    all_tasks_succeeded: bool
    average_improvement_rate: float
    required_improvement_rate: float
    meets_average_improvement_requirement: bool
