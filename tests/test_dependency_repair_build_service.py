import asyncio
import io
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.co_debug.schemas.dependencies import (
    DependencyRepairBuildRequest,
)
from app.modules.co_debug.services.repair_build_service import (
    DependencyRepairBuildService,
)
from app.platform.api.deps import (
    get_dependency_repair_build_service,
)
from app.platform.domain.enums import TaskStatus
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


def _create_task_service(
    tmp_path,
) -> tuple[
    TaskService,
    WorkspaceService,
    TaskLogService,
]:
    """
    创建一套真实的 A 模块运行环境。

    这里不 Mock ProcessRunner，
    因为我们就是要验证真正的 make/gcc 调用。
    """

    workspace_service = WorkspaceService(
        storage_root=tmp_path / "workspaces"
    )

    task_store = TaskStore(
        tmp_path / "tasks.sqlite3"
    )

    log_service = TaskLogService(
        200,
        tmp_path / "logs.sqlite3",
    )

    task_service = TaskService(
        workspace_service=workspace_service,
        task_store=task_store,
        log_service=log_service,
        process_runner=ProcessRunner(),
        default_timeout_seconds=20,
    )

    return (
        task_service,
        workspace_service,
        log_service,
    )


async def _create_hidden_dependency_project(
    workspace_service: WorkspaceService,
):
    """
    创建一个真实 C 工程。

    实际源码：

        main.c -> add.h
        add.c  -> add.h

    但原始 Makefile：

        main.o: src/main.c
        add.o:  src/add.c

    故意隐藏 add.h 依赖。
    """

    archive = io.BytesIO()

    makefile_content = (
        "CC = gcc\n"
        "CFLAGS = -Iinclude -Wall\n"
        "\n"
        "app: main.o add.o\n"
        "\t$(CC) main.o add.o -o app\n"
        "\n"
        "main.o: src/main.c\n"
        "\t$(CC) $(CFLAGS) -c src/main.c -o main.o\n"
        "\n"
        "add.o: src/add.c\n"
        "\t$(CC) $(CFLAGS) -c src/add.c -o add.o\n"
    )

    main_c = (
        '#include "add.h"\n'
        "#include <stdio.h>\n"
        "\n"
        "int main(void) {\n"
        "    printf(\"%d\\n\", add(1, 2));\n"
        "    return 0;\n"
        "}\n"
    )

    add_c = (
        '#include "add.h"\n'
        "\n"
        "int add(int a, int b) {\n"
        "    return a + b;\n"
        "}\n"
    )

    add_h = (
        "#ifndef ADD_H\n"
        "#define ADD_H\n"
        "\n"
        "int add(int a, int b);\n"
        "\n"
        "#endif\n"
    )

    with ZipFile(
        archive,
        "w",
    ) as zip_file:
        zip_file.writestr(
            "Makefile",
            makefile_content,
        )

        zip_file.writestr(
            "src/main.c",
            main_c,
        )

        zip_file.writestr(
            "src/add.c",
            add_c,
        )

        zip_file.writestr(
            "include/add.h",
            add_h,
        )

    archive.seek(0)

    project = await (
        workspace_service.create_from_archive(
            archive=UploadFile(
                file=archive,
                filename="hidden-dependency.zip",
            ),
            display_name="hidden-dependency-project",
        )
    )

    return project, makefile_content


async def _create_project_without_makefile(
    workspace_service: WorkspaceService,
):
    """
    创建一个没有 Makefile 的工程，
    用来验证 B6 prepare 失败时 Task 会 FAILED。
    """

    archive = io.BytesIO()

    with ZipFile(
        archive,
        "w",
    ) as zip_file:
        zip_file.writestr(
            "main.c",
            "int main(void) { return 0; }\n",
        )

    archive.seek(0)

    return await (
        workspace_service.create_from_archive(
            archive=UploadFile(
                file=archive,
                filename="no-makefile.zip",
            ),
            display_name="no-makefile-project",
        )
    )


async def _wait_for_terminal(
    task_service: TaskService,
    task_id: UUID,
):
    """
    等待 A 的后台 Task 执行结束。
    """

    for _ in range(300):

        task = task_service.require_task(
            task_id
        )

        if task.status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            return task

        await asyncio.sleep(0.02)

    raise AssertionError(
        "task did not reach terminal state"
    )


@pytest.mark.asyncio
async def test_dependency_repair_build_end_to_end(
    tmp_path,
):
    """
    B1-B6 完整端到端测试。

    验证：

    1. A 创建独立 BUILD Workspace
    2. B1-B4 找到隐藏依赖
    3. B5 创建 Makefile.repaired
    4. B6 返回 make 命令
    5. A ProcessRunner 真正执行 make
    6. BUILD Task 成功
    7. Build Workspace 被保留
    8. 原始 source 完全不被污染
    """

    (
        task_service,
        workspace_service,
        log_service,
    ) = _create_task_service(
        tmp_path
    )

    try:
        (
            project,
            original_makefile,
        ) = await (
            _create_hidden_dependency_project(
                workspace_service
            )
        )

        repair_build_service = (
            DependencyRepairBuildService(
                task_service=task_service
            )
        )

        request = (
            DependencyRepairBuildRequest(
                project_id=project.id,
                timeout_seconds=10,
            )
        )

        # --------------------------
        # 创建 B6 BUILD Task
        # --------------------------

        task = await (
            repair_build_service.create_task(
                request
            )
        )

        completed = await _wait_for_terminal(
            task_service,
            task.id,
        )

        # --------------------------
        # Task 应真正成功
        # --------------------------

        assert (
            completed.status
            == TaskStatus.SUCCEEDED
        )

        assert completed.exit_code == 0

        assert completed.error is None

        # prepare 完成后，
        # A 应该已经把真正命令持久化。
        assert completed.command == [
            "make",
            "-f",
            "Makefile.repaired",
        ]

        # --------------------------
        # 检查 A 保存的构建结果
        # --------------------------

        assert (
            completed.result["success"]
            is True
        )

        assert (
            completed.result["artifact"][
                "build_task_id"
            ]
            == str(task.id)
        )

        assert (
            completed.result["artifact"][
                "workspace"
            ]
            == "workspace"
        )

        # --------------------------
        # 找到成功BUILD的Workspace
        # --------------------------

        build_workspace = (
            workspace_service
            .resolve_task_workspace(
                project.id,
                task.id,
            )
        )

        assert build_workspace.exists()

        # A 创建的 Workspace 必须不是 source
        assert (
            build_workspace.resolve()
            != project.source_path.resolve()
        )

        # --------------------------
        # B5 应真正生成 repaired Makefile
        # --------------------------

        repaired_makefile = (
            build_workspace
            / "Makefile.repaired"
        )

        assert repaired_makefile.is_file()

        repaired_content = (
            repaired_makefile.read_text(
                encoding="utf-8"
            )
        )

        assert (
            "# Auto-generated dependency compensation"
            in repaired_content
        )

        assert (
            "main.o: include/add.h"
            in repaired_content
        )

        assert (
            "add.o: include/add.h"
            in repaired_content
        )

        # --------------------------
        # Make/GCC应该产生真实构建结果
        # --------------------------

        assert (
            build_workspace / "main.o"
        ).is_file()

        assert (
            build_workspace / "add.o"
        ).is_file()

        executable = (
            build_workspace / "app"
        )

        assert executable.is_file()

        # --------------------------
        # 原始source必须保持不变
        # --------------------------

        source_makefile = (
            project.source_path
            / "Makefile"
        )

        assert (
            source_makefile.read_text(
                encoding="utf-8"
            )
            == original_makefile
        )

        assert not (
            project.source_path
            / "Makefile.repaired"
        ).exists()

        assert not (
            project.source_path
            / "main.o"
        ).exists()

        assert not (
            project.source_path
            / "add.o"
        ).exists()

        assert not (
            project.source_path
            / "app"
        ).exists()

        # --------------------------
        # 检查日志
        # --------------------------

        logs = log_service.history(
            task.id
        )

        messages = [
            event.message
            for event in logs
        ]

        assert any(
            "dependency repair build preparation started"
            in message
            for message in messages
        )

        assert any(
            "Makefile.repaired generated"
            in message
            for message in messages
        )

        # --------------------------
        # 检查Artifact
        # --------------------------

        artifacts = (
            task_service.list_task_artifacts(
                task.id
            )
        )

        artifact_paths = {
            path
            for path, _ in artifacts
        }

        assert (
            "Makefile.repaired"
            in artifact_paths
        )

        assert "app" in artifact_paths

    finally:
        await task_service.shutdown(
            grace_seconds=0
        )

        task_service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_dependency_repair_build_supports_target(
    tmp_path,
):
    """
    用户指定 Make target 时：

        target="app"

    最终应执行：

        make -f Makefile.repaired app
    """

    (
        task_service,
        workspace_service,
        _,
    ) = _create_task_service(
        tmp_path
    )

    try:
        (
            project,
            _,
        ) = await (
            _create_hidden_dependency_project(
                workspace_service
            )
        )

        service = (
            DependencyRepairBuildService(
                task_service=task_service
            )
        )

        task = await service.create_task(
            DependencyRepairBuildRequest(
                project_id=project.id,
                target="app",
                timeout_seconds=10,
            )
        )

        completed = await _wait_for_terminal(
            task_service,
            task.id,
        )

        assert (
            completed.status
            == TaskStatus.SUCCEEDED
        )

        assert completed.command == [
            "make",
            "-f",
            "Makefile.repaired",
            "app",
        ]

        build_workspace = (
            workspace_service
            .resolve_task_workspace(
                project.id,
                task.id,
            )
        )

        assert (
            build_workspace / "app"
        ).is_file()

    finally:
        await task_service.shutdown(
            grace_seconds=0
        )

        task_service.close_resources_when_idle()


@pytest.mark.asyncio
async def test_dependency_repair_build_fails_without_makefile(
    tmp_path,
):
    """
    工程中没有 Makefile 时：

    prepare 阶段应该失败，
    ProcessRunner不应该继续执行make，
    最终Task状态应该为FAILED。
    """

    (
        task_service,
        workspace_service,
        _,
    ) = _create_task_service(
        tmp_path
    )

    try:
        project = await (
            _create_project_without_makefile(
                workspace_service
            )
        )

        service = (
            DependencyRepairBuildService(
                task_service=task_service
            )
        )

        task = await service.create_task(
            DependencyRepairBuildRequest(
                project_id=project.id,
                timeout_seconds=10,
            )
        )

        completed = await _wait_for_terminal(
            task_service,
            task.id,
        )

        assert (
            completed.status
            == TaskStatus.FAILED
        )

        assert completed.exit_code is None

        assert completed.error is not None

        assert (
            "No Makefile"
            in completed.error
        )

        # prepare失败，因此最终command不应该被设置
        assert completed.command == []

        # 失败BUILD的Workspace按照A的逻辑应该被清理
        task_root = (
            project.root_path
            / "tasks"
            / str(task.id)
        )

        for _ in range(100):

            if not task_root.exists():
                break

            await asyncio.sleep(0.01)

        assert not task_root.exists()

    finally:
        await task_service.shutdown(
            grace_seconds=0
        )

        task_service.close_resources_when_idle()


def test_dependency_repair_build_api_creates_task(
    tmp_path,
):
    """
    测试前端实际调用的HTTP接口：

        POST
        /api/v1/modules/co-debug/dependencies/repair-build

    这里主要验证：

        Route
        → B6 Service
        → A TaskService

    能正常接通。
    """

    (
        task_service,
        workspace_service,
        _,
    ) = _create_task_service(
        tmp_path
    )

    async def prepare_project():
        return await (
            _create_hidden_dependency_project(
                workspace_service
            )
        )

    project, _ = asyncio.run(
        prepare_project()
    )

    repair_build_service = (
        DependencyRepairBuildService(
            task_service=task_service
        )
    )

    app = create_app()

    app.dependency_overrides[
        get_dependency_repair_build_service
    ] = lambda: repair_build_service

    try:
        with TestClient(app) as client:

            response = client.post(
                (
                    "/api/v1/modules/co-debug/"
                    "dependencies/repair-build"
                ),
                json={
                    "project_id": str(
                        project.id
                    ),
                    "target": None,
                    "timeout_seconds": 10,
                },
            )

        assert response.status_code == 200

        body = response.json()

        assert body["success"] is True

        task_data = body["data"]

        assert (
            task_data["project_id"]
            == str(project.id)
        )

        assert (
            task_data["task_type"]
            == "BUILD"
        )

        task_id = UUID(
            task_data["id"]
        )

        # HTTP接口只负责创建任务，
        # 后台BUILD仍然继续运行。
        completed = asyncio.run(
            _wait_for_terminal(
                task_service,
                task_id,
            )
        )

        assert (
            completed.status
            == TaskStatus.SUCCEEDED
        )

        assert completed.command == [
            "make",
            "-f",
            "Makefile.repaired",
        ]

    finally:
        asyncio.run(
            task_service.shutdown(
                grace_seconds=0
            )
        )

        task_service.close_resources_when_idle()