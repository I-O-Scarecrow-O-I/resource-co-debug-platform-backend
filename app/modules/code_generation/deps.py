from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.core.config import get_settings
from app.modules.code_generation.client import NaturalCCClient
from app.modules.code_generation.service import NaturalCCService
from app.modules.code_generation.task_service import CodeGenerationTaskService
from app.platform.api.deps import get_task_service
from app.platform.services.task_service import TaskService


@lru_cache
def get_code_generation_service() -> NaturalCCService:
    settings = get_settings()
    return NaturalCCService(
        NaturalCCClient(
            base_url=settings.naturalcc_base_url,
            connect_timeout_seconds=settings.naturalcc_connect_timeout_seconds,
            request_timeout_seconds=settings.naturalcc_request_timeout_seconds,
        )
    )


@lru_cache
def _get_code_generation_task_service(
    task_service: TaskService,
    naturalcc_service: NaturalCCService,
    approve_execute: bool,
) -> CodeGenerationTaskService:
    return CodeGenerationTaskService(
        task_service=task_service,
        naturalcc_service=naturalcc_service,
        approve_execute=approve_execute,
    )


def get_code_generation_task_service(
    task_service: Annotated[TaskService, Depends(get_task_service)],
    naturalcc_service: Annotated[
        NaturalCCService,
        Depends(get_code_generation_service),
    ],
) -> CodeGenerationTaskService:
    return _get_code_generation_task_service(
        task_service,
        naturalcc_service,
        get_settings().naturalcc_approve_execute,
    )


def clear_code_generation_task_service_cache() -> None:
    _get_code_generation_task_service.cache_clear()
