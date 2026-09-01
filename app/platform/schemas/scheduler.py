from pydantic import BaseModel

from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec


class TaskAssignment(BaseModel):
    task_name: str
    core_id: int
    queue_position: int
    estimated_start_ms: int
    estimated_finish_ms: int


class SchedulePlan(BaseModel):
    strategy: SchedulerStrategy
    core_ids: list[int]
    ordered_tasks: list[TaskExecutionSpec]
    assignments: list[TaskAssignment]
    core_loads_ms: dict[int, int]
    estimated_total_ms: int
    estimated_makespan_ms: int
    notes: list[str]


class ExecutedTaskResult(BaseModel):
    task_name: str
    core_id: int
    queue_position: int
    exit_code: int
    elapsed_ms: int
    started_offset_ms: int
    finished_offset_ms: int
    affinity_applied: bool


class ScheduleExecutionResult(BaseModel):
    task_results: list[ExecutedTaskResult]
    actual_makespan_ms: int
    all_succeeded: bool


class StrategyRunResult(BaseModel):
    plan: SchedulePlan
    execution: ScheduleExecutionResult


class WorkloadComparisonResult(BaseModel):
    workload_name: str
    cost_estimation_source: str
    fifo: StrategyRunResult
    optimized: StrategyRunResult
    duration_spread_rate: float
    improvement_rate: float
    meets_duration_spread_requirement: bool
    meets_improvement_requirement: bool


class ScheduleComparisonSummary(BaseModel):
    workload_results: list[WorkloadComparisonResult]
    workload_count: int
    average_improvement_rate: float
    has_required_workload_count: bool
    all_duration_spreads_eligible: bool
    all_tasks_succeeded: bool
    meets_average_improvement_requirement: bool
    meets_contract_target: bool

