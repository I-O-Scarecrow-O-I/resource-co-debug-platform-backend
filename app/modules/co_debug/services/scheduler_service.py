import os
from collections.abc import Callable
from uuid import UUID

from app.modules.co_debug.scheduler.contracts import TaskContext
from app.modules.co_debug.scheduler.scheduler import plan_tasks
from app.modules.co_debug.schemas.scheduler import SchedulePlan
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec

LogCallback = Callable[[str, str], None]
# 进度回调由平台注入，便于单元测试直接记录规划事件。
ProgressCallback = Callable[[int, str], None]


# 将平台任务的进度区间和可用 CPU 核转换为纯规划器的输入。
# 单策略实验与双策略对比共用这层，不各自解释核心编号。
class SchedulerService:
    def create_plan(
        self,
        task_id: UUID,
        strategy: SchedulerStrategy,
        tasks: list[TaskExecutionSpec],
        on_log: LogCallback,
        on_progress: ProgressCallback,
        is_cancelled: Callable[[], bool],
        core_ids: list[int] | None = None,
        progress_start: int = 10,
        progress_end: int = 35,
    ) -> SchedulePlan:
        # 规划器只报告自身的 0–100；平台任务可能还有执行阶段。
        # 将规划进度缩放到调用方的区间，避免计划生成时整项任务已显示完成。
        # 日志默认归入调度流，同时允许规划器给具体事件指定来源。
        context = TaskContext(
            task_id=task_id,
            log=lambda message, stream="co_debug.scheduler": on_log(message, stream),
            progress=lambda percent, message: on_progress(
                progress_start
                + round(percent * (progress_end - progress_start) / 100),
                message,
            ),
            is_cancelled=is_cancelled,
        )
        # 缺省时使用当前进程可运行的核心；显式传入时保留调用方的顺序。
        # 不把非法编号替换成其他核心，否则对比的资源配置会悄然变化。
        allowed_cores = _default_core_ids()
        selected_cores = allowed_cores if core_ids is None else core_ids
        invalid_cores = sorted(set(selected_cores) - set(allowed_cores))
        # 此处校验宿主环境；重复编号、空列表等结构约束仍由规划器校验。
        if invalid_cores:
            raise ValueError(
                f"core_ids contain unavailable CPU cores: {invalid_cores}; "
                f"allowed cores are {allowed_cores}"
            )
        # 这里不启动作业；执行服务会按计划再申请实际 CPU 亲和性。
        # 原样传递任务，避免接入层提前排序后改变 FIFO 的基准输入。
        return plan_tasks(
            strategy=strategy,
            tasks=tasks,
            context=context,
            core_ids=selected_cores,
        )


def _default_core_ids() -> list[int]:
    # 容器或受限进程的可用核可能不连续，也可能少于系统报告的总数。
    # 支持时优先用当前进程的亲和性掩码，避免计划选中无法绑定的核。
    get_affinity = getattr(os, "sched_getaffinity", None)
    if get_affinity is not None:
        try:
            allowed_cores = sorted(get_affinity(0))
        except OSError:
            # 查询失败时走与不支持该接口的平台相同的退化路径。
            pass
        else:
            if allowed_cores:
                return allowed_cores

    # Windows 等平台没有该接口；无法取得核心数量时仍可生成单核计划。
    # 这里仅选择计划编号，运行时是否实际绑定由执行结果单独记录。
    return list(range(os.cpu_count() or 1))

