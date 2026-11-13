from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.core.config import get_settings
from app.modules.code_generation.client import NaturalCCClient
from app.modules.code_generation.service import NaturalCCService
from app.modules.code_generation.task_service import CodeGenerationTaskService
from app.modules.vulnerability.client import PipelineClient
from app.platform.api.deps import get_task_service
from app.platform.services.task_service import TaskService


# 门面客户端从应用地址配置连接上游；模型选择和密钥不属于此依赖。
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


# 以实际注入的服务与执行权限为缓存键，避免每个 HTTP 请求创建新的运行登记表。
@lru_cache
def _get_code_generation_task_service(
    task_service: TaskService,
    naturalcc_service: NaturalCCService,
    approve_execute: bool,
) -> CodeGenerationTaskService:
    # Agent 负责生成，自检使用独立的 Pipeline 客户端。
    # 它们可以共用 NaturalCC 应用地址，但请求协议和结果不同。
    return CodeGenerationTaskService(
        task_service=task_service,
        naturalcc_service=naturalcc_service,
        approve_execute=approve_execute,
        pipeline=PipelineClient(
            base_url=get_settings().naturalcc_base_url,
            connect_timeout_seconds=get_settings().naturalcc_connect_timeout_seconds,
        ),
    )


# 通过 FastAPI 依赖获取当前平台任务服务，再选用相同生命周期的模块实例。
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


# 这里只释放缓存引用；异步清理重试需要先由应用调用服务关闭。
def clear_code_generation_task_service_cache() -> None:
    _get_code_generation_task_service.cache_clear()
