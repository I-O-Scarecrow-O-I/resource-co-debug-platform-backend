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
    DebugSessionResponse,
    DebugSessionStateResponse,
    DebugStackFramesResponse,
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
    return ApiResponse.ok(service.summarize(request.comparison_task_ids))


@router.post("/debug/comparisons", response_model=ApiResponse[TaskResponse])
async def create_debug_comparison(
    request: DebugComparisonRequest,
    service: Annotated[CoDebugTaskService, Depends(get_co_debug_task_service)],
) -> ApiResponse[TaskResponse]:
    task = await service.create_debug_schedule_comparison(request)
    return ApiResponse.ok(TaskResponse.from_record(task))


@router.post("/dependencies/analyze", response_model=ApiResponse[DependencyAnalysisResponse])
async def analyze_dependencies(
    project_id: UUID,
    dependency_service: Annotated[DependencyAnalysisService, Depends(get_dependency_service)],
) -> ApiResponse[DependencyAnalysisResponse]:
    return ApiResponse.ok(dependency_service.analyze(project_id))

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

    task = await service.create_task(
        request
    )

    return ApiResponse.ok(
        TaskResponse.from_record(task)
    )

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
    return ApiResponse.ok(
        dependency_service.repair(project_id)
    )


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
    return ApiResponse.ok(
        interactive_debug_service.list_candidates(
            project_id=project_id
        )
    )

@router.get("/debug/sessions/{task_id}", response_model=ApiResponse[DebugSessionResponse])
async def describe_debug_session(
    task_id: UUID,
    debug_service: Annotated[DebugSessionService, Depends(get_debug_service)],
) -> ApiResponse[DebugSessionResponse]:
    return ApiResponse.ok(debug_service.describe(task_id))

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
    task = await debug_service.create(
        request
    )

    return ApiResponse.ok(
        TaskResponse.from_record(
            task
        )
    )


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
    return ApiResponse.ok(
        await debug_service.state(
            task_id
        )
    )


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
    await debug_service.set_arguments(
        task_id,
        request.arguments,
    )

    return ApiResponse.ok(True)


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

    return ApiResponse.ok(
        DebugBreakpointResponse(
            **result
        )
    )


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
    await (
        debug_service
        .delete_breakpoint(
            task_id,
            breakpoint_number,
        )
    )

    return ApiResponse.ok(True)


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
    return ApiResponse.ok(
        await (
            debug_service
            .continue_execution(
                task_id
            )
        )
    )


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
    return ApiResponse.ok(
        await debug_service.wait_for_stop(
            task_id,
            timeout_seconds=(
                request.timeout_seconds
            ),
        )
    )


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
    result = (
        await debug_service
        .stack_frames(task_id)
    )

    return ApiResponse.ok(
        DebugStackFramesResponse(
            **result
        )
    )


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
    return ApiResponse.ok(
        await debug_service.close(
            task_id
        )
    )


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
    return ApiResponse.ok(await service.create(request))


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
    return ApiResponse.ok(await service.get(group_id))


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
    return ApiResponse.ok(await service.remove_member(group_id, partition_id))


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
    return ApiResponse.ok(await service.get(group_id))


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
    return ApiResponse.ok(await service.create_rule(group_id, request))


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
    return ApiResponse.ok(True)


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
    return ApiResponse.ok(await service.list_events(group_id, limit=limit))


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
    return ApiResponse.ok(await service.run(group_id))


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
    return ApiResponse.ok(await service.continue_execution(group_id))


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
    return ApiResponse.ok(await service.close_sessions(group_id))


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

@router.get("/metrics/build-success-rate", response_model=ApiResponse[float])
async def build_success_rate(
    success_count: int,
    total_count: int,
    metric_service: Annotated[AcceptanceMetricService, Depends(get_metric_service)],
) -> ApiResponse[float]:
    return ApiResponse.ok(metric_service.build_success_rate(success_count, total_count))


@router.get("/metrics/improvement-rate", response_model=ApiResponse[float])
async def improvement_rate(
    fifo_millis: int,
    optimized_millis: int,
    metric_service: Annotated[AcceptanceMetricService, Depends(get_metric_service)],
) -> ApiResponse[float]:
    return ApiResponse.ok(metric_service.improvement_rate(fifo_millis, optimized_millis))
