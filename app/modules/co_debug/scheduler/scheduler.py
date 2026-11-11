from app.modules.co_debug.scheduler.contracts import TaskContext
from app.modules.co_debug.schemas.scheduler import SchedulePlan, TaskAssignment
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec


# 规划阶段只决定任务顺序和核心分配，不启动子进程。
# 同一份计划既可展示预估时间线，也可交给执行层按队列运行。
def plan_tasks(
    strategy: SchedulerStrategy,
    tasks: list[TaskExecutionSpec],
    context: TaskContext,
    core_ids: list[int] | None = None,
) -> SchedulePlan:
    # 先统一核心列表并校验任务，避免两种策略接受不同的输入。
    # 输入列表不会被原地排序，后续的对比运行仍能使用原始顺序。
    normalized_cores = _normalize_core_ids(core_ids)
    _validate_tasks(tasks)

    context.log(f"planning {len(tasks)} tasks with strategy {strategy.value}")
    context.progress(20, "validating tasks and CPU cores")
    # 日志和进度由调用方处理；在生成分配表前响应取消。
    context.check_cancelled()

    # FIFO 的分配不看耗时，保留一个不利用负载信息的对照基线。
    # 优化策略先排长任务，再把它分给当前预计最早空闲的核心。
    if strategy == SchedulerStrategy.FIFO_BASELINE:
        ordered = _fifo_plan(tasks)
        assignments, core_loads = _assign_fifo(ordered, normalized_cores)
    else:
        ordered = _resource_aware_plan(tasks)
        assignments, core_loads = _assign_least_loaded(ordered, normalized_cores)

    context.progress(70, "task-to-core assignments generated")
    # 即使分配已计算完，任务取消后也不再交付可执行计划。
    context.check_cancelled()

    # 总耗时是逐任务成本之和；并行完工时间由最慢的核心队列决定。
    # 这些都是预估值，正式的策略对比使用执行层返回的实测时间。
    estimated_total = sum(task.estimated_ms for task in ordered)
    estimated_makespan = max(core_loads.values(), default=0)
    # 将策略假设带回前端，方便解释计划与实测结果的差异。
    notes = [
        "The co_debug scheduler is called as an ordinary Python function by the platform.",
        "The platform owns process execution, cancellation state, and frontend log streaming.",
        "FIFO uses static round-robin core mapping with a FIFO queue on each core.",
        (
            "RESOURCE_AWARE uses longest-processing-time-first and assigns each task "
            "to the least-loaded core."
        ),
    ]
    context.progress(100, "schedule plan ready")
    return SchedulePlan(
        strategy=strategy,
        core_ids=normalized_cores,
        ordered_tasks=ordered,
        assignments=assignments,
        core_loads_ms=core_loads,
        estimated_total_ms=estimated_total,
        estimated_makespan_ms=estimated_makespan,
        notes=notes,
    )


def _fifo_plan(tasks: list[TaskExecutionSpec]) -> list[TaskExecutionSpec]:
    # 复制容器保留提交顺序，不复制任务对象；分配过程只读任务字段。
    return list(tasks)


def _resource_aware_plan(tasks: list[TaskExecutionSpec]) -> list[TaskExecutionSpec]:
    # 最长任务优先可以减少长任务堆在某条队列尾部的机会。
    # Python 的稳定排序使估时相同的任务仍按提交顺序排列。
    return sorted(tasks, key=lambda task: task.estimated_ms, reverse=True)


def _normalize_core_ids(core_ids: list[int] | None) -> list[int]:
    # 直接调用规划器时默认生成单核计划；宿主机可用核由服务层提供。
    # 保留输入顺序，因为它决定 FIFO 的轮转次序。
    normalized = [0] if core_ids is None else list(core_ids)
    if not normalized:
        raise ValueError("core_ids must contain at least one CPU core")
    # 这里只能判断编号是否合法；进程能否使用该核取决于运行环境。
    if any(core_id < 0 for core_id in normalized):
        raise ValueError("core_ids must not contain negative CPU core IDs")
    # 重复编号会在队列字典里合并，导致分配次数与计划核心数不一致。
    if len(set(normalized)) != len(normalized):
        raise ValueError("core_ids must not contain duplicate CPU core IDs")
    return normalized


def _validate_tasks(tasks: list[TaskExecutionSpec]) -> None:
    # 分配和执行结果通过任务名关联，重名会让执行层取错任务。
    names = [task.name for task in tasks]
    if len(set(names)) != len(names):
        raise ValueError("task names must be unique within a scheduler workload")
    # 当前策略假设任务彼此独立，不把依赖任务当作可随时并行的作业。
    if any(task.depends_on for task in tasks):
        raise ValueError("task dependencies are not supported by the minimal scheduler")


def _assign_fifo(
    tasks: list[TaskExecutionSpec],
    core_ids: list[int],
) -> tuple[list[TaskAssignment], dict[int, int]]:
    # 每核分别维护累计耗时和排队序号，序号不取全局任务下标。
    # 空核心也保留零负载，使输出覆盖调用方请求的全部核心。
    core_loads = {core_id: 0 for core_id in core_ids}
    queue_positions = {core_id: 0 for core_id in core_ids}
    assignments: list[TaskAssignment] = []

    for index, task in enumerate(tasks):
        # 用下标轮转分核；同一核心内的相对顺序与输入一致。
        core_id = core_ids[index % len(core_ids)]
        assignments.append(
            _create_assignment(
                task=task,
                core_id=core_id,
                queue_position=queue_positions[core_id],
                estimated_start_ms=core_loads[core_id],
            )
        )
        # 先记录当前累计值作为开工时间，再把本任务计入队列负载。
        core_loads[core_id] += task.estimated_ms
        queue_positions[core_id] += 1

    return assignments, core_loads


def _assign_least_loaded(
    tasks: list[TaskExecutionSpec],
    core_ids: list[int],
) -> tuple[list[TaskAssignment], dict[int, int]]:
    # 这里收到的任务已按估时从长到短排列，只负责选择核心。
    # 负载是本轮计划的累计时长，不是操作系统报告的实时 CPU 利用率。
    core_loads = {core_id: 0 for core_id in core_ids}
    queue_positions = {core_id: 0 for core_id in core_ids}
    assignments: list[TaskAssignment] = []

    for task in tasks:
        # 相同负载时取较小的核心编号，让重复规划得到确定的结果。
        # 只比较既有负载即可，因为当前任务加到任一核心的预估成本相同。
        core_id = min(core_ids, key=lambda item: (core_loads[item], item))
        assignments.append(
            _create_assignment(
                task=task,
                core_id=core_id,
                queue_position=queue_positions[core_id],
                estimated_start_ms=core_loads[core_id],
            )
        )
        # 分配后立即更新该核心负载，下一任务要看到本次选择的影响。
        core_loads[core_id] += task.estimated_ms
        queue_positions[core_id] += 1

    return assignments, core_loads


def _create_assignment(
    task: TaskExecutionSpec,
    core_id: int,
    queue_position: int,
    estimated_start_ms: int,
) -> TaskAssignment:
    # 起止值是相对本轮起点的毫秒数，用于画计划时间线。
    # 执行层会另外记录实际起止偏移，不用这里的估值充当实测数据。
    return TaskAssignment(
        task_name=task.name,
        core_id=core_id,
        queue_position=queue_position,
        estimated_start_ms=estimated_start_ms,
        estimated_finish_ms=estimated_start_ms + task.estimated_ms,
    )

