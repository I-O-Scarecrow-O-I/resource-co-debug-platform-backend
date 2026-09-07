import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import app
from app.modules.code_generation.client import (
    NaturalCCClient,
    NaturalCCClientError,
    NaturalCCCreateError,
)
from app.modules.code_generation.deps import get_code_generation_service
from app.modules.code_generation.schemas import NaturalCCRunRequest
from app.modules.code_generation.service import NaturalCCService


def test_naturalcc_settings_are_server_controlled_with_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NATURALCC_BASE_URL", raising=False)
    monkeypatch.delenv("NATURALCC_CONNECT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("NATURALCC_REQUEST_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("NATURALCC_APPROVE_EXECUTE", raising=False)

    settings = Settings(_env_file=None)

    assert settings.naturalcc_base_url == "http://127.0.0.1:7860"
    assert settings.naturalcc_connect_timeout_seconds == 5
    assert settings.naturalcc_request_timeout_seconds == 30
    assert settings.naturalcc_approve_execute is False


@pytest.mark.parametrize("base_url", ["", "ftp://127.0.0.1:7860", "127.0.0.1:7860"])
def test_naturalcc_settings_require_http_url(base_url: str) -> None:
    with pytest.raises(ValidationError, match="naturalcc_base_url"):
        Settings(_env_file=None, naturalcc_base_url=base_url)

    assert (
        Settings(_env_file=None, naturalcc_base_url="https://10.0.0.2:7860/").naturalcc_base_url
        == "https://10.0.0.2:7860"
    )


@pytest.mark.asyncio
async def test_naturalcc_client_uses_expected_run_api_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    client = NaturalCCClient(
        base_url="http://naturalcc.test/",
        connect_timeout_seconds=2,
        request_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )
    request = NaturalCCRunRequest(
        goal="Repair the failing test",
        target_files=["src/example.py"],
        budget={"max_tool_calls": 3},
        thread_id="thread-1",
        capabilities={"codegraph": False},
    )

    await client.health()
    await client.create_run(
        workspace=Path("O:/controlled/workspace"),
        request=request,
    )
    await client.approve("run-1", "write")
    await client.run("run-1", timeout_seconds=60)
    await client.get_run("run-1", timeout_seconds=4)
    await client.events("run-1", after=7)
    await client.cancel("run-1", timeout_seconds=3)

    assert [(item.method, item.url.path) for item in requests] == [
        ("GET", "/api/health"),
        ("POST", "/api/agent/runs"),
        ("POST", "/api/agent/runs/run-1/approve"),
        ("POST", "/api/agent/runs/run-1/run"),
        ("GET", "/api/agent/runs/run-1"),
        ("GET", "/api/agent/runs/run-1/events"),
        ("POST", "/api/agent/runs/run-1/cancel"),
    ]
    assert json.loads(requests[2].content) == {"risk": "write"}
    assert requests[3].extensions["timeout"] == {
        "connect": 2,
        "read": 60,
        "write": 60,
        "pool": 60,
    }
    assert requests[5].url.params == httpx.QueryParams({"after": "7"})
    assert json.loads(requests[1].content) == {
        "workspace": "O:\\controlled\\workspace",
        "goal": "Repair the failing test",
        "target_files": ["src/example.py"],
        "authorized_paths": [],
        "budget": {"max_tool_calls": 3},
        "thread_id": "thread-1",
        "capabilities": {"codegraph": False},
    }


@pytest.mark.asyncio
async def test_naturalcc_client_error_does_not_leak_remote_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed with a remote detail", request=request)

    client = NaturalCCClient(
        base_url="http://naturalcc.test",
        connect_timeout_seconds=2,
        request_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(NaturalCCClientError) as exc_info:
        await client.create_run(
            workspace=Path("O:/controlled/workspace"),
            request=NaturalCCRunRequest(goal="Do not leak remote details"),
        )

    assert str(exc_info.value) == "NaturalCC service request failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "outcome_unknown"),
    [
        (httpx.Response(400, json={}), False),
        (httpx.Response(503, json={}), True),
        (httpx.Response(200, content=b"not-json"), True),
    ],
)
async def test_create_run_classifies_cleanup_safety(
    response: httpx.Response,
    outcome_unknown: bool,
) -> None:
    client = NaturalCCClient(
        base_url="http://naturalcc.test",
        connect_timeout_seconds=2,
        request_timeout_seconds=10,
        transport=httpx.MockTransport(lambda _: response),
    )

    with pytest.raises(NaturalCCCreateError) as exc_info:
        await client.create_run(
            workspace=Path("O:/controlled/workspace"),
            request=NaturalCCRunRequest(goal="Create run"),
        )

    assert str(exc_info.value) == "NaturalCC service request failed"
    assert exc_info.value.outcome_unknown is outcome_unknown


@pytest.mark.asyncio
async def test_create_run_connection_failure_is_known_not_created() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = NaturalCCClient(
        base_url="http://naturalcc.test",
        connect_timeout_seconds=2,
        request_timeout_seconds=10,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(NaturalCCCreateError) as exc_info:
        await client.create_run(
            workspace=Path("O:/controlled/workspace"),
            request=NaturalCCRunRequest(goal="Create run"),
        )

    assert exc_info.value.outcome_unknown is False


def test_code_generation_routes_describe_capabilities_and_unavailable_health() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service unavailable", request=request)

    unavailable_service = NaturalCCService(
        NaturalCCClient(
            base_url="http://naturalcc.test",
            connect_timeout_seconds=2,
            request_timeout_seconds=10,
            transport=httpx.MockTransport(handler),
        )
    )
    app.dependency_overrides[get_code_generation_service] = lambda: unavailable_service
    try:
        client = TestClient(app)
        health_response = client.get("/api/v1/modules/code-generation/health")
        capabilities_response = client.get("/api/v1/modules/code-generation/capabilities")
    finally:
        app.dependency_overrides.clear()

    assert health_response.status_code == 200
    assert health_response.json()["data"] == {
        "status": "unavailable",
        "detail": "NaturalCC service is unavailable",
    }
    assert capabilities_response.status_code == 200
    assert capabilities_response.json()["data"] == {
        "provider": "NaturalCC code_agent",
        "operations": ["completion", "repair", "refactor"],
        "execution_route_available": True,
        "detail": "Execution is available through the unified platform task lifecycle.",
    }
