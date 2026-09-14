import asyncio
import io
import shutil
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi import UploadFile

from app.modules.co_debug.debug.manager import DebugSessionManager
from app.modules.co_debug.schemas.dependencies import (
    DependencyRepairBuildRequest,
)
from app.modules.co_debug.services.interactive_debug_service import (
    InteractiveDebugService,
)
from app.modules.co_debug.services.repair_build_service import (
    DependencyRepairBuildService,
)
from app.platform.domain.enums import TaskStatus, TaskType
from app.platform.schemas.tasks import DebugTaskRequest
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


pytestmark = pytest.mark.skipif(
    shutil.which("gcc") is None
    or shutil.which("make") is None
    or shutil.which("gdb") is None,
    reason="gcc, make and gdb are required",
)


def _services(tmp_path):
    """
    创建一套真正的 A + B 运行环境。

    这里不 Mock：
    - make
    - gcc
    - gdb
    - ProcessRunner

    我们就是要验证最终真实闭环。
    """

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
        default_timeout_seconds=10,
    )

    session_manager = DebugSessionManager()

    repair_build_service = (
        DependencyRepairBuildService(
            task_service=task_service,
        )
    )

    interactive_debug_service = (
        InteractiveDebugService(
            task_service=task_service,
            session_manager=session_manager,
        )
    )

    return (
        task_service,
        workspace_service,
        session_manager,
        repair_build_service,
        interactive_debug_service,
    )


async def _create_hidden_dependency_project(
    workspace_service: WorkspaceService,
):
    """
    创建一个真实的隐藏依赖工程。

    main.c 实际依赖：

        main.c -> value.h

    但 Makefile 故意只声明：

        main.o: main.c

    缺失：

        main.o: value.h

    B1-B5 应检测并补偿，
    B6 应使用 Makefile.repaired 编译成功。

    main.c 行号：

        1  #include <stdio.h>
        2  #include "value.h"
        3
        4  int main(void) {
        5      int x = VALUE;
        6      x = x + 1;
        7      printf("%d\\n", x);
        8      return 0;
        9  }

    我们在第7行打断点，
    此时 x 应当已经变成 42。
    """

    main_c = (
        '#include <stdio.h>\n'
        '#include "value.h"\n'
        '\n'
        'int main(void) {\n'
        '    int x = VALUE;\n'
        '    x = x + 1;\n'
        '    printf("%d\\n", x);\n'
        '    return 0;\n'
        '}\n'
    )

    value_h = (
        '#ifndef VALUE_H\n'
        '#define VALUE_H\n'
        '\n'
        '#define VALUE 41\n'
        '\n'
        '#endif\n'
    )

    makefile = (
        'CC := gcc\n'
        'CFLAGS := -g -O0 -Wall\n'
        '\n'
        'app: main.o\n'
        '\t$(CC) $(CFLAGS) -o app main.o\n'
        '\n'
        'main.o: main.c\n'
        '\t$(CC) $(CFLAGS) -c main.c -o main.o\n'
    )

    archive = io.BytesIO()

    with ZipFile(
        archive,
        "w",
    ) as zip_file:
        zip_file.writestr(
            "main.c",
            main_c,
        )
        zip_file.writestr(
            "value.h",
            value_h,
        )
        zip_file.writestr(
            "Makefile",
            makefile,
        )

    archive.seek(0)

    project = await (
        workspace_service
        .create_from_archive(
            UploadFile(
                file=archive,
                filename=(
                    "build-to-debug-project.zip"
                ),
            )
        )
    )

    return project


async def _wait_for_terminal_task(
    task_service: TaskService,
    task_id: UUID,
    *,
    timeout: float = 10,
):
    """
    等待 Platform Task 进入终态。
    """

    async with asyncio.timeout(
        timeout
    ):
        while True:
            task = (
                task_service
                .require_task(task_id)
            )

            if task.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return task

            await asyncio.sleep(
                0.02
            )


async def _wait_for_debug_session(
    session_manager: DebugSessionManager,
    task_id: UUID,
    *,
    timeout: float = 5,
):
    """
    InteractiveDebugService 会先启动真实 GDB，
    等收到初始 (gdb) prompt 后才注册 Broker。

    因此这里等待 Session 真正 READY。
    """

    async with asyncio.timeout(
        timeout
    ):
        while True:
            if session_manager.contains(
                task_id
            ):
                state = await (
                    session_manager
                    .state(task_id)
                )

                if (
                    state["state"]
                    == "READY"
                ):
                    return state

            await asyncio.sleep(
                0.02
            )


@pytest.mark.asyncio
async def test_hidden_dependency_repair_build_to_real_gdb_debug(
    tmp_path,
):
    """
    B1 -> B8 最终端到端闭环。

    验证：

    1. 上传带隐藏依赖的 C 工程
    2. B1-B4 检测 main.o -> value.h 隐藏依赖
    3. B5 生成 Makefile.repaired
    4. B6 真实 make + gcc 编译
    5. BUILD Task SUCCEEDED
    6. BUILD workspace 被保留
    7. 原 project source 没有被编译产物污染

    8. B7 使用 build_task_id 创建真实 GDB Session
    9. A 使用 source_task_id 从 BUILD workspace
       复制出独立 DEBUG workspace
    10. DEBUG workspace 中能够看到 B6 编译出的 app
    11. DEBUG workspace 与 BUILD workspace 不相同

    12. GDB/MI 插入真实断点
    13. run
    14. 命中 main.c:7
    15. evaluate x == 42
    16. continue
    17. 程序正常退出
    18. close GDB
    19. DEBUG Platform Task SUCCEEDED
    20. DebugSessionManager 完成注销
    """

    (
        task_service,
        workspace_service,
        session_manager,
        repair_build_service,
        interactive_debug_service,
    ) = _services(
        tmp_path
    )

    project = await (
        _create_hidden_dependency_project(
            workspace_service
        )
    )

    debug_task_id = None

    try:
        # ==========================================
        # 第一阶段：
        # B1-B6 隐藏依赖修复 + 真实编译
        # ==========================================

        build_task = await (
            repair_build_service
            .create_task(
                DependencyRepairBuildRequest(
                    project_id=project.id,
                    timeout_seconds=10,
                )
            )
        )

        assert (
            build_task.task_type
            == TaskType.BUILD
        )

        completed_build = await (
            _wait_for_terminal_task(
                task_service,
                build_task.id,
            )
        )

        assert (
            completed_build.status
            == TaskStatus.SUCCEEDED
        ), completed_build.error

        # B6 成功后 workspace 必须被保留。
        build_workspace = (
            workspace_service
            .resolve_task_workspace(
                project.id,
                build_task.id,
            )
        )

        assert (
            build_workspace.is_dir()
        )

        # B5 真实生成 repaired Makefile。
        repaired_makefile = (
            build_workspace
            / "Makefile.repaired"
        )

        assert (
            repaired_makefile.is_file()
        )

        repaired_content = (
            repaired_makefile
            .read_text(
                encoding="utf-8"
            )
        )

        # 不依赖具体格式，
        # 但必须确认隐藏头文件被补入。
        assert (
            "value.h"
            in repaired_content
        )

        # B6 必须真正生成 executable。
        build_executable = (
            build_workspace
            / "app"
        )

        assert (
            build_executable.is_file()
        )

        # 同时应有真实中间目标。
        assert (
            build_workspace
            / "main.o"
        ).is_file()

        # ==========================================
        # 非常重要：
        # 原始 project source 不允许被污染
        # ==========================================

        assert not (
            project.source_path
            / "app"
        ).exists()

        assert not (
            project.source_path
            / "main.o"
        ).exists()

        assert not (
            project.source_path
            / "Makefile.repaired"
        ).exists()

        # ==========================================
        # 第二阶段：
        # B6 BUILD task -> B7 interactive debug
        # ==========================================

        debug_task = await (
            interactive_debug_service
            .create_task(
                DebugTaskRequest(
                    project_id=project.id,

                    # 这是整个测试最关键的一项。
                    build_task_id=(
                        build_task.id
                    ),

                    # 相对于 BUILD workspace。
                    executable_path="app",

                    timeout_seconds=10,
                )
            )
        )

        debug_task_id = (
            debug_task.id
        )

        assert (
            debug_task.task_type
            == TaskType.DEBUG
        )

        # 等待真实 GDB：
        #
        # gdb --interpreter=mi2
        #
        # 启动并收到初始 prompt。
        ready = await (
            _wait_for_debug_session(
                session_manager,
                debug_task.id,
            )
        )

        assert (
            ready["state"]
            == "READY"
        )

        # ==========================================
        # 第三阶段：
        # 验证 A 的 source_task_id
        # 真正做了独立 workspace copy
        # ==========================================

        #
        # InteractiveDebugService 当前使用：
        #
        #     context.create_workspace("debug")
        #
        # 因此 DEBUG workspace 路径为：
        #
        debug_workspace = (
            project.root_path
            / "tasks"
            / str(debug_task.id)
            / "debug"
        )

        assert (
            debug_workspace.is_dir()
        )

        debug_executable = (
            debug_workspace
            / "app"
        )

        assert (
            debug_executable.is_file()
        )

        # 最核心的隔离断言：
        #
        # GDB绝对不能直接运行在B6 workspace。
        assert (
            debug_workspace.resolve()
            != build_workspace.resolve()
        )

        assert (
            debug_executable.resolve()
            != build_executable.resolve()
        )

        # 但内容应该来自 B6 编译结果。
        assert (
            debug_executable.read_bytes()
            == build_executable.read_bytes()
        )

        # ==========================================
        # 第四阶段：
        # B7 真实 GDB/MI 调试
        # ==========================================

        breakpoint = await (
            session_manager
            .insert_breakpoint(
                debug_task.id,
                location="main.c:7",
            )
        )

        assert (
            breakpoint["number"]
            is not None
        )

        assert (
            breakpoint["enabled"]
            is True
        )

        # 启动 inferior。
        await (
            session_manager
            .run(
                debug_task.id
            )
        )

        # 等待真实断点。
        stopped = await (
            session_manager
            .wait_for_stop(
                debug_task.id,
                stop_timeout_seconds=3,
                request_timeout_seconds=4,
            )
        )

        assert (
            stopped["state"]
            == "STOPPED"
        )

        assert (
            stopped["current_line"]
            == 7
        )

        assert (
            stopped["stop_reason"]
            == "breakpoint-hit"
        )

        # ==========================================
        # 第五阶段：
        # 验证真实运行时变量
        # ==========================================

        evaluated = await (
            session_manager
            .evaluate(
                debug_task.id,
                "x",
            )
        )

        assert (
            evaluated["value"]
            == "42"
        )

        # ==========================================
        # 第六阶段：
        # continue -> 程序正常退出
        # ==========================================

        await (
            session_manager
            .continue_execution(
                debug_task.id
            )
        )

        exited = await (
            session_manager
            .wait_for_stop(
                debug_task.id,
                stop_timeout_seconds=3,
                request_timeout_seconds=4,
            )
        )

        assert (
            exited["state"]
            == "EXITED"
        )

        # ==========================================
        # 第七阶段：
        # 正常关闭 GDB
        # ==========================================

        closed = await (
            session_manager
            .close_session(
                debug_task.id
            )
        )

        assert (
            closed["state"]
            == "EXITED"
        )

        # Platform Managed DEBUG Task
        # 最终也必须成功。
        completed_debug = await (
            _wait_for_terminal_task(
                task_service,
                debug_task.id,
            )
        )

        assert (
            completed_debug.status
            == TaskStatus.SUCCEEDED
        ), completed_debug.error

        assert (
            completed_debug.result[
                "protocol"
            ]
            == "GDB/MI"
        )

        assert (
            completed_debug.result[
                "closed_by"
            ]
            == "client"
        )

        # Manager 中不应残留 Session。
        assert (
            session_manager.contains(
                debug_task.id
            )
            is False
        )

        # ==========================================
        # 第八阶段：
        # Debug结束不能破坏B6 retained workspace
        # ==========================================

        assert (
            build_workspace.is_dir()
        )

        assert (
            build_executable.is_file()
        )

        # 原 source 到最后仍然保持干净。
        assert not (
            project.source_path
            / "app"
        ).exists()

        assert not (
            project.source_path
            / "main.o"
        ).exists()

        assert not (
            project.source_path
            / "Makefile.repaired"
        ).exists()

    finally:
        # 如果中途 assertion 失败，
        # 尽量关闭仍然存活的 GDB Session。
        if (
            debug_task_id
            is not None
            and session_manager.contains(
                debug_task_id
            )
        ):
            try:
                await (
                    session_manager
                    .close_session(
                        debug_task_id
                    )
                )
            except Exception:
                pass

        await (
            task_service
            .shutdown(
                grace_seconds=1
            )
        )

        task_service.close_resources_when_idle()