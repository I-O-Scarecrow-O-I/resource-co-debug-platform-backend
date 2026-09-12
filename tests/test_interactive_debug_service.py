import asyncio
import io
import shutil
import subprocess
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi import UploadFile

from app.modules.co_debug.debug.manager import (
    DebugSessionManager,
)
from app.modules.co_debug.services.interactive_debug_service import (
    InteractiveDebugService,
)
from app.platform.domain.enums import (
    TaskStatus,
)
from app.platform.schemas.tasks import (
    DebugTaskRequest,
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

    debug_service = InteractiveDebugService(
        task_service=task_service,
        session_manager=manager,
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
    source = (
        '#include <stdio.h>\n'
        'int main(void) {\n'
        '    int x = 41;\n'
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
                filename="debug-project.zip",
            )
        )
    )

    # 测试fixture中直接编译即可。
    # 正式产品链最终会由B6构建产物接入。
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

async def _create_concurrent_project(
    workspace_service,
):
    """
    为 B8 创建可以通过不同 argv
    区分两个真实 GDB Session 的程序。

    main.c 行号：

        1  #include <stdio.h>
        2  #include <stdlib.h>
        3  int main(...)
        4      int x = ...
        5      x = x + 1;
        6      printf(...)
        7      return 0;
        8  }

    在第6行断下以后：

        session A: argv=10 -> x=11
        session B: argv=20 -> x=21
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
                filename=(
                    "concurrent-debug-project.zip"
                ),
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

async def _wait_for_session(
    manager: DebugSessionManager,
    task_id: UUID,
    *,
    timeout: float = 5,
):
    async with asyncio.timeout(
        timeout
    ):
        while not manager.contains(
            task_id
        ):
            await asyncio.sleep(
                0.01
            )


async def _wait_for_terminal(
    task_service: TaskService,
    task_id: UUID,
    *,
    timeout: float = 5,
):
    async with asyncio.timeout(
        timeout
    ):
        while True:
            task = (
                task_service
                .require_task(
                    task_id
                )
            )

            if task.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return task

            await asyncio.sleep(
                0.01
            )


@pytest.mark.asyncio
async def test_real_gdb_mi_debug_session(
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

    try:
        project = await _create_project(
            workspace_service
        )

        task = await debug_service.create_task(
            DebugTaskRequest(
                project_id=project.id,
                executable_path="app",
                timeout_seconds=3,
            )
        )

        assert (
            task.status
            == TaskStatus.PENDING
        )

        await _wait_for_session(
            manager,
            task.id,
        )

        initial = await manager.state(
            task.id
        )

        assert (
            initial["state"]
            == "READY"
        )

        breakpoint = (
            await manager
            .insert_breakpoint(
                task.id,
                "main.c:5",
            )
        )

        assert (
            breakpoint["number"]
            is not None
        )

        await manager.run(
            task.id
        )

        stopped = await manager.wait_for_stop(
            task.id,
            stop_timeout_seconds=3,
            request_timeout_seconds=4,
        )

        assert (
            stopped["state"]
            == "STOPPED"
        )

        assert (
            stopped["current_line"]
            == 5
        )

        evaluated = await manager.evaluate(
            task.id,
            "x",
        )

        assert (
            evaluated["value"]
            == "42"
        )

        await manager.next(
            task.id
        )

        stepped = await manager.wait_for_stop(
            task.id,
            stop_timeout_seconds=3,
            request_timeout_seconds=4,
        )

        assert (
            stepped["state"]
            == "STOPPED"
        )

        await manager.continue_execution(
            task.id
        )

        exited = await manager.wait_for_stop(
            task.id,
            stop_timeout_seconds=3,
            request_timeout_seconds=4,
        )

        assert (
            exited["state"]
            == "EXITED"
        )

        result = await manager.close_session(
            task.id
        )

        assert (
            result["state"]
            == "EXITED"
        )

        completed = await _wait_for_terminal(
            task_service,
            task.id,
        )

        assert (
            completed.status
            == TaskStatus.SUCCEEDED
        )

        assert (
            completed.task_type.value
            == "DEBUG"
        )

        assert (
            completed.result[
                "protocol"
            ]
            == "GDB/MI"
        )

        assert (
            completed.result[
                "closed_by"
            ]
            == "client"
        )

        assert (
            manager.contains(
                task.id
            )
            is False
        )

        logs = (
            task_service.log_service
            .history(task.id)
        )

        assert any(
            event.stream == "co_debug.gdb"
            for event in logs
        )

        assert any(
            event.stream == "stdout"
            for event in logs
        )

    finally:
        await task_service.shutdown(
            grace_seconds=1
        )

        task_service.close_resources_when_idle()

@pytest.mark.asyncio
async def test_two_real_gdb_sessions_are_isolated(
    tmp_path,
):
    """
    B8 真实多调试会话验收测试。

    同一个工程同时创建两个 DEBUG Managed Task：

        task A
            ↓
        broker A
            ↓
        GdbMiSession A
            ↓
        real GDB A

        task B
            ↓
        broker B
            ↓
        GdbMiSession B
            ↓
        real GDB B

    两个 inferior 使用不同 argv：

        A: 10 -> x == 11
        B: 20 -> x == 21

    同时验证：
        1. 两个真实 GDB 能同时存在
        2. MI 命令不会串 Session
        3. MI 返回不会串 Session
        4. 一个 Session 退出不会影响另一个
        5. 两个 Managed DEBUG Task 独立成功
    """

    (
        task_service,
        workspace_service,
        manager,
        debug_service,
    ) = _services(
        tmp_path
    )

    try:
        project = (
            await _create_concurrent_project(
                workspace_service
            )
        )

        # ---------------------------------
        # 创建两个真正独立的 Managed Task
        # ---------------------------------

        first_task, second_task = (
            await asyncio.gather(
                debug_service.create_task(
                    DebugTaskRequest(
                        project_id=project.id,
                        executable_path="app",
                        timeout_seconds=5,
                    )
                ),
                debug_service.create_task(
                    DebugTaskRequest(
                        project_id=project.id,
                        executable_path="app",
                        timeout_seconds=5,
                    )
                ),
            )
        )

        assert (
            first_task.id
            != second_task.id
        )

        # 等待两个真实GDB都收到初始(gdb) prompt。
        await asyncio.gather(
            _wait_for_session(
                manager,
                first_task.id,
            ),
            _wait_for_session(
                manager,
                second_task.id,
            ),
        )

        assert (
            manager.contains(
                first_task.id
            )
            is True
        )

        assert (
            manager.contains(
                second_task.id
            )
            is True
        )

        assert set(
            manager.list_session_ids()
        ) == {
            first_task.id,
            second_task.id,
        }

        # ---------------------------------
        # 给两个inferior设置不同参数
        # ---------------------------------

        await asyncio.gather(
            manager.set_arguments(
                first_task.id,
                ["10"],
            ),
            manager.set_arguments(
                second_task.id,
                ["20"],
            ),
        )

        # ---------------------------------
        # 两边分别插入断点
        # ---------------------------------

        first_breakpoint, second_breakpoint = (
            await asyncio.gather(
                manager.insert_breakpoint(
                    first_task.id,
                    "main.c:6",
                ),
                manager.insert_breakpoint(
                    second_task.id,
                    "main.c:6",
                ),
            )
        )

        # 两个GDB自己都有自己的 breakpoint #1。
        assert (
            first_breakpoint["number"]
            == "1"
        )

        assert (
            second_breakpoint["number"]
            == "1"
        )

        # ---------------------------------
        # 真正并发运行两个inferior
        # ---------------------------------

        await asyncio.gather(
            manager.run(
                first_task.id
            ),
            manager.run(
                second_task.id
            ),
        )

        first_stopped, second_stopped = (
            await asyncio.gather(
                manager.wait_for_stop(
                    first_task.id,
                    stop_timeout_seconds=4,
                    request_timeout_seconds=5,
                ),
                manager.wait_for_stop(
                    second_task.id,
                    stop_timeout_seconds=4,
                    request_timeout_seconds=5,
                ),
            )
        )

        assert (
            first_stopped["state"]
            == "STOPPED"
        )

        assert (
            second_stopped["state"]
            == "STOPPED"
        )

        assert (
            first_stopped["current_line"]
            == 6
        )

        assert (
            second_stopped["current_line"]
            == 6
        )

        # ---------------------------------
        # 最关键的隔离验证
        #
        # A必须看到11
        # B必须看到21
        # ---------------------------------

        first_value, second_value = (
            await asyncio.gather(
                manager.evaluate(
                    first_task.id,
                    "x",
                ),
                manager.evaluate(
                    second_task.id,
                    "x",
                ),
            )
        )

        assert first_value == {
            "value": "11"
        }

        assert second_value == {
            "value": "21"
        }

        # ---------------------------------
        # 先只让第一个Session继续运行。
        #
        # 第二个必须仍保持STOPPED。
        # 这是非常重要的独立性验证。
        # ---------------------------------

        await manager.continue_execution(
            first_task.id
        )

        first_exited = (
            await manager.wait_for_stop(
                first_task.id,
                stop_timeout_seconds=4,
                request_timeout_seconds=5,
            )
        )

        assert (
            first_exited["state"]
            == "EXITED"
        )

        second_still_stopped = (
            await manager.state(
                second_task.id
            )
        )

        assert (
            second_still_stopped["state"]
            == "STOPPED"
        )

        assert (
            second_still_stopped[
                "current_line"
            ]
            == 6
        )

        # 第二个Session应该仍然能读取自己的值。
        second_value_again = (
            await manager.evaluate(
                second_task.id,
                "x",
            )
        )

        assert second_value_again == {
            "value": "21"
        }

        # ---------------------------------
        # 再结束第二个inferior
        # ---------------------------------

        await manager.continue_execution(
            second_task.id
        )

        second_exited = (
            await manager.wait_for_stop(
                second_task.id,
                stop_timeout_seconds=4,
                request_timeout_seconds=5,
            )
        )

        assert (
            second_exited["state"]
            == "EXITED"
        )

        # ---------------------------------
        # 并发关闭两个GDB Session
        # ---------------------------------

        first_close, second_close = (
            await asyncio.gather(
                manager.close_session(
                    first_task.id
                ),
                manager.close_session(
                    second_task.id
                ),
            )
        )

        assert (
            first_close["state"]
            == "EXITED"
        )

        assert (
            second_close["state"]
            == "EXITED"
        )

        # ---------------------------------
        # 两个A Managed Task也必须独立成功
        # ---------------------------------

        first_completed, second_completed = (
            await asyncio.gather(
                _wait_for_terminal(
                    task_service,
                    first_task.id,
                ),
                _wait_for_terminal(
                    task_service,
                    second_task.id,
                ),
            )
        )

        assert (
            first_completed.status
            == TaskStatus.SUCCEEDED
        )

        assert (
            second_completed.status
            == TaskStatus.SUCCEEDED
        )

        assert (
            first_completed.result[
                "protocol"
            ]
            == "GDB/MI"
        )

        assert (
            second_completed.result[
                "protocol"
            ]
            == "GDB/MI"
        )

        assert (
            first_completed.result[
                "closed_by"
            ]
            == "client"
        )

        assert (
            second_completed.result[
                "closed_by"
            ]
            == "client"
        )

        # ---------------------------------
        # Manager最终不能残留Session
        # ---------------------------------

        assert (
            manager.contains(
                first_task.id
            )
            is False
        )

        assert (
            manager.contains(
                second_task.id
            )
            is False
        )

        assert (
            manager.list_session_ids()
            == []
        )

    finally:
        await task_service.shutdown(
            grace_seconds=1
        )

        task_service.close_resources_when_idle()
    