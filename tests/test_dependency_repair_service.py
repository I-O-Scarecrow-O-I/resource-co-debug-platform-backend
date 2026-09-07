from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.co_debug.services.dependency_service import (
    DependencyAnalysisService,
)
from app.platform.api.deps import (
    get_dependency_service,
)


class FakeWorkspaceService:
    """
    B5 Service 测试使用的最小 WorkspaceService。

    DependencyAnalysisService 目前只需要：

        require_project(project_id)
            ↓
        workspace.source_path
    """

    def __init__(self, source_path):
        self.workspace = SimpleNamespace(
            source_path=source_path,
        )

    def require_project(self, project_id):
        return self.workspace


def _create_test_project(tmp_path):
    """
    创建一个存在隐藏头文件依赖的最小 C 工程。

    main.c 实际依赖：
        include/add.h

    但 Makefile 中：
        main.o: main.c

    没有声明 add.h。
    """

    include_dir = tmp_path / "include"
    include_dir.mkdir()

    (include_dir / "add.h").write_text(
        "int add(int a, int b);\n",
        encoding="utf-8",
    )

    (tmp_path / "main.c").write_text(
        '#include "add.h"\n'
        "\n"
        "int main(void) {\n"
        "    return add(1, 2);\n"
        "}\n",
        encoding="utf-8",
    )

    original_makefile = (
        "app: main.o\n"
        "\tgcc main.o -o app\n"
        "\n"
        "main.o: main.c\n"
        "\tgcc -Iinclude -c main.c -o main.o\n"
    )

    makefile = tmp_path / "Makefile"

    makefile.write_text(
        original_makefile,
        encoding="utf-8",
    )

    return makefile, original_makefile


def test_dependency_repair_service_generates_preview(
    tmp_path,
):
    """
    测试 Service 层：

    project_id
        ↓
    WorkspaceService
        ↓
    B1-B4
        ↓
    B5 generate()
        ↓
    DependencyRepairResponse

    同时必须保证 source 不被修改。
    """

    makefile, original_makefile = (
        _create_test_project(tmp_path)
    )

    project_id = uuid4()

    service = DependencyAnalysisService(
        workspace_service=FakeWorkspaceService(
            tmp_path
        )
    )

    response = service.repair(
        project_id
    )

    assert response.project_id == project_id

    assert response.original_makefile == "Makefile"

    assert response.repaired_makefile == (
        "Makefile.repaired"
    )

    # B4 应该检测到隐藏依赖
    assert (
        "main.o -> include/add.h"
        in response.applied_dependencies
    )

    # B5 应该生成补偿后的 Makefile
    assert (
        "# Auto-generated dependency compensation"
        in response.repaired_content
    )

    assert (
        "main.o: include/add.h"
        in response.repaired_content
    )

    # 原始 Makefile 必须保持不变
    assert (
        makefile.read_text(encoding="utf-8")
        == original_makefile
    )

    # repair() 当前只是预览，不应该创建文件
    assert not (
        tmp_path / "Makefile.repaired"
    ).exists()


def test_dependency_repair_api(
    tmp_path,
):
    """
    测试完整 HTTP 调用链：

    POST /dependencies/repair
        ↓
    route
        ↓
    DependencyAnalysisService.repair()
        ↓
    B1-B5
        ↓
    JSON response
    """

    makefile, original_makefile = (
        _create_test_project(tmp_path)
    )

    project_id = uuid4()

    service = DependencyAnalysisService(
        workspace_service=FakeWorkspaceService(
            tmp_path
        )
    )

    app = create_app()

    app.dependency_overrides[
        get_dependency_service
    ] = lambda: service

    with TestClient(app) as client:
        response = client.post(
            (
                "/api/v1/modules/co-debug/"
                "dependencies/repair"
            ),
            params={
                "project_id": str(project_id),
            },
        )

    assert response.status_code == 200

    body = response.json()

    assert body["success"] is True

    data = body["data"]

    assert data["project_id"] == str(
        project_id
    )

    assert data["original_makefile"] == (
        "Makefile"
    )

    assert data["repaired_makefile"] == (
        "Makefile.repaired"
    )

    assert (
        "main.o -> include/add.h"
        in data["applied_dependencies"]
    )

    assert (
        "# Auto-generated dependency compensation"
        in data["repaired_content"]
    )

    assert (
        "main.o: include/add.h"
        in data["repaired_content"]
    )

    # HTTP 调用也绝对不能污染 source
    assert (
        makefile.read_text(encoding="utf-8")
        == original_makefile
    )

    assert not (
        tmp_path / "Makefile.repaired"
    ).exists()