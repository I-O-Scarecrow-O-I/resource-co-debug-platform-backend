from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.modules.code_generation.schemas import NaturalCCRunRequest


class NaturalCCClientError(RuntimeError):
    """A NaturalCC request failed without exposing request secrets."""


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
        self._base_url = base_url.rstrip("/")
        self._connect_timeout_seconds = connect_timeout_seconds
        self._timeout = httpx.Timeout(
            timeout=request_timeout_seconds,
            connect=connect_timeout_seconds,
        )
        self._transport = transport

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/api/health")

    async def create_run(
        self,
        *,
        workspace: Path,
        request: NaturalCCRunRequest,
    ) -> dict[str, Any]:
        payload = request.model_dump()
        payload["workspace"] = str(workspace)
        payload["authorized_paths"] = []
        return await self._request(
            "POST",
            "/api/agent/runs",
            json=payload,
            create_request=True,
        )

    async def run(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/agent/runs/{_path_segment(run_id)}/run",
            timeout_seconds=timeout_seconds,
        )

    async def approve(self, run_id: str, risk: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/agent/runs/{_path_segment(run_id)}/approve",
            json={"risk": risk},
        )

    async def get_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/agent/runs/{_path_segment(run_id)}",
            timeout_seconds=timeout_seconds,
        )

    async def events(self, run_id: str, *, after: int = 0) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/agent/runs/{_path_segment(run_id)}/events",
            params={"after": after},
        )

    async def cancel(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
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
        timeout = self._timeout
        if timeout_seconds is not None:
            timeout = httpx.Timeout(
                timeout=timeout_seconds,
                connect=self._connect_timeout_seconds,
            )
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(method, path, json=json, params=params)
        except httpx.RequestError as exc:
            if create_request:
                raise NaturalCCCreateError(
                    outcome_unknown=not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                ) from None
            raise NaturalCCClientError("NaturalCC service request failed") from None

        if response.is_error:
            if create_request:
                raise NaturalCCCreateError(outcome_unknown=response.status_code >= 500)
            raise NaturalCCClientError("NaturalCC service request failed")

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
        return payload


def _path_segment(value: str) -> str:
    return quote(value, safe="")
