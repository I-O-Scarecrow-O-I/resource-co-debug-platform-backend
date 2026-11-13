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
        # 汇总依赖平台持久任务，不查询实时 GDB 通道或再次运行比较。
        self.task_service = task_service
        self.metric_service = metric_service

    # 按指定任务汇总已完成的调试对比，不触发新的构建或调试。
    # 重复 ID 会给同一历史结果加权，因此整组请求拒绝。
    def summarize(
        self,
        comparison_task_ids: list[UUID],
    ) -> DebugComparisonAggregateResponse:
        # 同一个任务 ID 不能多次计入平均值，防止请求方改变样本权重。
        # 每个比较任务只计一次，避免重复 ID 改变样本权重。
        # 先拒绝重复 ID，避免同一实测样本被意外重复计权。
        if len(comparison_task_ids) != len(set(comparison_task_ids)):
            # 去重由调用端明确完成，服务拒绝重复 ID 而不悄悄改变样本权重。
            raise AppError("comparison_task_ids must not contain duplicates")

        # 保留请求顺序构造历史条目，聚合不重新筛选最新或最大收益样本。
        comparison_results: list[DebugComparisonHistoryResult] = []
        # 收益单独收集为每个已存比较任务的一项。
        # 一个任务内部可能有多份负载，聚合接口不再展开或重新加权。
        improvement_rates: list[float] = []

        # 任务类型只证明做过调度对比，元数据另外区分调试与普通负载。
        # 仅接受成功终态，以免缺少实测数据的中间状态进入样本。
        # 要求真实的调试比较历史且平台已成功；普通调度任务不能纳入。
        for task_id in comparison_task_ids:
            # 每个 ID 都需查到原任务；不存在时由平台报告，不能悄悄跳过坏样本。
            task = self.task_service.require_task(task_id)
            # 调度类型只说明执行路径；还必须确认它来自调试清单入口。
            # 调试比较由通用比较任务承载，因此任务类型与调试来源标签缺一不可。
            if task.task_type != TaskType.SCHEDULE_COMPARISON:
                raise AppError(f"task is not a schedule comparison: {task_id}")
            # 来源标签由调试入口设置；普通比较结果结构相同也不纳入这组历史。
            if not self._is_debug_comparison(task.metadata):
                raise AppError(f"task is not a debug comparison: {task_id}")
            # 仅成功任务可参与聚合，失败结果保留在原任务供诊断。
            if task.status != TaskStatus.SUCCEEDED:
                # 终态成功只是允许读取比较报告的前置条件。
                # 单个任务的命令执行门槛仍由报告里的 all_tasks_succeeded 表达。
                raise AppError(f"debug comparison task must be succeeded: {task_id}")

            # 历史结果来自持久化记录，使用当前结果契约验证后才提取指标。
            # 损坏样本应可定位，不能静默排除后再计算均值。
            # 历史结果仍须符合当前比较摘要结构，否则整组输入报错，不静默少算样本。
            # 从已持久化的结果解析当前契约；字段损坏不能用零或空列表补出正式样本。
            try:
                # 历史任务的 result 是序列化对象，按当前模型校验后再读取字段。
                summary = ScheduleComparisonSummary.model_validate(task.result)
            except ValidationError as exc:
                # 存储结果形状不符合当前比较模型时明确拒绝。
                # 不回退到把缺项当成零收益，否则会把损坏报告混入有效样本。
                raise AppError(f"debug comparison result is invalid: {task_id}") from exc

            # 前端可用构建 ID 回溯生成调试程序的任务；老记录允许无链接。
            # 保留构建身份和工程身份，前端可追踪该样本使用的可执行文件来源。
            # 保留每次比较的项目、来源和独立指标，方便追溯聚合输入。
            # 构建 ID 作为来源信息，不改变该样本的收益计算。
            build_task_id = self._build_task_id(task.metadata, task_id)
            # 每条比较任务贡献一个自己的平均值，避免把多负载任务按负载数额外加权。
            improvement_rates.append(summary.average_improvement_rate)
            # 响应保留样本的工程、构建与创建时刻，供调用者回查原始结果。
            # 历史样本暴露比较任务身份及可用的构建来源。
            # 这些字段可供定位原结果，不是对构建产物仍存在的承诺。
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

        # 每个已完成对比提供一个平均收益，跨对比的汇总保持等权。
        # 耗时离散与执行成功单独返回，便于区分实验门槛与结果。
        # 跨任务平均使用各任务的汇总值，不再次遍历底层命令或重算时耗。
        # 对每次比较的平均提升再求平均，各比较任务是同权样本。
        # 执行成功与耗时分布是否合格独立于阈值判定。
        # 聚合只使用已存收益值；不重新计算单轮实测值或比较阈值。
        average_improvement_rate = self.metric_service.average_improvement_rate(
            improvement_rates
        )
        # 阈值随结果返回，前端无需在展示层硬编码另一套合同判断。
        # 读取当前口径的收益门槛用于本次聚合展示，历史任务的原结果不被修改。
        required_improvement_rate = self.metric_service.REQUIRED_IMPROVEMENT_RATE
        # 执行成功、离散度与平均收益各自保留，不把单个收益布尔值写成合同整体通过。
        # 聚合保留所有样本的负载资格与命令结果。
        # 达到均值门槛不能覆盖其中不合格的单项。
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
    # 新元数据携带明确来源；缺省标记的旧结果才靠清单字段兼容。
    def _is_debug_comparison(metadata: dict) -> bool:
        # 新记录有显式类型时以它为准；仅缺省时用旧清单字段识别。
        # 显式比较类型优先；缺省时才用旧版 manifest 元数据识别来源。
        # 有显式类型标签时以它为准，不能用旧清单字段覆盖一个不同的新类型。
        comparison_kind = metadata.get("comparison_kind")
        if comparison_kind is not None:
            # 显式类型出现时采用它的判断；仅旧记录缺少类型才看 manifest。
            # 有其他类型的任务不能因碰巧有同名字段被纳入。
            return comparison_kind == "debug-batch"
        # 只对未写类型标签的旧记录兼容清单字段，新的普通比较入口不可提交该保留键。
        return "debug_workload_manifest" in metadata

    @staticmethod
    def _build_task_id(metadata: dict, comparison_task_id: UUID) -> UUID | None:
        # 缺省构建关联属于可接受的历史记录，存在但不是 UUID 则属于损坏数据。
        # 构建来源可空；存在但不能解析为 UUID 时作为损坏元数据处理。
        # 允许没有构建来源的比较，例如目标本来包含在上传工程内。
        value = metadata.get("build_task_id")
        if value is None:
            return None
        # 存在关联时严格转为 UUID，损坏的历史元数据不会被悄悄当作缺省。
        # 存在来源值时必须能解析 UUID，避免把损坏的溯源信息当作无来源。
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            # 若写过来源却无法恢复为 UUID，指出出错的比较任务。
            # 缺少来源的旧记录则允许为空，两种情形需要区分。
            raise AppError(
                f"debug comparison build_task_id is invalid: {comparison_task_id}"
            ) from exc
