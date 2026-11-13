from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.modules.co_debug.schemas.coordination import (
    DebugCoordinationEventResponse,
    DebugCoordinationGroupRequest,
    DebugCoordinationGroupResponse,
    DebugCoordinationRuleRequest,
    DebugCoordinationRuleResponse,
    DebugGroupCommandResponse,
)
from app.modules.co_debug.schemas.debug import (
    DebugArgumentsRequest,
    DebugBreakpointRequest,
    DebugBreakpointResponse,
    DebugBuildCandidate,
    DebugExpressionRequest,
    DebugExpressionResponse,
    DebugLocalVariablesResponse,
    DebugSessionResponse,
    DebugSessionStateResponse,
    DebugStackFramesResponse,
    DebugThreadSelectRequest,
    DebugThreadsResponse,
    DebugWaitForStopRequest,
)
from app.modules.co_debug.schemas.debug_workloads import DebugComparisonRequest
from app.modules.co_debug.schemas.dependencies import (
    DependencyAnalysisResponse,
    DependencyRepairBuildRequest,
    DependencyRepairResponse,
)
from app.modules.co_debug.schemas.metrics import (
    DebugComparisonAggregateResponse,
    DebugComparisonSummaryRequest,
)
from app.modules.co_debug.services.debug_comparison_summary_service import (
    DebugComparisonSummaryService,
)
from app.modules.co_debug.services.debug_coordination_service import (
    DebugCoordinationService,
)
from app.modules.co_debug.services.debug_service import DebugSessionService
from app.modules.co_debug.services.dependency_service import DependencyAnalysisService
from app.modules.co_debug.services.interactive_debug_service import (
    InteractiveDebugService,
)
from app.modules.co_debug.services.metric_service import AcceptanceMetricService
from app.modules.co_debug.services.repair_build_service import DependencyRepairBuildService
from app.modules.co_debug.services.task_service import CoDebugTaskService
from app.platform.api.deps import (
    get_co_debug_task_service,
    get_debug_comparison_summary_service,
    get_debug_coordination_service,
    get_debug_service,
    get_dependency_repair_build_service,
    get_dependency_service,
    get_interactive_debug_service,
    get_metric_service,
)
from app.platform.schemas.common import ApiResponse
from app.platform.schemas.tasks import DebugTaskRequest, TaskResponse

router = APIRouter()


# 只汇总客户端给定的历史比较任务；此入口不启动 GDB 或创建新的测量任务。
@router.post(
    "/metrics/debug-comparison-summary",
    response_model=ApiResponse[DebugComparisonAggregateResponse],
)
async def summarize_debug_comparisons(
    request: DebugComparisonSummaryRequest,
    service: Annotated[
        DebugComparisonSummaryService,
        Depends(get_debug_comparison_summary_service),
    ],
) -> ApiResponse[DebugComparisonAggregateResponse]:
    # 这条接口只接收比较任务标识，不接收客户端自行计算的耗时。
    # 服务从已保存任务的结果判断哪些记录可以聚合。
    return ApiResponse.ok(service.summarize(request.comparison_task_ids))


# 调试比较返回平台任务凭证，具体轮次、进程结果和指标随后从任务结果读取。
@router.post("/debug/comparisons", response_model=ApiResponse[TaskResponse])
async def create_debug_comparison(
    request: DebugComparisonRequest,
    service: Annotated[CoDebugTaskService, Depends(get_co_debug_task_service)],
) -> ApiResponse[TaskResponse]:
    # 创建成功后，用返回的任务 ID 订阅进度或读取结果。
    # HTTP 响应本身不包含尚未运行的策略轮次数据。
    task = await service.create_debug_schedule_comparison(request)
    return ApiResponse.ok(TaskResponse.from_record(task))


# 依赖分析是只读查询，结果区分声明、推断和疑似遗漏的边。
@router.post("/dependencies/analyze", response_model=ApiResponse[DependencyAnalysisResponse])
async def analyze_dependencies(
    project_id: UUID,
    dependency_service: Annotated[DependencyAnalysisService, Depends(get_dependency_service)],
) -> ApiResponse[DependencyAnalysisResponse]:
    # 工程 ID 是查找入口，不由请求指定任意磁盘根。
    # 服务以登记的工程源树为输入，结果不意味着已经应用修复。
    return ApiResponse.ok(dependency_service.analyze(project_id))

# 修复并构建由托管任务异步执行，可用平台任务接口继续读取日志及产物。
@router.post(
    "/dependencies/repair-build",
    response_model=ApiResponse[TaskResponse],
)
async def repair_and_build_dependencies(
    request: DependencyRepairBuildRequest,
    service: Annotated[
        DependencyRepairBuildService,
        Depends(
            get_dependency_repair_build_service
        ),
    ],
) -> ApiResponse[TaskResponse]:

    # 请求中的可选构建目标会随任务提交。
    # 平台后续完成副本准备与真实构建后，才可判断是否产生可调试产物。
    task = await service.create_task(
        request
    )

    return ApiResponse.ok(
        TaskResponse.from_record(task)
    )

# 预览入口只给修复内容，不改项目，也不启动构建。
# 应用修复并生成产物使用独立任务入口。
@router.post(
    "/dependencies/repair",
    response_model=ApiResponse[
        DependencyRepairResponse
    ],
)
async def repair_dependencies(
    project_id: UUID,
    dependency_service: Annotated[
        DependencyAnalysisService,
        Depends(get_dependency_service),
    ],
) -> ApiResponse[DependencyRepairResponse]:
    # 修复预览与构建产物有不同生命周期。
    # 预览中的补偿文本不进入任务下载列表，也不能作为已构建证明。
    return ApiResponse.ok(
        dependency_service.repair(project_id)
    )


# 候选来自成功且仍可读取的构建产物，响应路径相对于对应构建来源。
@router.get(
    "/debug/candidates",
    response_model=ApiResponse[
        list[DebugBuildCandidate]
    ],
)
async def list_debug_candidates(
    interactive_debug_service: Annotated[
        InteractiveDebugService,
        Depends(get_interactive_debug_service),
    ],
    project_id: UUID | None = None,
) -> ApiResponse[list[DebugBuildCandidate]]:
    # 未给项目 ID 时，不做这项过滤；各候选仍携带自己的任务归属。
    # 选中候选发起调试时必须保留该来源身份。
    return ApiResponse.ok(
        interactive_debug_service.list_candidates(
            project_id=project_id
        )
    )

# 描述可以查询历史 DEBUG 任务；是否有可操作的会话需看实时状态。
@router.get("/debug/sessions/{task_id}", response_model=ApiResponse[DebugSessionResponse])
async def describe_debug_session(
    task_id: UUID,
    debug_service: Annotated[DebugSessionService, Depends(get_debug_service)],
) -> ApiResponse[DebugSessionResponse]:
    # 这里描述的是登记记录，历史查询不要求活跃 MI 通道。
    # 调用者不能仅凭描述成功就继续发送控制命令。
    return ApiResponse.ok(debug_service.describe(task_id))

# 创建只返回任务登记；等待 MI 握手后才有可接受控制命令的会话。
@router.post(
    "/debug/sessions",
    response_model=ApiResponse[
        TaskResponse
    ],
)
async def create_debug_session(
    request: DebugTaskRequest,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
) -> ApiResponse[TaskResponse]:
    # 与复用历史构建产物的查找不同，此操作登记一个新的调试任务身份。
    # 路由不会在创建 HTTP 请求内等待调试进程结束。
    task = await debug_service.create(
        request
    )

    return ApiResponse.ok(
        TaskResponse.from_record(
            task
        )
    )


# 活动状态走实时会话；会话注销后仍可返回历史终态。
@router.get(
    "/debug/sessions/{task_id}/state",
    response_model=ApiResponse[
        DebugSessionStateResponse
    ],
)
async def get_debug_session_state(
    task_id: UUID,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
) -> ApiResponse[
    DebugSessionStateResponse
]:
    # 返回的状态是服务观察到的快照，调用期间进程仍可能前进。
    # 如果接下来要发有状态前提的命令，仍应接受服务重新检查。
    return ApiResponse.ok(
        await debug_service.state(
            task_id
        )
    )


# 设置的是被调试程序下次运行的参数，不改变 GDB 服务进程本身的启动参数。
@router.post(
    "/debug/sessions/{task_id}/arguments",
    response_model=ApiResponse[bool],
)
async def set_debug_arguments(
    task_id: UUID,
    request: DebugArgumentsRequest,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
) -> ApiResponse[bool]:
    # 即使参数为空数组，也作为显式设置提交。
    # 它与缺少任务 ID 或不存在会话的错误不是同一种结果。
    await debug_service.set_arguments(
        task_id,
        request.arguments,
    )

    return ApiResponse.ok(True)


# 断点选项分别传入协议层，HTTP 路由不把条件拼成控制命令。
@router.post(
    "/debug/sessions/{task_id}/breakpoints",
    response_model=ApiResponse[
        DebugBreakpointResponse
    ],
)
async def insert_debug_breakpoint(
    task_id: UUID,
    request: DebugBreakpointRequest,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
) -> ApiResponse[
    DebugBreakpointResponse
]:
    # 位置与可选条件由请求模型分别提供。
    # 路由只接收结构化字段，不定义路径或 GDB 条件语法。
    result = (
        await debug_service
        .insert_breakpoint(
            task_id,
            location=request.location,
            temporary=(
                request.temporary
            ),
            disabled=request.disabled,
            condition=request.condition,
        )
    )

    # 响应由 GDB 返回编号构成，调用方应保存该编号而不是按位置删除。
    return ApiResponse.ok(
        DebugBreakpointResponse(
            **result
        )
    )


# 删除按会话内的断点号定位，同位置的其他断点不在此请求中。
@router.delete(
    "/debug/sessions/{task_id}/breakpoints/{breakpoint_number}",
    response_model=ApiResponse[bool],
)
async def delete_debug_breakpoint(
    task_id: UUID,
    breakpoint_number: str,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
) -> ApiResponse[bool]:
    # 编号位于会话任务的 URL 下。
    # 同一个编号不能从其他会话的断点响应直接拿来使用。
    await (
        debug_service
        .delete_breakpoint(
            task_id,
            breakpoint_number,
        )
    )

    return ApiResponse.ok(True)


# 首次运行与继续执行是独立 API，状态快照来自命令返回时刻。
@router.post(
    "/debug/sessions/{task_id}/run",
    response_model=ApiResponse[
        DebugSessionStateResponse
    ],
)
async def run_debug_session(
    task_id: UUID,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
):
    return ApiResponse.ok(
        await debug_service.run(
            task_id
        )
    )


# 继续请求复用已存在会话与目标，不创建新的 DEBUG 平台任务。
@router.post(
    "/debug/sessions/{task_id}/continue",
    response_model=ApiResponse[
        DebugSessionStateResponse
    ],
)
async def continue_debug_session(
    task_id: UUID,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
):
    # 这里没有单独的等待时限字段，调用目标是继续而非阻塞到下一次命中。
    return ApiResponse.ok(
        await (
            debug_service
            .continue_execution(
                task_id
            )
        )
    )


# next 用于跨过调用；如需确认下一行现场，再通过等待或状态接口观察。
@router.post(
    "/debug/sessions/{task_id}/next",
    response_model=ApiResponse[
        DebugSessionStateResponse
    ],
)
async def next_debug_session(
    task_id: UUID,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
):
    return ApiResponse.ok(
        await debug_service.next(
            task_id
        )
    )


# step 允许进入调用；命令回执与目标实际停止为不同阶段。
@router.post(
    "/debug/sessions/{task_id}/step",
    response_model=ApiResponse[
        DebugSessionStateResponse
    ],
)
async def step_debug_session(
    task_id: UUID,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
):
    return ApiResponse.ok(
        await debug_service.step(
            task_id
        )
    )


# 中断 API 返回应答时的状态；仍为 RUNNING 时可通过等待接口核实暂停。
@router.post(
    "/debug/sessions/{task_id}/interrupt",
    response_model=ApiResponse[
        DebugSessionStateResponse
    ],
)
async def interrupt_debug_session(
    task_id: UUID,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
):
    return ApiResponse.ok(
        await debug_service.interrupt(
            task_id
        )
    )


# 等待入口受请求的停止期限约束；返回状态可能是暂停、退出或失败。
@router.post(
    "/debug/sessions/{task_id}/wait",
    response_model=ApiResponse[
        DebugSessionStateResponse
    ],
)
async def wait_for_debug_stop(
    task_id: UUID,
    request: DebugWaitForStopRequest,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
):
    # 停止等待预算通过请求传递给服务。
    # HTTP 处理不循环重复发起 run 或 continue 来模拟等待。
    return ApiResponse.ok(
        await debug_service.wait_for_stop(
            task_id,
            timeout_seconds=(
                request.timeout_seconds
            ),
        )
    )


# 表达式发送到当前 GDB 上下文；HTTP 只包装协议返回的数据。
@router.post(
    "/debug/sessions/{task_id}/evaluate",
    response_model=ApiResponse[
        DebugExpressionResponse
    ],
)
async def evaluate_debug_expression(
    task_id: UUID,
    request: DebugExpressionRequest,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
):
    # 仅把当前表达式送到已有会话。
    # 本路由不会自动切换线程或构造临时栈帧来解释表达式。
    result = (
        await debug_service.evaluate(
            task_id,
            request.expression,
        )
    )

    return ApiResponse.ok(
        DebugExpressionResponse(
            **result
        )
    )


# 栈帧只读取当前选中上下文的协议数据，不从源码文件重新推断。
@router.get(
    "/debug/sessions/{task_id}/stack-frames",
    response_model=ApiResponse[
        DebugStackFramesResponse
    ],
)
async def get_debug_stack_frames(
    task_id: UUID,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
):
    # 抓取的栈帧由服务转换为公开模型的字段。
    # 没有自行遍历项目源文件填补 GDB 未提供的帧。
    result = (
        await debug_service
        .stack_frames(task_id)
    )

    return ApiResponse.ok(
        DebugStackFramesResponse(
            **result
        )
    )


# 局部变量使用当前选中线程和帧，线程切换后查询结果可不同。
@router.get(
    "/debug/sessions/{task_id}/local-variables",
    response_model=ApiResponse[DebugLocalVariablesResponse],
)
async def get_debug_local_variables(
    task_id: UUID,
    debug_service: Annotated[DebugSessionService, Depends(get_debug_service)],
):
    return ApiResponse.ok(
        DebugLocalVariablesResponse(**await debug_service.local_variables(task_id))
    )


# 线程列表使用 GDB 编号，Linux 系统线程号只在可识别时作为附加字段返回。
@router.get(
    "/debug/sessions/{task_id}/threads",
    response_model=ApiResponse[DebugThreadsResponse],
)
async def get_debug_threads(
    task_id: UUID,
    debug_service: Annotated[DebugSessionService, Depends(get_debug_service)],
):
    # 线程快照可给后续选择请求提供有效的 GDB 编号。
    # 异步退出可能使编号失效，选择时仍须由服务验证。
    return ApiResponse.ok(DebugThreadsResponse(**await debug_service.threads(task_id)))


# 选择后回传刷新后的线程表，选中和断点命中仍是不同标记。
@router.post(
    "/debug/sessions/{task_id}/threads/select",
    response_model=ApiResponse[DebugThreadsResponse],
)
async def select_debug_thread(
    task_id: UUID,
    request: DebugThreadSelectRequest,
    debug_service: Annotated[DebugSessionService, Depends(get_debug_service)],
):
    # 线程选择与刷新放在同一服务调用里。
    # 响应中的命中标志不能据此推断选择动作触发了断点。
    return ApiResponse.ok(
        DebugThreadsResponse(
            **await debug_service.select_thread(task_id, request.thread_id)
        )
    )


# 关闭会话只返回最终调试快照；平台任务终态由托管执行层异步收尾。
@router.post(
    "/debug/sessions/{task_id}/close",
    response_model=ApiResponse[
        DebugSessionStateResponse
    ],
)
async def close_debug_session(
    task_id: UUID,
    debug_service: Annotated[
        DebugSessionService,
        Depends(get_debug_service),
    ],
):
    # 会话关闭使用独立的停止路径；不通过提交一个新的任务来结束旧任务。
    # 最终快照之外的进度和日志仍由任务接口查询。
    return ApiResponse.ok(
        await debug_service.close(
            task_id
        )
    )


# 组入口关联已有会话，并不创建新进程；逻辑分区身份独立于任务 ID。
@router.post(
    "/debug/groups",
    response_model=ApiResponse[DebugCoordinationGroupResponse],
)
async def create_debug_coordination_group(
    request: DebugCoordinationGroupRequest,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[DebugCoordinationGroupResponse]:
    # 协同组拥有关系和规则，不接管会话进程的创建。
    # 调用方传入的是已有成员身份，服务验证归属和当前可用性。
    return ApiResponse.ok(await service.create(request))


# 组列表可按项目过滤；活动成员数由查询时的会话快照计算。
@router.get(
    "/debug/groups",
    response_model=ApiResponse[list[DebugCoordinationGroupResponse]],
)
async def list_debug_coordination_groups(
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
    project_id: UUID | None = None,
) -> ApiResponse[list[DebugCoordinationGroupResponse]]:
    return ApiResponse.ok(await service.list(project_id))


@router.get(
    "/debug/groups/{group_id}",
    response_model=ApiResponse[DebugCoordinationGroupResponse],
)
async def get_debug_coordination_group(
    group_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[DebugCoordinationGroupResponse]:
    # 组详情由存储的关系与运行时会话快照组合。
    # 组存在不代表每个成员都处于可执行命令的状态。
    return ApiResponse.ok(await service.get(group_id))


# 按组与分区双重定位移除，成员的独立调试进程不受此请求终止。
@router.delete(
    "/debug/groups/{group_id}/members/{partition_id}",
    response_model=ApiResponse[DebugCoordinationGroupResponse],
)
async def remove_debug_coordination_member(
    group_id: UUID,
    partition_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[DebugCoordinationGroupResponse]:
    # 删除成员的路由使用逻辑分区身份。
    # 它既不是核心编号，也不要求客户端用 GDB 会话编号代替分区。
    return ApiResponse.ok(await service.remove_member(group_id, partition_id))


# 组状态与组详情使用同一响应形状，便于前端更新成员视图。
@router.get(
    "/debug/groups/{group_id}/state",
    response_model=ApiResponse[DebugCoordinationGroupResponse],
)
async def get_debug_coordination_group_state(
    group_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[DebugCoordinationGroupResponse]:
    # 状态路径保留与详情相同的查询语义。
    # 无需客户端分别合并两份互相独立的组状态。
    return ApiResponse.ok(await service.get(group_id))


# 规则在登记时校验两个活动成员和源断点，动作目标不是任意命令文本。
@router.post(
    "/debug/groups/{group_id}/rules",
    response_model=ApiResponse[DebugCoordinationRuleResponse],
)
async def create_debug_coordination_rule(
    group_id: UUID,
    request: DebugCoordinationRuleRequest,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[DebugCoordinationRuleResponse]:
    # 规则动作与发给 GDB 的命令请求不是同一个 API。
    # 登记成功仅说明规则已保存，实际触发情况应查看事件。
    return ApiResponse.ok(await service.create_rule(group_id, request))


# 规则列表只含仍可参与后续匹配的定义，不是事件审计日志。
@router.get(
    "/debug/groups/{group_id}/rules",
    response_model=ApiResponse[list[DebugCoordinationRuleResponse]],
)
async def list_debug_coordination_rules(
    group_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[list[DebugCoordinationRuleResponse]]:
    return ApiResponse.ok(await service.list_rules(group_id))


# 删除使规则不再用于后续匹配，既往事件仍保留来源规则身份。
@router.delete(
    "/debug/groups/{group_id}/rules/{rule_id}",
    response_model=ApiResponse[bool],
)
async def delete_debug_coordination_rule(
    group_id: UUID,
    rule_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[bool]:
    await service.delete_rule(group_id, rule_id)
    # 成功布尔值对应规则删除操作。
    # 它不表示之前已经派发到其他会话的动作被撤回。
    return ApiResponse.ok(True)


# 返回最近发生的有限条事件；逐条结果可区分命令送达和暂停确认。
@router.get(
    "/debug/groups/{group_id}/events",
    response_model=ApiResponse[list[DebugCoordinationEventResponse]],
)
async def list_debug_coordination_events(
    group_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ApiResponse[list[DebugCoordinationEventResponse]]:
    # 上限作用于本次审计读取，不改变事件保留策略。
    # 调用方收到的短列表也不能当作从建组开始的完整历史。
    return ApiResponse.ok(await service.list_events(group_id, limit=limit))


# 组命令按成员报告结果；HTTP 成功不表示所有调试进程都成功执行。
@router.post(
    "/debug/groups/{group_id}/run",
    response_model=ApiResponse[DebugGroupCommandResponse],
)
async def run_debug_coordination_group(
    group_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[DebugGroupCommandResponse]:
    # 组启动结果应逐个分区查看。
    # 某个分区成功并不会替未就绪的其他成员记录成功。
    return ApiResponse.ok(await service.run(group_id))


# 组内每个成员独立检查是否处于可继续的暂停态，不强行同步所有目标。
@router.post(
    "/debug/groups/{group_id}/continue",
    response_model=ApiResponse[DebugGroupCommandResponse],
)
async def continue_debug_coordination_group(
    group_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[DebugGroupCommandResponse]:
    # 组继续并非一次原子的多进程状态迁移。
    # 当各会话状态不同或正在退出时，仍保留每个成员自己的结果。
    return ApiResponse.ok(await service.continue_execution(group_id))


# 组中断与断点联动事件的响应口径不同；逐成员查看命令回执和现场。
@router.post(
    "/debug/groups/{group_id}/interrupt",
    response_model=ApiResponse[DebugGroupCommandResponse],
)
async def interrupt_debug_coordination_group(
    group_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[DebugGroupCommandResponse]:
    return ApiResponse.ok(await service.interrupt(group_id))


# 关闭成员会话与关闭组关联是不同操作，调用方可分别控制生命周期。
@router.post(
    "/debug/groups/{group_id}/close-sessions",
    response_model=ApiResponse[DebugGroupCommandResponse],
)
async def close_debug_coordination_group_sessions(
    group_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[DebugGroupCommandResponse]:
    # 此路径针对会话生命周期，不等价于删除组。
    # 管理组关系的调用方可再按需要走组关闭接口。
    return ApiResponse.ok(await service.close_sessions(group_id))


# 关闭关联释放成员关系，但不会替调用方结束现有 GDB 会话。
@router.post(
    "/debug/groups/{group_id}/close",
    response_model=ApiResponse[DebugCoordinationGroupResponse],
)
async def close_debug_coordination_group(
    group_id: UUID,
    service: Annotated[
        DebugCoordinationService,
        Depends(get_debug_coordination_service),
    ],
) -> ApiResponse[DebugCoordinationGroupResponse]:
    return ApiResponse.ok(await service.close_group(group_id))

# 两个基础指标由显式提供的计数或耗时计算，并不替调用方采样。
@router.get("/metrics/build-success-rate", response_model=ApiResponse[float])
async def build_success_rate(
    success_count: int,
    total_count: int,
    metric_service: Annotated[AcceptanceMetricService, Depends(get_metric_service)],
) -> ApiResponse[float]:
    # 该指标接受显式统计分子和分母。
    # 端点不会按项目自动搜集成功任务或决定应纳入哪些样本。
    return ApiResponse.ok(metric_service.build_success_rate(success_count, total_count))


@router.get("/metrics/improvement-rate", response_model=ApiResponse[float])
async def improvement_rate(
    fifo_millis: int,
    optimized_millis: int,
    metric_service: Annotated[AcceptanceMetricService, Depends(get_metric_service)],
) -> ApiResponse[float]:
    # 耗时以毫秒为同一单位，基准是 FIFO。
    # 这个计算不启动调度对照，也不检查输入是否来自同一负载。
    return ApiResponse.ok(metric_service.improvement_rate(fifo_millis, optimized_millis))
