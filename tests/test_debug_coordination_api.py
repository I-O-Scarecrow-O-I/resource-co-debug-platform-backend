from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.modules.co_debug.coordination.models import (
    DebugCoordinationEventOutcome,
    DebugCoordinationGroupStatus,
    DebugCoordinationRuleAction,
    DebugGroupCommand,
    DebugGroupCommandOutcome,
)
from app.modules.co_debug.routes import router
from app.modules.co_debug.schemas.coordination import (
    DebugCoordinationEventResponse,
    DebugCoordinationGroupResponse,
    DebugCoordinationRuleResponse,
    DebugGroupCommandResponse,
    DebugGroupMemberCommandResult,
    DebugLogicalPartitionResponse,
)
from app.platform.api.deps import get_debug_coordination_service


class FakeCoordinationService:
    def __init__(self) -> None:
        self.group_id = uuid4()
        self.project_id = uuid4()
        self.partition_id = uuid4()
        self.session_id = uuid4()
        self.rule_id = uuid4()
        self.calls: list[tuple[str, object]] = []

    def group(self) -> DebugCoordinationGroupResponse:
        return DebugCoordinationGroupResponse(
            group_id=self.group_id,
            project_id=self.project_id,
            name="logical-debug",
            status=DebugCoordinationGroupStatus.ACTIVE,
            created_at=datetime.now(tz=UTC),
            closed_at=None,
            active_member_count=2,
            members=[
                DebugLogicalPartitionResponse(
                    partition_id=self.partition_id,
                    partition_name="producer",
                    session_task_id=self.session_id,
                    associated=True,
                    joined_at=datetime.now(tz=UTC),
                    removed_at=None,
                )
            ],
        )

    async def create(self, request):
        self.calls.append(("create", request))
        return self.group()

    async def list(self, project_id=None):
        self.calls.append(("list", project_id))
        return [self.group()]

    async def get(self, group_id: UUID):
        self.calls.append(("get", group_id))
        return self.group()

    async def remove_member(self, group_id: UUID, partition_id: UUID):
        self.calls.append(("remove", (group_id, partition_id)))
        return self.group()

    async def close_group(self, group_id: UUID):
        self.calls.append(("close_group", group_id))
        return self.group()

    async def create_rule(self, group_id: UUID, request):
        self.calls.append(("create_rule", (group_id, request)))
        return DebugCoordinationRuleResponse(
            rule_id=self.rule_id,
            group_id=self.group_id,
            source_partition_id=self.partition_id,
            source_breakpoint_number="1",
            target_partition_id=uuid4(),
            action=DebugCoordinationRuleAction.INTERRUPT,
            created_at=datetime.now(tz=UTC),
        )

    async def list_rules(self, group_id: UUID):
        self.calls.append(("list_rules", group_id))
        return []

    async def delete_rule(self, group_id: UUID, rule_id: UUID):
        self.calls.append(("delete_rule", (group_id, rule_id)))

    async def list_events(self, group_id: UUID, *, limit: int = 100):
        self.calls.append(("list_events", (group_id, limit)))
        return [
            DebugCoordinationEventResponse(
                event_id=uuid4(),
                source_event_id=uuid4(),
                group_id=self.group_id,
                rule_id=self.rule_id,
                source_partition_id=self.partition_id,
                source_session_task_id=self.session_id,
                target_partition_id=uuid4(),
                target_session_task_id=uuid4(),
                breakpoint_number="1",
                occurred_at=datetime.now(tz=UTC),
                processed_at=datetime.now(tz=UTC),
                outcome=DebugCoordinationEventOutcome.INTERRUPT_CONFIRMED,
                command_sent=True,
                target_paused=True,
                message="target stopped",
            )
        ]

    async def run(self, group_id: UUID):
        return self.command("run", DebugGroupCommand.RUN, group_id)

    async def continue_execution(self, group_id: UUID):
        return self.command("continue", DebugGroupCommand.CONTINUE, group_id)

    async def interrupt(self, group_id: UUID):
        return self.command("interrupt", DebugGroupCommand.INTERRUPT, group_id)

    async def close_sessions(self, group_id: UUID):
        return self.command("close_sessions", DebugGroupCommand.CLOSE_SESSIONS, group_id)

    def command(
        self,
        call: str,
        command: DebugGroupCommand,
        group_id: UUID,
    ) -> DebugGroupCommandResponse:
        self.calls.append((call, group_id))
        return DebugGroupCommandResponse(
            group_id=self.group_id,
            command=command,
            all_succeeded=True,
            results=[
                DebugGroupMemberCommandResult(
                    partition_id=self.partition_id,
                    partition_name="producer",
                    session_task_id=self.session_id,
                    outcome=DebugGroupCommandOutcome.SUCCEEDED,
                )
            ],
        )


def _app(service: FakeCoordinationService) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/modules/co-debug")

    async def override_service():
        return service

    app.dependency_overrides[get_debug_coordination_service] = override_service
    return app


@pytest.mark.asyncio
async def test_debug_coordination_group_api_routes() -> None:
    service = FakeCoordinationService()
    request = {
        "project_id": str(service.project_id),
        "name": "logical-debug",
        "members": [
            {"partition_name": "producer", "session_task_id": str(uuid4())},
            {"partition_name": "consumer", "session_task_id": str(uuid4())},
        ],
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/api/v1/modules/co-debug/debug/groups", json=request)
        assert response.status_code == 200
        response = await client.get(
            "/api/v1/modules/co-debug/debug/groups",
            params={"project_id": str(service.project_id)},
        )
        assert response.status_code == 200
        base = f"/api/v1/modules/co-debug/debug/groups/{service.group_id}"
        assert (await client.get(base)).status_code == 200
        assert (await client.get(f"{base}/state")).status_code == 200
        rule_response = await client.post(
            f"{base}/rules",
            json={
                "source_partition_id": str(service.partition_id),
                "source_breakpoint_number": "1",
                "target_partition_id": str(uuid4()),
            },
        )
        assert rule_response.status_code == 200
        assert (await client.get(f"{base}/rules")).status_code == 200
        event_response = await client.get(f"{base}/events", params={"limit": 25})
        assert event_response.status_code == 200
        assert event_response.json()["data"][0]["target_paused"] is True
        assert (await client.delete(f"{base}/rules/{service.rule_id}")).status_code == 200
        for command in ("run", "continue", "interrupt", "close-sessions"):
            response = await client.post(f"{base}/{command}")
            assert response.status_code == 200
            assert response.json()["data"]["all_succeeded"] is True
        assert (await client.delete(f"{base}/members/{service.partition_id}")).status_code == 200
        assert (await client.post(f"{base}/close")).status_code == 200

    called = [name for name, _ in service.calls]
    assert called == [
        "create",
        "list",
        "get",
        "get",
        "create_rule",
        "list_rules",
        "list_events",
        "delete_rule",
        "run",
        "continue",
        "interrupt",
        "close_sessions",
        "remove",
        "close_group",
    ]
