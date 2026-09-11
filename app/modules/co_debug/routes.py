from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.modules.co_debug.schemas.debug import DebugSessionResponse
from app.modules.co_debug.schemas.dependencies import DependencyAnalysisResponse,DependencyRepairBuildRequest,DependencyRepairResponse
from app.modules.co_debug.services.debug_service import DebugSessionService
from app.modules.co_debug.services.dependency_service import DependencyAnalysisService
from app.modules.co_debug.services.metric_service import AcceptanceMetricService
from app.modules.co_debug.services.repair_build_service import DependencyRepairBuildService
from app.modules.co_debug.schemas.debug import DebugArgumentsRequest,DebugBreakpointRequest, DebugBreakpointResponse,DebugExpressionRequest,DebugExpressionResponse,DebugSessionResponse,DebugSessionStateResponse, DebugStackFramesResponse,DebugWaitForStopRequest
from app.platform.api.deps import get_debug_service, get_dependency_service, get_dependency_repair_build_service,get_metric_service
from app.platform.schemas.common import ApiResponse
from app.platform.schemas.tasks import DebugTaskRequest,TaskResponse

router = APIRouter()


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
