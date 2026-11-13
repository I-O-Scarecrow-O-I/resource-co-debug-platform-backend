import os
from collections.abc import Callable
from uuid import UUID

from app.modules.co_debug.scheduler.contracts import TaskContext
from app.modules.co_debug.scheduler.scheduler import plan_tasks
from app.modules.co_debug.schemas.scheduler import SchedulePlan
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec

LogCallback = Callable[[str, str], None]
ProgressCallback = Callable[[int, str], None]


class SchedulerService:
    # 服务层把局部规划进度映射到父任务分配的区间。
    # 同一算法可作为独立规划阶段，也可嵌入多策略对比。
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
        # 局部百分比经线性缩放后交给父任务，事件文本沿用算法给出的阶段提示。
        # 规划的日志和取消归同一个父任务。
        # 上下文不授予算法写任务记录或启动运行器的权限。
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
        # 调用方可选择可用核心的子集，不能声明当前进程不可用的核心。
        # 保留请求顺序，FIFO 的轮转从该顺序的首项开始。
        # 把算法的局部百分比线性映射为父任务进度。
        # 比较任务可给多轮不重叠区间，无需在算法内部知道轮次。
        allowed_cores = _default_core_ids()
        # 默认使用当前进程可用的所有逻辑核心。
        # 调用方给出子集时不重排，以保持基线映射定义。
        selected_cores = allowed_cores if core_ids is None else core_ids
        # 集合差只用于报告不存在的编号，不改变原始选择顺序或去重。
        # 空列表和重复核心由计划器统一拒绝。
        invalid_cores = sorted(set(selected_cores) - set(allowed_cores))
        # 无效核在执行前就报告，不启动注定无法遵守核心配置的负载。
        if invalid_cores:
            # 无效显式选择直接失败，不悄悄替换为所有可用核心。
            # 这使重复实验的核心集合仍由实际请求决定。
            raise ValueError(
                f"core_ids contain unavailable CPU cores: {invalid_cores}; "
                f"allowed cores are {allowed_cores}"
            )
        # 计划函数收到已选核心及绑定本次任务的回调；不再读取系统设置。
        return plan_tasks(
            strategy=strategy,
            tasks=tasks,
            context=context,
            core_ids=selected_cores,
        )


# 支持查询亲和性时，优先使用当前进程可用的核心集合。
# 系统逻辑核心总数可能包含容器或进程配额之外的核心。
def _default_core_ids() -> list[int]:
    # 亲和性能力按平台探测，而不是假设所有环境都提供 POSIX 接口。
    get_affinity = getattr(os, "sched_getaffinity", None)
    # 亲和查询使用当前进程作为观察对象，计划不能假定宿主全部逻辑 CPU 都可分配。
    if get_affinity is not None:
        try:
            # 优先按当前进程实际被允许的核心集合规划。
            # 容器或父进程限制可能使它不是从零连续编号。
            allowed_cores = sorted(get_affinity(0))
        # 查询失败只表示无法得知进程限制，回退仍可得到非空逻辑编号集合。
        except OSError:
            pass
        else:
            # 空亲和集合与查询异常都走回退，避免生成没有可用核心的计划输入。
            if allowed_cores:
                return allowed_cores

    # 无法取得亲和性时退回逻辑编号；这里不承诺运行时能够绑核。
    # 无法获得非空亲和集合时，退回系统报告的逻辑核数。
    # 该回退只用于生成计划，不证明当前系统能应用绑定。
    return list(range(os.cpu_count() or 1))

