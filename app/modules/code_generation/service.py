from pathlib import Path
from typing import Any

from app.modules.code_generation.client import NaturalCCClient, NaturalCCClientError
from app.modules.code_generation.schemas import (
    CodeGenerationCapabilities,
    NaturalCCHealthStatus,
    NaturalCCRunRequest,
)


class NaturalCCService:
    def __init__(self, client: NaturalCCClient) -> None:
        self._client = client

    async def health(self) -> NaturalCCHealthStatus:
        try:
            response = await self._client.health()
        except NaturalCCClientError:
            return NaturalCCHealthStatus(
                status="unavailable",
                detail="NaturalCC service is unavailable",
            )
        if response.get("status") != "ok":
            return NaturalCCHealthStatus(
                status="unavailable",
                detail="NaturalCC service reported an unhealthy status",
            )
        return NaturalCCHealthStatus(status="available", detail="NaturalCC service is reachable")

    def capabilities(self) -> CodeGenerationCapabilities:
        return CodeGenerationCapabilities(
            provider="NaturalCC code_agent",
            operations=["completion", "repair", "refactor"],
            execution_route_available=True,
            detail="Execution is available through the unified platform task lifecycle.",
        )

    async def create_run(
        self,
        *,
        workspace: Path,
        request: NaturalCCRunRequest,
    ) -> dict[str, Any]:
        return await self._client.create_run(workspace=workspace, request=request)

    async def run(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._client.run(run_id, timeout_seconds=timeout_seconds)

    async def approve(self, run_id: str, risk: str) -> dict[str, Any]:
        return await self._client.approve(run_id, risk)

    async def get_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._client.get_run(run_id, timeout_seconds=timeout_seconds)

    async def events(self, run_id: str, *, after: int = 0) -> dict[str, Any]:
        return await self._client.events(run_id, after=after)

    async def cancel(
        self,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._client.cancel(run_id, timeout_seconds=timeout_seconds)
