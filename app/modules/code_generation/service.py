from pathlib import Path
from typing import Any

from app.modules.code_generation.client import NaturalCCClient, NaturalCCClientError
from app.modules.code_generation.schemas import (
    CodeGenerationCapabilities,
    NaturalCCHealthStatus,
    NaturalCCRunRequest,
)


class NaturalCCService:
    # 门面仅持有 NaturalCC HTTP 客户端，不根据部署模型另建一套推理配置。
    def __init__(self, client: NaturalCCClient) -> None:
        self._client = client

    # 把连接失败和可达但不健康映射为不同提示；都不暴露原始上游内容。
    # 健康只反映上游应用可达且返回约定状态，不验证模型推理或工具权限。
    async def health(self) -> NaturalCCHealthStatus:
        # 健康接口把已知客户端错误收敛为服务不可用；不把上游原文作为状态详情返回。
        try:
            response = await self._client.health()
        except NaturalCCClientError:
            return NaturalCCHealthStatus(
                status="unavailable",
                detail="NaturalCC service is unavailable",
            )
        # 能连接 HTTP 服务与上游应用报告正常是不同判断，两者都满足才返回可用。
        if response.get("status") != "ok":
            return NaturalCCHealthStatus(
                status="unavailable",
                detail="NaturalCC service reported an unhealthy status",
            )
        return NaturalCCHealthStatus(status="available", detail="NaturalCC service is reachable")

    # 能力是平台接入声明，表示可以提交任务，不证明当前上游或模型健康。
    # 能力声明描述平台可提交的任务类型，与健康状态分开查询。
    # 能力列表描述平台接入的操作，不证明当前模型或上游服务已能运行任务。
    def capabilities(self) -> CodeGenerationCapabilities:
        return CodeGenerationCapabilities(
            provider="NaturalCC code_agent",
            operations=["completion", "repair", "refactor"],
            execution_route_available=True,
            detail="Execution is available through the unified platform task lifecycle.",
        )

    # 服务层保留上游运行协议；状态、超时恢复与审批由任务编排层决定。
    # 创建运行只交由客户端处理传输；目录的生命周期仍由托管任务服务负责。
    # 新建运行只接受调用方分配的工作区；任务隔离和目录回收由平台执行层处理。
    async def create_run(
        self,
        *,
        workspace: Path,
        request: NaturalCCRunRequest,
    ) -> dict[str, Any]:
        return await self._client.create_run(workspace=workspace, request=request)

    # 启动已有运行使用上游 ID，不把 HTTP 调用成功等同于任务已终结。
    async def run(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._client.run(run_id, timeout_seconds=timeout_seconds)

    # 批准是独立请求，调用者应先读状态并核对具体等待对象。
    # 审批保留风险类型和可选工具调用 ID，决定是否发送由任务编排层负责。
    async def approve(
        self,
        run_id: str,
        risk: str,
        tool_call_id: str | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._client.approve(
            run_id,
            risk,
            tool_call_id,
            timeout_seconds=timeout_seconds,
        )

    # 状态读取只查询已有运行，不创建或隐式重新启动它。
    async def get_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._client.get_run(run_id, timeout_seconds=timeout_seconds)

    # 事件查询由任务层持有游标；此门面不保存跨运行的进度。
    # 事件读取接受游标；编排层可增量取得日志和审批证据而不必重放全部历史。
    async def events(
        self,
        run_id: str,
        *,
        after: int = 0,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._client.events(
            run_id,
            after=after,
            timeout_seconds=timeout_seconds,
        )

    # 取消只向上游发送意图；是否进入终态由任务恢复或取消逻辑查询。
    # 取消请求交给上游处理；资源是否已经释放仍需按生命周期状态确认。
    async def cancel(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._client.cancel(run_id, timeout_seconds=timeout_seconds)
