from pydantic import BaseModel

from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec


# 分配条目是计划，不代表子进程已经启动或绑核成功。
class TaskAssignment(BaseModel):
    task_name: str
    core_id: int
    # 序号只在同一个核心内比较；起止时间以本轮起点为基准。
    queue_position: int
    estimated_start_ms: int
    estimated_finish_ms: int


class SchedulePlan(BaseModel):
    strategy: SchedulerStrategy
    core_ids: list[int]
    ordered_tasks: list[TaskExecutionSpec]
    assignments: list[TaskAssignment]
    # 总耗时累计所有任务，预计完工耗时只取最慢的核心队列。
    # 各核心负载单独返回，方便检查任务分配是否均衡。
    core_loads_ms: dict[int, int]
    estimated_total_ms: int
    estimated_makespan_ms: int
    notes: list[str]


class ExecutedTaskResult(BaseModel):
    task_name: str
    core_id: int
    queue_position: int
    exit_code: int
    # elapsed_ms 是子进程耗时，偏移用于把作业画在整轮实测时间线上。
    elapsed_ms: int
    started_offset_ms: int
    finished_offset_ms: int
    # 即使计划分配了核心，宿主环境也可能不支持实际绑核。
    affinity_applied: bool


class ScheduleExecutionResult(BaseModel):
    # 结果按计划顺序返回；整轮完工耗时不等于各子任务耗时之和。
    task_results: list[ExecutedTaskResult]
    actual_makespan_ms: int
    all_succeeded: bool


# 成对保存计划与实测，避免页面把估算值和实验值混成同一指标。
class StrategyRunResult(BaseModel):
    plan: SchedulePlan
    execution: ScheduleExecutionResult


class WorkloadComparisonResult(BaseModel):
    workload_name: str
    # 记录优化估值的来源，让结果能解释两轮的任务成本为何不同。
    cost_estimation_source: str
    fifo: StrategyRunResult
    optimized: StrategyRunResult
    # 差异率针对组内 FIFO 任务；改进率针对两轮整体完工时间。
    duration_spread_rate: float
    improvement_rate: float
    meets_duration_spread_requirement: bool
    # 单组改进率门槛供展示；整体验收用多组平均值。
    meets_improvement_requirement: bool


class ScheduleComparisonSummary(BaseModel):
    # 样本数不足时仍保留实验结果，调用方可区分联调和正式验收。
    workload_results: list[WorkloadComparisonResult]
    workload_count: int
    # 各组等权统计，不按任务数或基线耗时加权。
    average_improvement_rate: float
    has_required_workload_count: bool
    all_duration_spreads_eligible: bool
    all_tasks_succeeded: bool
    meets_average_improvement_requirement: bool
    # 汇总判定是各独立条件的交集；分项状态用于定位不通过原因。
    meets_contract_target: bool

