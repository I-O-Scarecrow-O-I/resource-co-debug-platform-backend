from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.platform.schemas.common import ApiResponse


class _RequestBodyTooLarge(Exception):
    pass


class ProjectUploadSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_project_upload(scope):
            await self.app(scope, receive, send)
            return
        if self._content_length(scope) > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        received_bytes = 0
        response_started = False
        rejected = False
        request_too_large = False

        async def reject_once() -> None:
            nonlocal rejected, response_started
            if rejected or response_started:
                return
            rejected = True
            response_started = True
            await self._reject(scope, receive, send)

        async def limited_receive() -> dict[str, object]:
            nonlocal received_bytes, request_too_large
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    request_too_large = True
                    raise _RequestBodyTooLarge
            return message

        async def limited_send(message: dict[str, object]) -> None:
            nonlocal response_started
            if request_too_large:
                await reject_once()
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _RequestBodyTooLarge:
            await reject_once()

    @staticmethod
    def _is_project_upload(scope: Scope) -> bool:
        return scope["type"] == "http" and (
            scope["method"] == "POST" and scope["path"] == "/api/v1/projects"
        )

    @staticmethod
    def _content_length(scope: Scope) -> int:
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return 0
        return 0

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content=jsonable_encoder(ApiResponse.failed("project upload exceeds size limit")),
        )
        await response(scope, receive, send)
