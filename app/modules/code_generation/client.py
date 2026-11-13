from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.modules.code_generation.schemas import NaturalCCRunRequest


class NaturalCCClientError(RuntimeError):
    """A NaturalCC request failed without exposing request secrets."""


# 创建失败需要单独表示是否可能已经在上游建单，供平台决定后续恢复动作。
# 创建失败额外区分对端是否可能已经接收请求。
# 上层据此保留或释放共享工作区，不能只看异常类型是否属于网络故障。
class NaturalCCCreateError(NaturalCCClientError):
    def __init__(self, *, outcome_unknown: bool) -> None:
        super().__init__("NaturalCC service request failed")
        self.outcome_unknown = outcome_unknown


class NaturalCCClient:
    def __init__(
        self,
        *,
        base_url: str,
        connect_timeout_seconds: float,
        request_timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # 客户端地址指向 NaturalCC 应用，模型路由由该应用处理。
        # 连接预算与请求整体各阶段使用的超时可以分别配置。
        # 统一处理服务根地址和连接期限；每次请求可以收紧总预算而不改变共享设置。
        self._base_url = base_url.rstrip("/")
        self._connect_timeout_seconds = connect_timeout_seconds
        # 默认超时对象供每次 HTTP 操作复用。
        # 它不代替任务层贯穿建单、执行和轮询的总截止时间。
        self._timeout = httpx.Timeout(
            timeout=request_timeout_seconds,
            connect=connect_timeout_seconds,
        )
        self._transport = transport

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/api/health")

    # 运行请求使用平台提供的工作区；不会额外授权副本之外的路径。
    async def create_run(
        self,
        *,
        workspace: Path,
        request: NaturalCCRunRequest,
    ) -> dict[str, Any]:
        # 只把隔离工作区交给 Agent；此处不随请求授权任何工作区之外的路径。
        payload = request.model_dump()
        payload["workspace"] = str(workspace)
        payload["authorized_paths"] = []
        # 创建路径只负责登记请求，不在收到响应前发第二次创建。
        # 如果无法确定结果，恢复层也不能靠重复 POST 探测原请求。
        return await self._request(
            "POST",
            "/api/agent/runs",
            json=payload,
            create_request=True,
        )

    # 创建只取得运行身份，本入口才驱动该 ID 下的既有运行。
    async def run(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        # 执行和继续使用同一个运行入口，是否续接由上游运行状态决定。
        return await self._request(
            "POST",
            f"/api/agent/runs/{_path_segment(run_id)}/run",
            timeout_seconds=timeout_seconds,
        )

    # 审批可关联具体工具调用，旧响应缺少调用 ID 时仍支持按风险类别审批。
    async def approve(
        self,
        run_id: str,
        risk: str,
        tool_call_id: str | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        # 风险标识与调用 ID 一起提交，使已知审批能定位到具体待处理调用。
        payload = {"risk": risk}
        if tool_call_id is not None:
            payload["tool_call_id"] = tool_call_id
        return await self._request(
            "POST",
            f"/api/agent/runs/{_path_segment(run_id)}/approve",
            json=payload,
            timeout_seconds=timeout_seconds,
        )

    # 读取状态不触发再次运行，可用于判断超时后的上游进度。
    async def get_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        # 状态 GET 只读运行快照，可用于确认取消后的终态而不再次执行。
        return await self._request(
            "GET",
            f"/api/agent/runs/{_path_segment(run_id)}",
            timeout_seconds=timeout_seconds,
        )

    # 按序号获取新增事件，调用方负责记住上次确认的游标。
    async def events(
        self,
        run_id: str,
        *,
        after: int = 0,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        # 轮询只取给定游标之后的事件，不回放之前已经展示的日志。
        return await self._request(
            "GET",
            f"/api/agent/runs/{_path_segment(run_id)}/events",
            params={"after": after},
            timeout_seconds=timeout_seconds,
        )

    # 取消针对已知运行 ID；若请求失败，上层仍需根据运行状态判断是否生效。
    async def cancel(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        # 取消请求成功仅说明上游受理；释放目录前仍需通过 GET 观察终态。
        return await self._request(
            "POST",
            f"/api/agent/runs/{_path_segment(run_id)}/cancel",
            timeout_seconds=timeout_seconds,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        create_request: bool = False,
    ) -> dict[str, Any]:
        # 调用方可以在任务接近截止时缩短本次请求预算，同时维持独立连接设置。
        # 给定期限只作用于当前请求；连接期限保持客户端的单独配置。
        timeout = self._timeout
        if timeout_seconds is not None:
            timeout = httpx.Timeout(
                timeout=timeout_seconds,
                connect=self._connect_timeout_seconds,
            )
        # 本层每次请求独立管理 HTTP 客户端生命周期。
        # 保持远端操作身份靠路径中的 run_id，不依赖上一条连接继续存在。
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout,
                transport=self._transport,
            ) as client:
                # 创建结果不在发送成功时就确定。
                # 只有收到可用的对象响应后，上层才有机会保存远端运行标识。
                response = await client.request(method, path, json=json, params=params)
        # 建连失败表示请求未送达；其他请求错误不能据此断言未创建运行。
        # 错误不串联原始传输异常，避免把连接或请求细节带入平台状态。
        # 建立连接失败可判断创建未送达；其他传输失败可能在对端处理后发生。
        # 错误不附带原始 HTTP 请求或响应文本。
        except httpx.RequestError as exc:
            # 结果未知是保守结论，可能意味着副本已被远端使用。
            # 它不是对远端确实已创建任务的肯定判断。
            if create_request:
                raise NaturalCCCreateError(
                    outcome_unknown=not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                ) from None
            raise NaturalCCClientError("NaturalCC service request failed") from None

        # 创建接口的服务端错误也可能发生在写入之后；保留结果未知语义。
        # 创建遇到服务端错误时保留结果未知的语义。
        # 其他请求只公开统一错误，不转发上游错误正文。
        if response.is_error:
            if create_request:
                raise NaturalCCCreateError(outcome_unknown=response.status_code >= 500)
            raise NaturalCCClientError("NaturalCC service request failed")

        # 响应体只接受对象；创建阶段即使无法解码也不能假定没有上游副作用。
        # JSON 解析失败或返回形状不对时，没有可安全使用的运行标识。
        # 对创建调用仍不能据此认定没有远端运行。
        try:
            payload = response.json()
        except ValueError:
            if create_request:
                raise NaturalCCCreateError(outcome_unknown=True) from None
            raise NaturalCCClientError("NaturalCC service returned an invalid response") from None
        if not isinstance(payload, dict):
            if create_request:
                raise NaturalCCCreateError(outcome_unknown=True)
            raise NaturalCCClientError("NaturalCC service returned an invalid response")
        # 客户端只验证通用响应形状。
        # 执行状态和运行标识是否符合具体接口仍交给任务适配层判断。
        return payload


# 运行 ID 按单个路径段编码，不能改变调用的 API 路径结构。
def _path_segment(value: str) -> str:
    # 外部返回的运行 ID 只能占一个 URL 路径段，不能解释为路径分隔或查询参数。
    return quote(value, safe="")
