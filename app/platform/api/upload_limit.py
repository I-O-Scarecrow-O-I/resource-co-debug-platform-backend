from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.platform.schemas.common import ApiResponse


# 内部异常只用于提前退出接收，不作为客户端响应正文。
class _RequestBodyTooLarge(Exception):
    pass


# 只约束工程上传的原始请求体，包含 multipart 边界与字段开销。
# 归档自身的压缩及解压大小由工作区服务另行校验。
class ProjectUploadSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    # 仅在匹配的上传路径包裹 ASGI 收发。
    # 其他接口不会承担逐分块计数的开销。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 健康查询、项目查询和非 HTTP scope 均保持原 ASGI 调用方式。
        if not self._is_project_upload(scope):
            await self.app(scope, receive, send)
            return
        # 声明长度已超限时可以立即拒绝；缺省或无效长度仍由流式计数覆盖。
        if self._content_length(scope) > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        # 计数和响应标记属于当前请求闭包，不会在并行上传间共享。
        received_bytes = 0
        # 这些标记分别记录是否已发响应头、是否已经拒绝、以及接收是否触发超限。
        # 接收路径和发送路径会读取同一组请求局部状态。
        response_started = False
        rejected = False
        request_too_large = False

        # 一旦响应头发出就不能改写状态；拒绝路径必须与下游发送协调。
        async def reject_once() -> None:
            nonlocal rejected, response_started
            # 发送 413 前先确认响应未开始，避免同一个请求出现两份状态头。
            if rejected or response_started:
                return
            rejected = True
            response_started = True
            await self._reject(scope, receive, send)

        # 计数实际到达的请求分块，在越过边界的分块进入 multipart 解析前终止。
        async def limited_receive() -> dict[str, object]:
            nonlocal received_bytes, request_too_large
            # 仅从已经到达的 ASGI 消息计数，未信任客户端对分块边界或总长度的声明。
            message = await receive()
            # 只对实际请求数据消息计数，断开等 ASGI 控制消息原样交付。
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                # 恰好等于上限仍可继续，首个超限块会在解析前抛出内部信号。
                if received_bytes > self.max_body_bytes:
                    # 先记录超限事实再抛内部异常，以便下游捕获异常并尝试发响应时仍能被替换。
                    request_too_large = True
                    raise _RequestBodyTooLarge
            return message

        # 下游可能将接收异常转成自己的响应；超限标记让中间件接管该响应。
        async def limited_send(message: dict[str, object]) -> None:
            nonlocal response_started
            # 应用即使捕获了接收异常，也不能把自己的响应混入超限回执。
            if request_too_large:
                await reject_once()
                return
            # 仅在开始消息上记录响应状态；后续分块按原样交付，除非已进入超限分支。
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        # 未被下游处理的超限信号在这里收口，与下游尝试发送时共用一次性拒绝逻辑。
        try:
            await self.app(scope, limited_receive, limited_send)
        except _RequestBodyTooLarge:
            await reject_once()

    @staticmethod
    # 限定路由和 HTTP 方法，日志 WebSocket 与其他请求保持原有接收方式。
    def _is_project_upload(scope: Scope) -> bool:
        # 这一约束绑定到注册的上传入口；路由前缀若迁移需同步检查此匹配规则。
        return scope["type"] == "http" and (
            scope["method"] == "POST" and scope["path"] == "/api/v1/projects"
        )

    @staticmethod
    # 长度头只用作快速拒绝依据，不依赖它作为实际接收字节数。
    def _content_length(scope: Scope) -> int:
        # ASGI 请求头为字节序列，按不区分大小写匹配长度名。
        # 无可用值时回退至实际流量限制。
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    return int(value)
                # 无效声明不能证明体积安全，回退后仍检查实际流。
                except ValueError:
                    return 0
        return 0

    @staticmethod
    # 返回与业务接口一致的错误信封，客户端可统一展示上传失败。
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content=jsonable_encoder(ApiResponse.failed("project upload exceeds size limit")),
        )
        await response(scope, receive, send)
