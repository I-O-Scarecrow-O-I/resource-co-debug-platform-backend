from pydantic import BaseModel

from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec


# 队列位置是单核局部顺序；预计时间使用计划起点为零点。
class TaskAssignment(BaseModel):
    # 任务名连接原始命令与分配；同名任务不允许出现在同一负载中。
    task_name: str
    core_id: int
    # 这个位置从零开始且只在同核内比较，不能用于重建不同核心之间的实际先后。
    queue_position: int
    estimated_start_ms: int
    estimated_finish_ms: int


# 计划描述分配而不是进程状态，可独立展示和复用执行。
class SchedulePlan(BaseModel):
    # 核心列表记录本次候选范围，包括最终没有任务的核心。
    strategy: SchedulerStrategy
    core_ids: list[int]
    # 策略排序和实际分配是不同视图；并发执行也不会按这份列表逐条启动全部任务。
    ordered_tasks: list[TaskExecutionSpec]
    assignments: list[TaskAssignment]
    # 每核负载的最大值对应预计总历时；任务估时之和是另一项工作量指标。
    core_loads_ms: dict[int, int]
    estimated_total_ms: int
    estimated_makespan_ms: int
    notes: list[str]


# 同一任务同时报告计划位置和实测结果，供前端在一条时间线上对照。
class ExecutedTaskResult(BaseModel):
    # 任务名和队列位置保持计划身份，用于与预计区间对齐。
    task_name: str
    core_id: int
    queue_position: int
    # 进程的非零退出记录仍保留，单独对执行结果的成功条件产生影响。
    exit_code: int
    elapsed_ms: int
    # 偏移相对本次策略运行的统一起点；单任务耗时取自运行器。
    started_offset_ms: int
    finished_offset_ms: int
    # 核心编号为目标核心，只有此标记为真才表示 OS 实际绑定。
    affinity_applied: bool


# 总历时按墙钟计算，各并发子任务耗时不可直接相加。
class ScheduleExecutionResult(BaseModel):
    # 交付时列表按分配顺序稳定排列；真实结束先后请看偏移字段。
    task_results: list[ExecutedTaskResult]
    actual_makespan_ms: int
    all_succeeded: bool


# 成对保留计划和实测，便于定位策略分配与运行差异。
class StrategyRunResult(BaseModel):
    # 保留计划防止把事后实测时长误当成当时用于分配的估计。
    plan: SchedulePlan
    execution: ScheduleExecutionResult


# 两种策略应对应同一工作负载；记录优化估时来源供验收核对。
class WorkloadComparisonResult(BaseModel):
    # 工作负载名称用于关联两轮证据，不用策略名称代替同一组输入的身份。
    workload_name: str
    cost_estimation_source: str
    fifo: StrategyRunResult
    optimized: StrategyRunResult
    # 离散度来自基线单任务耗时，收益来自基线与优化总历时。
    duration_spread_rate: float
    improvement_rate: float
    # 离散度和收益的布尔标志各自对应自己的数值门槛，可独立失败。
    meets_duration_spread_requirement: bool
    meets_improvement_requirement: bool


# 汇总分别保留每项验收条件，整体布尔值不能替代各负载证据。
class ScheduleComparisonSummary(BaseModel):
    workload_results: list[WorkloadComparisonResult]
    # 负载数从真实对比结果计数；前端不用由子任务数量反推有效样本数。
    workload_count: int
    average_improvement_rate: float
    has_required_workload_count: bool
    all_duration_spreads_eligible: bool
    # 任务成功是独立门槛，不能用较短的失败运行证明耗时改进。
    all_tasks_succeeded: bool
    meets_average_improvement_requirement: bool
    # 最终结论由样本数、离散度、退出码和平均收益共同约束。
    meets_contract_target: bool

