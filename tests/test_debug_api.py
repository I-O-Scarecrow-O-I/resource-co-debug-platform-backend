import asyncio
import io
import shutil
import subprocess
import time
from zipfile import ZipFile

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.co_debug.debug.manager import (
    DebugSessionManager,
)
from app.modules.co_debug.services.debug_service import (
    DebugSessionService,
)
from app.modules.co_debug.services.interactive_debug_service import (
    InteractiveDebugService,
)
from app.platform.api.deps import (
    get_debug_service,
    get_interactive_debug_service,
    get_task_service,
)
from app.platform.domain.enums import (
    BackendModuleName,
    TaskType,
)
from app.platform.services.log_service import (
    TaskLogService,
)
from app.platform.services.process_runner import (
    ProcessRunner,
)
from app.platform.services.task_service import (
    TaskService,
)
from app.platform.services.task_store import (
    TaskStore,
)
from app.platform.services.workspace_service import (
    WorkspaceService,
)

DEBUG_BASE = "/api/v1/modules/co-debug/debug"
BASE = f"{DEBUG_BASE}/sessions"
CANDIDATES = f"{DEBUG_BASE}/candidates"


pytestmark = pytest.mark.skipif(
    shutil.which("gcc") is None
    or shutil.which("gdb") is None,
    reason="gcc and gdb are required",
)


def _services(
    tmp_path,
):
    workspace_service = WorkspaceService(
        tmp_path / "workspaces"
    )

    task_service = TaskService(
        workspace_service=workspace_service,
        task_store=TaskStore(
            tmp_path / "tasks.sqlite3"
        ),
        log_service=TaskLogService(
            500,
            tmp_path / "logs.sqlite3",
        ),
        process_runner=ProcessRunner(),
        default_timeout_seconds=5,
    )

    manager = DebugSessionManager()

    interactive_service = (
        InteractiveDebugService(
            task_service=task_service,
            session_manager=manager,
        )
    )

    debug_service = DebugSessionService(
        task_store=task_service.task_store,
        session_manager=manager,
        interactive_service=(
            interactive_service
        ),
    )

    return (
        task_service,
        workspace_service,
        manager,
        debug_service,
    )


async def _create_project(
    workspace_service,
):
    """
    行号：

        1 #include <stdio.h>
        2 #include <stdlib.h>
        3 int main(...)
        4     int x = ...
        5     x = x + 1;
        6     printf(...)
        7     return 0;
        8 }

    在第6行停止时：

        argv = 10
        x = 11
    """

    source = (
        '#include <stdio.h>\n'
        '#include <stdlib.h>\n'
        'int main(int argc, char **argv) {\n'
        '    int x = argc > 1 ? atoi(argv[1]) : 0;\n'
        '    x = x + 1;\n'
        '    printf("%d\\n", x);\n'
        '    return 0;\n'
        '}\n'
    )

    archive = io.BytesIO()

    with ZipFile(
        archive,
        "w",
    ) as zip_file:
        zip_file.writestr(
            "main.c",
            source,
        )

    archive.seek(0)

    project = (
        await workspace_service
        .create_from_archive(
            UploadFile(
                file=archive,
                filename="debug-api-project.zip",
            )
        )
    )

    subprocess.run(
        [
            "gcc",
            "-g",
            "-O0",
            "-o",
            "app",
            "main.c",
        ],
        cwd=project.source_path,
        check=True,
        capture_output=True,
        text=True,
    )

    return project


async def _create_candidate_project(
    workspace_service,
    *,
    filename: str,
):
    source = (
        '#include <stdio.h>\n'
        'int main(void) {\n'
        '    puts("candidate");\n'
        '    return 0;\n'
        '}\n'
    )

    archive = io.BytesIO()

    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr(
            "main.c",
            source,
        )

    archive.seek(0)

    return await workspace_service.create_from_archive(
        UploadFile(
            file=archive,
            filename=filename,
        )
    )


async def _create_candidate_build(
    task_service: TaskService,
    *,
    project_id,
    metadata: dict | None = None,
    command: list[str] | None = None,
):
    return await task_service.create_process_task(
        module=BackendModuleName.CO_DEBUG,
        project_id=project_id,
        task_type=TaskType.BUILD,
        command=(
            command
            or [
                "sh",
                "-c",
                (
                    "gcc -g -O0 -o app main.c && "
                    "printf '#!/bin/sh\nexit 0\n' > helper.sh && "
                    "chmod +x helper.sh && "
                    "printf 'plain\n' > note.txt"
                ),
            ]
        ),
        metadata=metadata or {},
        timeout_seconds=5,
        artifacts_on_success=True,
    )


def _wait_for_ready(
    client: TestClient,
    task_id: str,
    *,
    timeout: float = 5,
):
    deadline = (
        time.monotonic()
        + timeout
    )

    last_state = None

    while (
        time.monotonic()
        < deadline
    ):
        response = client.get(
            f"{BASE}/{task_id}/state"
        )

        assert (
            response.status_code
            == 200
        )

        data = (
            response.json()["data"]
        )

        last_state = data

        if (
            data["active"]
            and data["state"]
            == "READY"
        ):
            return data

        time.sleep(
            0.05
        )

    raise AssertionError(
        "debug session did not become "
        f"READY: {last_state}"
    )


def _wait_for_task_terminal(
    client: TestClient,
    task_id: str,
    *,
    timeout: float = 5,
):
    deadline = (
        time.monotonic()
        + timeout
    )

    last_task = None

    while (
        time.monotonic()
        < deadline
    ):
        response = client.get(
            f"/api/v1/tasks/{task_id}"
        )

        assert (
            response.status_code
            == 200
        )

        task = (
            response.json()["data"]
        )

        last_task = task

        if task["status"] in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
        }:
            return task

        time.sleep(
            0.05
        )

    raise AssertionError(
        "debug task did not finish: "
        f"{last_task}"
    )


def test_debug_api_routes_are_registered():
    app = create_app()

    paths = set(
        app.openapi()["paths"]
    )

    expected = {
        f"{BASE}",
        f"{BASE}/{{task_id}}",
        f"{BASE}/{{task_id}}/state",
        f"{BASE}/{{task_id}}/arguments",
        f"{BASE}/{{task_id}}/breakpoints",
        (
            f"{BASE}/{{task_id}}"
            "/breakpoints/"
            "{breakpoint_number}"
        ),
        f"{BASE}/{{task_id}}/run",
        f"{BASE}/{{task_id}}/continue",
        f"{BASE}/{{task_id}}/next",
        f"{BASE}/{{task_id}}/step",
        f"{BASE}/{{task_id}}/interrupt",
        f"{BASE}/{{task_id}}/wait",
        f"{BASE}/{{task_id}}/evaluate",
        f"{BASE}/{{task_id}}/stack-frames",
        f"{BASE}/{{task_id}}/close",
        CANDIDATES,
    }

    assert (
        expected
        <= paths
    )


def test_debug_candidates_returns_only_retained_elf_executables(
    tmp_path,
):
    (
        task_service,
        workspace_service,
        _,
        debug_service,
    ) = _services(
        tmp_path
    )

    first_project = asyncio.run(
        _create_candidate_project(
            workspace_service,
            filename="candidate-a.zip",
        )
    )
    second_project = asyncio.run(
        _create_candidate_project(
            workspace_service,
            filename="candidate-b.zip",
        )
    )

    repair_build = asyncio.run(
        _create_candidate_build(
            task_service,
            project_id=first_project.id,
            metadata={
                "operation": "dependency_repair_build",
            },
        )
    )

    regular_build = asyncio.run(
        _create_candidate_build(
            task_service,
            project_id=second_project.id,
        )
    )

    failed_build = asyncio.run(
        _create_candidate_build(
            task_service,
            project_id=first_project.id,
            command=[
                "sh",
                "-c",
                "exit 7",
            ],
        )
    )

    app = create_app()

    interactive_service = InteractiveDebugService(
        task_service=task_service,
        session_manager=DebugSessionManager(),
    )

    app.dependency_overrides[
        get_debug_service
    ] = lambda: debug_service

    app.dependency_overrides[
        get_interactive_debug_service
    ] = lambda: interactive_service

    app.dependency_overrides[
        get_task_service
    ] = lambda: task_service

    client = TestClient(
        app
    )

    try:
        repair_completed = _wait_for_task_terminal(
            client,
            str(repair_build.id),
        )
        regular_completed = _wait_for_task_terminal(
            client,
            str(regular_build.id),
        )
        failed_completed = _wait_for_task_terminal(
            client,
            str(failed_build.id),
        )

        assert repair_completed["status"] == "SUCCEEDED"
        assert regular_completed["status"] == "SUCCEEDED"
        assert failed_completed["status"] == "FAILED"

        response = client.get(
            CANDIDATES
        )

        assert response.status_code == 200

        candidates = response.json()["data"]
        by_task = {
            item["build_task_id"]: item
            for item in candidates
        }

        assert str(repair_build.id) in by_task
        assert str(regular_build.id) in by_task
        assert str(failed_build.id) not in by_task

        repair_candidate = by_task[
            str(repair_build.id)
        ]

        assert repair_candidate["project_id"] == str(
            first_project.id
        )
        assert repair_candidate["build_kind"] == "repair-build"
        assert repair_candidate["executables"] == [
            {
                "name": "app",
                "executable_path": "app",
            }
        ]

        regular_candidate = by_task[
            str(regular_build.id)
        ]
        assert regular_candidate["build_kind"] == "build"
        assert regular_candidate["executables"] == [
            {
                "name": "app",
                "executable_path": "app",
            }
        ]

        response = client.get(
            CANDIDATES,
            params={
                "project_id": str(first_project.id),
            },
        )

        assert response.status_code == 200
        filtered = response.json()["data"]
        assert [
            item["build_task_id"]
            for item in filtered
        ] == [
            str(repair_build.id)
        ]

        # Candidate discovery must stop exposing a build as soon as
        # its retained artifact workspace disappears.
        workspace_service.cleanup_task_workspaces(
            first_project.id,
            repair_build.id,
        )

        response = client.get(
            CANDIDATES,
            params={
                "project_id": str(first_project.id),
            },
        )

        assert response.status_code == 200
        assert response.json()["data"] == []

    finally:
        client.close()
        app.dependency_overrides.clear()

        asyncio.run(
            task_service.shutdown(
                grace_seconds=1
            )
        )

        task_service.close_resources_when_idle()


def test_real_gdb_debug_api_end_to_end(
    tmp_path,
):
    (
        task_service,
        workspace_service,
        manager,
        debug_service,
    ) = _services(
        tmp_path
    )

    project = asyncio.run(
        _create_project(
            workspace_service
        )
    )

    app = create_app()

    # 非常重要：
    #
    # debug API 和 platform task API
    # 必须看到同一套测试 TaskService。
    app.dependency_overrides[
        get_debug_service
    ] = lambda: debug_service

    app.dependency_overrides[
        get_task_service
    ] = lambda: task_service

    client = TestClient(
        app
    )

    try:
        # ---------------------------------
        # 1. REST API创建真实GDB Session
        # ---------------------------------

        response = client.post(
            BASE,
            json={
                "project_id": (
                    str(project.id)
                ),
                "executable_path": "app",
                "timeout_seconds": 5,
            },
        )

        assert (
            response.status_code
            == 200
        )

        created = (
            response.json()["data"]
        )

        task_id = created["id"]

        assert (
            created["task_type"]
            == "DEBUG"
        )

        assert (
            created["status"]
            == "PENDING"
        )

        assert (
            created["command"][:2]
            == [
                "gdb",
                "--interpreter=mi2",
            ]
        )

        # ---------------------------------
        # 2. 等待真实GDB READY
        # ---------------------------------

        ready = _wait_for_ready(
            client,
            task_id,
        )

        assert (
            ready["active"]
            is True
        )

        assert (
            ready["state"]
            == "READY"
        )

        # ---------------------------------
        # 3. 原来的describe接口
        # ---------------------------------

        response = client.get(
            f"{BASE}/{task_id}"
        )

        assert (
            response.status_code
            == 200
        )

        description = (
            response.json()["data"]
        )

        assert (
            description["protocol"]
            == "GDB/MI"
        )

        assert (
            "run"
            in description[
                "supported_commands"
            ]
        )

        # ---------------------------------
        # 4. 设置inferior参数
        # ---------------------------------

        response = client.post(
            f"{BASE}/{task_id}/arguments",
            json={
                "arguments": [
                    "10",
                ]
            },
        )

        assert (
            response.status_code
            == 200
        )

        assert (
            response.json()["data"]
            is True
        )

        # ---------------------------------
        # 5. 插入真实断点
        # ---------------------------------

        response = client.post(
            f"{BASE}/{task_id}/breakpoints",
            json={
                "location": "main.c:6",
            },
        )

        assert (
            response.status_code
            == 200
        )

        breakpoint = (
            response.json()["data"]
        )

        assert (
            breakpoint["number"]
            == "1"
        )

        assert (
            breakpoint["enabled"]
            is True
        )

        # ---------------------------------
        # 6. run
        # ---------------------------------

        response = client.post(
            f"{BASE}/{task_id}/run"
        )

        assert (
            response.status_code
            == 200
        )

        # 真实程序很快，run接口返回时可能
        # 已经撞到断点，因此这里两种都合法。
        assert (
            response.json()
            ["data"]["state"]
            in {
                "RUNNING",
                "STOPPED",
            }
        )

        # ---------------------------------
        # 7. 等待断点
        # ---------------------------------

        response = client.post(
            f"{BASE}/{task_id}/wait",
            json={
                "timeout_seconds": 3,
            },
        )

        assert (
            response.status_code
            == 200
        )

        stopped = (
            response.json()["data"]
        )

        assert (
            stopped["state"]
            == "STOPPED"
        )

        assert (
            stopped["current_line"]
            == 6
        )

        assert (
            stopped["stop_reason"]
            == "breakpoint-hit"
        )

        # ---------------------------------
        # 8. evaluate
        # ---------------------------------

        response = client.post(
            f"{BASE}/{task_id}/evaluate",
            json={
                "expression": "x",
            },
        )

        assert (
            response.status_code
            == 200
        )

        assert (
            response.json()["data"]
            == {
                "value": "11",
            }
        )

        # ---------------------------------
        # 9. stack frames
        # ---------------------------------

        response = client.get(

                f"{BASE}/{task_id}"
                "/stack-frames"

        )

        assert (
            response.status_code
            == 200
        )

        frames = (
            response.json()
            ["data"]["frames"]
        )

        assert (
            len(frames)
            >= 1
        )

        # ---------------------------------
        # 10. next
        # ---------------------------------

        response = client.post(
            f"{BASE}/{task_id}/next"
        )

        assert (
            response.status_code
            == 200
        )

        response = client.post(
            f"{BASE}/{task_id}/wait",
            json={
                "timeout_seconds": 3,
            },
        )

        assert (
            response.status_code
            == 200
        )

        assert (
            response.json()
            ["data"]["state"]
            == "STOPPED"
        )

        # ---------------------------------
        # 11. continue -> program exit
        # ---------------------------------

        response = client.post(

                f"{BASE}/{task_id}"
                "/continue"

        )

        assert (
            response.status_code
            == 200
        )

        response = client.post(
            f"{BASE}/{task_id}/wait",
            json={
                "timeout_seconds": 3,
            },
        )

        assert (
            response.status_code
            == 200
        )

        exited = (
            response.json()["data"]
        )

        assert (
            exited["state"]
            == "EXITED"
        )

        # ---------------------------------
        # 12. close真实GDB
        # ---------------------------------

        response = client.post(
            f"{BASE}/{task_id}/close"
        )

        assert (
            response.status_code
            == 200
        )

        closed = (
            response.json()["data"]
        )

        assert (
            closed["state"]
            == "EXITED"
        )

        assert (
            closed["active"]
            is False
        )

        # ---------------------------------
        # 13. A平台Task也必须SUCCEEDED
        # ---------------------------------

        completed = (
            _wait_for_task_terminal(
                client,
                task_id,
            )
        )

        assert (
            completed["status"]
            == "SUCCEEDED"
        )

        assert (
            completed["task_type"]
            == "DEBUG"
        )

        assert (
            completed["result"][
                "protocol"
            ]
            == "GDB/MI"
        )

        assert (
            completed["result"][
                "closed_by"
            ]
            == "client"
        )

        # ---------------------------------
        # 14. Session已经从Manager移除
        # ---------------------------------

        assert (
            manager.contains(
                task_service
                .require_task(
                    completed["id"]
                )
                .id
            )
            is False
        )

        # ---------------------------------
        # 15. Session关闭后state接口
        #     仍然可以读取最终状态
        # ---------------------------------

        response = client.get(
            f"{BASE}/{task_id}/state"
        )

        assert (
            response.status_code
            == 200
        )

        final_state = (
            response.json()["data"]
        )

        assert (
            final_state[
                "task_status"
            ]
            == "SUCCEEDED"
        )

        assert (
            final_state["active"]
            is False
        )

        assert (
            final_state["state"]
            == "EXITED"
        )

    finally:
        client.close()

        app.dependency_overrides.clear()

        asyncio.run(
            task_service.shutdown(
                grace_seconds=1
            )
        )

        task_service.close_resources_when_idle()