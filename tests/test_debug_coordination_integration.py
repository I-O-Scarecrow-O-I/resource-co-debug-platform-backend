import asyncio
import io
import shutil
import subprocess
from zipfile import ZipFile

import pytest
from fastapi import UploadFile

from app.modules.co_debug.coordination.models import DebugCoordinationEventOutcome
from app.modules.co_debug.debug.events import DebugSessionEventBus
from app.modules.co_debug.debug.manager import DebugSessionManager
from app.modules.co_debug.schemas.coordination import (
    DebugCoordinationGroupRequest,
    DebugCoordinationRuleRequest,
    DebugLogicalPartitionRequest,
)
from app.modules.co_debug.services.debug_coordination_service import (
    DebugCoordinationService,
)
from app.modules.co_debug.services.debug_coordination_store import DebugCoordinationStore
from app.modules.co_debug.services.debug_service import DebugSessionService
from app.modules.co_debug.services.interactive_debug_service import (
    InteractiveDebugService,
)
from app.platform.schemas.tasks import DebugTaskRequest
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService

pytestmark = pytest.mark.skipif(
    shutil.which("gcc") is None or shutil.which("gdb") is None,
    reason="gcc and gdb are required",
)


async def _create_project(workspace_service: WorkspaceService):
    source = (
        "#include <string.h>\n"
        "#include <unistd.h>\n"
        "volatile int coordination_marker = 0;\n"
        "void source_breakpoint(void) { coordination_marker++; }\n"
        "int main(int argc, char **argv) {\n"
        '    if (argc > 1 && strcmp(argv[1], "source") == 0) {\n'
        "        usleep(200000);\n"
        "        source_breakpoint();\n"
        "        return 0;\n"
        "    }\n"
        "    while (coordination_marker == 0) { }\n"
        "    return 0;\n"
        "}\n"
    )
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("main.c", source)
    archive.seek(0)
    project = await workspace_service.create_from_archive(
        UploadFile(file=archive, filename="coordination-project.zip")
    )
    subprocess.run(
        ["gcc", "-g", "-O0", "-o", "app", "main.c"],
        cwd=project.source_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return project


async def _wait_for_sessions(
    manager: DebugSessionManager,
    task_ids: set,
) -> None:
    async with asyncio.timeout(8):
        while not task_ids <= set(manager.list_session_ids()):
            await asyncio.sleep(0.01)


async def _wait_for_coordination_event(
    service: DebugCoordinationService,
    group_id,
):
    async with asyncio.timeout(8):
        while True:
            events = await service.list_events(group_id)
            if events and events[0].outcome != DebugCoordinationEventOutcome.PROCESSING:
                return events[0]
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_real_breakpoint_hit_interrupts_other_logical_partition(tmp_path) -> None:
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    task_store = TaskStore(tmp_path / "tasks.sqlite3")
    task_service = TaskService(
        workspace_service=workspace_service,
        task_store=task_store,
        log_service=TaskLogService(500, tmp_path / "logs.sqlite3"),
        process_runner=ProcessRunner(),
        default_timeout_seconds=10,
    )
    manager = DebugSessionManager()
    event_bus = DebugSessionEventBus()
    interactive_service = InteractiveDebugService(
        task_service=task_service,
        session_manager=manager,
        event_publisher=event_bus,
    )
    debug_service = DebugSessionService(
        task_store=task_store,
        session_manager=manager,
        interactive_service=interactive_service,
    )
    coordination_store = DebugCoordinationStore(tmp_path / "tasks.sqlite3")
    coordination_service = DebugCoordinationService(
        store=coordination_store,
        task_store=task_store,
        session_manager=manager,
        debug_service=debug_service,
        event_bus=event_bus,
    )
    published_events = []
    probe_subscription = event_bus.subscribe(
        asyncio.get_running_loop(),
        published_events.append,
    )

    try:
        project = await _create_project(workspace_service)
        source_task, target_task = await asyncio.gather(
            interactive_service.create_task(
                DebugTaskRequest(
                    project_id=project.id,
                    executable_path="app",
                    timeout_seconds=10,
                )
            ),
            interactive_service.create_task(
                DebugTaskRequest(
                    project_id=project.id,
                    executable_path="app",
                    timeout_seconds=10,
                )
            ),
        )
        await _wait_for_sessions(manager, {source_task.id, target_task.id})
        await asyncio.gather(
            manager.set_arguments(source_task.id, ["source"]),
            manager.set_arguments(target_task.id, ["target"]),
        )
        breakpoint = await manager.insert_breakpoint(
            source_task.id,
            "source_breakpoint",
        )
        group = await coordination_service.create(
            DebugCoordinationGroupRequest(
                project_id=project.id,
                name="producer-consumer",
                members=[
                    DebugLogicalPartitionRequest(
                        partition_name="source",
                        session_task_id=source_task.id,
                    ),
                    DebugLogicalPartitionRequest(
                        partition_name="target",
                        session_task_id=target_task.id,
                    ),
                ],
            )
        )
        source_partition = next(
            item for item in group.members if item.session_task_id == source_task.id
        )
        target_partition = next(
            item for item in group.members if item.session_task_id == target_task.id
        )
        await coordination_service.create_rule(
            group.group_id,
            DebugCoordinationRuleRequest(
                source_partition_id=source_partition.partition_id,
                source_breakpoint_number=breakpoint["number"],
                target_partition_id=target_partition.partition_id,
            ),
        )

        target_running = await manager.run(target_task.id)
        assert target_running["state"] == "RUNNING"
        await manager.run(source_task.id)
        source_stopped = await manager.wait_for_stop(
            source_task.id,
            stop_timeout_seconds=4,
            request_timeout_seconds=5,
        )
        assert source_stopped["state"] == "STOPPED"
        assert source_stopped["stop_reason"] == "breakpoint-hit"
        async with asyncio.timeout(1):
            while not published_events:
                await asyncio.sleep(0.01)
        assert published_events[0].breakpoint_number == breakpoint["number"]
        assert coordination_store.matching_rules(
            source_task.id,
            breakpoint["number"],
        )

        event = await _wait_for_coordination_event(
            coordination_service,
            group.group_id,
        )
        target_state = await manager.state(target_task.id)

        assert event.outcome == DebugCoordinationEventOutcome.INTERRUPT_CONFIRMED, (
            event.message,
            target_state,
        )
        assert event.command_sent is True
        assert event.target_paused is True
        assert target_state["state"] == "STOPPED"

        await asyncio.gather(
            manager.close_session(source_task.id),
            manager.close_session(target_task.id),
        )
    finally:
        event_bus.unsubscribe(probe_subscription)
        coordination_service.close()
        coordination_store.close()
        await task_service.shutdown(grace_seconds=1)
        task_service.close_resources_when_idle()
