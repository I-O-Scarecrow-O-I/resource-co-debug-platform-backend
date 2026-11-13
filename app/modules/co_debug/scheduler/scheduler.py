from app.modules.co_debug.scheduler.contracts import TaskContext
from app.modules.co_debug.schemas.scheduler import SchedulePlan, TaskAssignment
from app.platform.domain.enums import SchedulerStrategy
from app.platform.schemas.tasks import TaskExecutionSpec


# 规划层只消费任务规格和核心编号，不创建工作区或进程。
# 通过回调接入平台日志、进度和取消状态，避免算法依赖任务存储。
# 输出保留估时与队列位置，执行器不需要再次决定任务映射。
def plan_tasks(
    strategy: SchedulerStrategy,
    tasks: list[TaskExecutionSpec],
    context: TaskContext,
    core_ids: list[int] | None = None,
) -> SchedulePlan:
    # 先规范化核心列表再检查任务，使任何策略都从相同输入边界开始。
    # 此处不读取系统可用核，也不尝试设置 CPU 亲和性。
    normalized_cores = _normalize_core_ids(core_ids)
    _validate_tasks(tasks)

    # 校验结束后再分配，确保两种策略面对相同的输入约束。
    # 阶段回调是取消检查点，规划器本身不持有后台任务。
    context.log(f"planning {len(tasks)} tasks with strategy {strategy.value}")
    context.progress(20, "validating tasks and CPU cores")
    context.check_cancelled()

    # 基线与资源策略的差别在计划阶段决定。
    # 下游执行器只识别分配结果，不再根据策略名称调整顺序。
    if strategy == SchedulerStrategy.FIFO_BASELINE:
        ordered = _fifo_plan(tasks)
        assignments, core_loads = _assign_fifo(ordered, normalized_cores)
    else:
        ordered = _resource_aware_plan(tasks)
        assignments, core_loads = _assign_least_loaded(ordered, normalized_cores)

    # 计划形成后再检查取消；被取消的任务不会返回一份可被误执行的计划。
    context.progress(70, "task-to-core assignments generated")
    context.check_cancelled()

    # 总估时表示串行工作量；最大核心负载才对应计划完成时间。
    # 这两项取自任务估算，实际耗时由执行阶段独立测量。
    estimated_total = sum(task.estimated_ms for task in ordered)
    estimated_makespan = max(core_loads.values(), default=0)
    notes = [
        "The co_debug scheduler is called as an ordinary Python function by the platform.",
        "The platform owns process execution, cancellation state, and frontend log streaming.",
        "FIFO uses static round-robin core mapping with a FIFO queue on each core.",
        (
            "RESOURCE_AWARE uses longest-processing-time-first and assigns each task "
            "to the least-loaded core."
        ),
    ]
    # 规划进度达到本阶段末端，不表示进程已启动或任何子任务已完成。
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


# 保留提交顺序，同时返回独立列表供后续生成分配结果。
def _fifo_plan(tasks: list[TaskExecutionSpec]) -> list[TaskExecutionSpec]:
    return list(tasks)


# 优先放置长任务，让后续短任务填补相对空闲的核心。
# 排序稳定，同估时任务沿用提交顺序。
def _resource_aware_plan(tasks: list[TaskExecutionSpec]) -> list[TaskExecutionSpec]:
    return sorted(tasks, key=lambda task: task.estimated_ms, reverse=True)


# 纯规划入口默认使用一个逻辑核心；可用核由服务层提供。
# 显式空列表是无效分配，不能被默认值掩盖。
def _normalize_core_ids(core_ids: list[int] | None) -> list[int]:
    # 复制调用方列表，保持输入顺序与所有权。
    # 列表中的编号可以不连续，算法按列表成员分配。
    normalized = [0] if core_ids is None else list(core_ids)
    if not normalized:
        raise ValueError("core_ids must contain at least one CPU core")
    if any(core_id < 0 for core_id in normalized):
        raise ValueError("core_ids must not contain negative CPU core IDs")
    # 重复核心会折叠字典键并扭曲队列位置，因此作为无效输入拒绝。
    if len(set(normalized)) != len(normalized):
        raise ValueError("core_ids must not contain duplicate CPU core IDs")
    return normalized


# 执行器用任务名关联命令和结果，重名会破坏对应关系。
# 当前计划不表示依赖边，拒绝带依赖的任务以免错误并行。
def _validate_tasks(tasks: list[TaskExecutionSpec]) -> None:
    # 任务名称在单个负载内必须唯一，实际命令相同则仍可作为两个独立任务。
    names = [task.name for task in tasks]
    if len(set(names)) != len(names):
        raise ValueError("task names must be unique within a scheduler workload")
    # 调度计划目前不表示先决条件；拒绝依赖避免生成违背拓扑顺序的队列。
    if any(task.depends_on for task in tasks):
        raise ValueError("task dependencies are not supported by the minimal scheduler")


# 基线按输入顺序轮转核心，队列位置只在同一核心内比较。
# 每核累计估时用来推导下一个任务的预计开始时间。
def _assign_fifo(
    tasks: list[TaskExecutionSpec],
    core_ids: list[int],
) -> tuple[list[TaskAssignment], dict[int, int]]:
    # 每核从零负载和零位置开始，空闲核心仍记录在计划中。
    core_loads = {core_id: 0 for core_id in core_ids}
    queue_positions = {core_id: 0 for core_id in core_ids}
    assignments: list[TaskAssignment] = []

    # 基线的取模只依赖输入位置和核心顺序。
    # 一轮用完核心后回到第一核，不根据估时重新分配。
    for index, task in enumerate(tasks):
        core_id = core_ids[index % len(core_ids)]
        assignments.append(
            _create_assignment(
                task=task,
                core_id=core_id,
                queue_position=queue_positions[core_id],
                estimated_start_ms=core_loads[core_id],
            )
        )
        # 分配对象先读取旧负载作为起点，再把该任务加入累积负载。
        core_loads[core_id] += task.estimated_ms
        queue_positions[core_id] += 1

    return assignments, core_loads


# 优先选当前预计最早空闲的核心，同负载时按核心编号决胜。
# 平局规则让同一任务规格和核心集合得到可复验的计划。
def _assign_least_loaded(
    tasks: list[TaskExecutionSpec],
    core_ids: list[int],
) -> tuple[list[TaskAssignment], dict[int, int]]:
    core_loads = {core_id: 0 for core_id in core_ids}
    queue_positions = {core_id: 0 for core_id in core_ids}
    assignments: list[TaskAssignment] = []

    # 资源策略每次只看已分配负载。
    # 它是可复验的贪心近似，不承诺求得全局最优排布。
    for task in tasks:
        core_id = min(core_ids, key=lambda item: (core_loads[item], item))
        assignments.append(
            _create_assignment(
                task=task,
                core_id=core_id,
                queue_position=queue_positions[core_id],
                estimated_start_ms=core_loads[core_id],
            )
        )
        # 核心选择比较估时而非已运行的监控数据，计划不会随着实际子进程状态动态变化。
        core_loads[core_id] += task.estimated_ms
        queue_positions[core_id] += 1

    return assignments, core_loads


# 任务占用区间在单核队列内接续，估算不包含进程启动或调度开销。
def _create_assignment(
    task: TaskExecutionSpec,
    core_id: int,
    queue_position: int,
    estimated_start_ms: int,
) -> TaskAssignment:
    # 保留任务名与核内位置，使执行端可以不复制命令也能恢复队列。
    return TaskAssignment(
        task_name=task.name,
        core_id=core_id,
        queue_position=queue_position,
        estimated_start_ms=estimated_start_ms,
        estimated_finish_ms=estimated_start_ms + task.estimated_ms,
    )

