from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# 原始毫秒与派生收益一起返回，使用方可区分零基线和真实零收益。
# 原始耗时与派生提升率一起保留，便于检查基准和优化口径。
class ImprovementResult(BaseModel):
    fifo_millis: int
    optimized_millis: int
    improvement_rate: float


# 汇总引用已完成的对比任务，不接收调用方上传的预计算百分比。
# 调用方显式选择聚合样本；服务另行校验去重、类型和终态。
class DebugComparisonSummaryRequest(BaseModel):
    # 每项是平台任务标识，接口先要求非空列表再查询已保存结果。
    comparison_task_ids: list[UUID] = Field(min_length=1)


# 保存每个对比任务的工程与构建身份，便于追踪历史样本来源。
# 历史条目保留来源信息和独立的执行、分布判断，聚合后仍可定位异常样本。
class DebugComparisonHistoryResult(BaseModel):
    # 历史条目以比较任务为主键，普通调试会话任务不是比较证据。
    comparison_task_id: UUID
    project_id: UUID
    # 构建来源可缺省；此时仍用工程与任务 ID 追溯执行输入。
    build_task_id: UUID | None
    created_at: datetime
    # 平均收益和有效条件分别保存；两者不能相互替代。
    average_improvement_rate: float
    # 任务完成不证明所有子命令均成功，继续交付单独的退出检查。
    all_duration_spreads_eligible: bool
    all_tasks_succeeded: bool


# 样本数按任务计算，不按负载或每轮启动的进程数计算。
# 样本数以比较任务计，不按内部作业数计。
# 是否过平均提升门槛不能替代任务成功和时长分布条件。
class DebugComparisonAggregateResponse(BaseModel):
    sample_count: int
    comparison_results: list[DebugComparisonHistoryResult]
    # 聚合的有效性字段不折叠为一个收益值；调用方可显示到底哪一组条件未满足。
    all_duration_spreads_eligible: bool
    all_tasks_succeeded: bool
    # 收益使用百分数；调用方不要再乘一百。
    average_improvement_rate: float
    # 阈值随汇总返回，页面可展示服务端实际使用的数值。
    required_improvement_rate: float
    # 该标志只表示平均收益过阈值，不等同于所有其他条件同时成立。
    meets_average_improvement_requirement: bool
