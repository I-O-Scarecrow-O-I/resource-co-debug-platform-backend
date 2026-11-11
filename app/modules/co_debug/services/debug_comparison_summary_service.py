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


# 汇总已保存的批处理调试记录，不重新执行原作业。
# 通用调度对比与调试对比使用同一任务类型，由任务元数据区分。
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
        # 每条历史任务只能贡献一次改进率，否则重复选择会改变样本权重。
        # 由调用方明确选样本；这里不把项目的所有历史任务自动纳入统计。
        if len(comparison_task_ids) != len(set(comparison_task_ids)):
            raise AppError("comparison_task_ids must not contain duplicates")

        comparison_results: list[DebugComparisonHistoryResult] = []
        improvement_rates: list[float] = []

        for task_id in comparison_task_ids:
            # 保持请求的顺序，返回的历史记录可与前端选择逐条对照。
            # 样本查找沿用统一任务服务，这里不直接依赖任务存储格式。
            task = self.task_service.require_task(task_id)
            # 先检查任务类型，再检查是否来自批处理调试入口。
            # 普通调度对比即使结果结构相同，也不能混入调试指标汇总。
            if task.task_type != TaskType.SCHEDULE_COMPARISON:
                raise AppError(f"task is not a schedule comparison: {task_id}")
            if not self._is_debug_comparison(task.metadata):
                raise AppError(f"task is not a debug comparison: {task_id}")
            # 未完成或失败的外层任务没有可用于汇总的正式结果。
            if task.status != TaskStatus.SUCCEEDED:
                raise AppError(f"debug comparison task must be succeeded: {task_id}")

            try:
                # 从持久化数据恢复结果模型；旧版本或残缺数据应报告具体任务编号。
                # 不用缺失字段的默认值继续计算，以免错误记录影响平均值。
                summary = ScheduleComparisonSummary.model_validate(task.result)
            except ValidationError as exc:
                raise AppError(f"debug comparison result is invalid: {task_id}") from exc

            # 构建任务是可选来源；工程内已有可执行文件时可能没有构建记录。
            # 保留工程和构建编号，使汇总条目能追溯到原始工作区来源。
            build_task_id = self._build_task_id(task.metadata, task_id)
            improvement_rates.append(summary.average_improvement_rate)
            comparison_results.append(
                DebugComparisonHistoryResult(
                    comparison_task_id=task.id,
                    project_id=task.project_id,
                    build_task_id=build_task_id,
                    created_at=task.created_at,
                    # 沿用原任务的门槛判断，不从四舍五入后的数值反推。
                    average_improvement_rate=summary.average_improvement_rate,
                    all_duration_spreads_eligible=summary.all_duration_spreads_eligible,
                    all_tasks_succeeded=summary.all_tasks_succeeded,
                )
            )

        # 对选中的对比记录等权平均；不是把各记录的全部底层作业合在一起重算。
        # 改进率是否过线与差异、成功状态分开返回，供调用方按实际口径展示。
        average_improvement_rate = self.metric_service.average_improvement_rate(
            improvement_rates
        )
        required_improvement_rate = self.metric_service.REQUIRED_IMPROVEMENT_RATE
        # 此接口只汇总所选记录，不把可选样本数当成完整合同通过的结论。
        # created_at 对应原对比任务创建时间，不是本次查询时间。
        return DebugComparisonAggregateResponse(
            sample_count=len(comparison_results),
            comparison_results=comparison_results,
            # 任一条记录不满足条件，汇总结果就不能标为全部满足。
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
        # 新记录以显式类型为准；旧记录没有类型时才回退到清单字段。
        # 已标成其他类型的记录不能仅凭带有清单字段就被识别为调试对比。
        comparison_kind = metadata.get("comparison_kind")
        if comparison_kind is not None:
            return comparison_kind == "debug-batch"
        return "debug_workload_manifest" in metadata

    @staticmethod
    def _build_task_id(metadata: dict, comparison_task_id: UUID) -> UUID | None:
        # 持久化后编号通常是字符串；统一转成响应模型使用的 UUID。
        value = metadata.get("build_task_id")
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            # 区分未提供来源和提供了无效编号，避免把损坏的关联当成无关联。
            raise AppError(
                f"debug comparison build_task_id is invalid: {comparison_task_id}"
            ) from exc
