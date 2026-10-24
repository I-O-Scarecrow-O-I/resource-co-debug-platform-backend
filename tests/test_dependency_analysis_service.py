import io
import json
from asyncio import run
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi import UploadFile

from app.core.errors import AppError
from app.main import create_app
from app.modules.co_debug.dependency.dependency_analyzer import DependencyAnalysisError
from app.modules.co_debug.dependency.makefile_parser import MakefileParser
from app.modules.co_debug.services.dependency_service import (
    DependencyAnalysisService,
)
from app.platform.services.workspace_service import (
    WorkspaceService,
)


@pytest.mark.asyncio
async def test_dependency_analysis_service(tmp_path):

    archive = io.BytesIO()

    with ZipFile(archive, "w") as zip_file:

        zip_file.writestr(
            "include/add.h",
            """
#ifndef ADD_H
#define ADD_H
int add(int a, int b);
#endif
""",
        )

        zip_file.writestr(
            "src/main.c",
            """
#include "add.h"

int main() {
    return add(1, 2);
}
""",
        )

        zip_file.writestr(
            "src/add.c",
            """
#include "add.h"

int add(int a, int b) {
    return a + b;
}
""",
        )

        zip_file.writestr(
            "Makefile",
            """
main: main.o add.o

main.o: src/main.c
add.o: src/add.c include/add.h
""",
        )

    archive.seek(0)

    workspace_service = WorkspaceService(
        storage_root=tmp_path
    )

    project = await workspace_service.create_from_archive(
        archive=UploadFile(
            file=archive,
            filename="dependency-test.zip",
        ),
        display_name="dependency-test",
    )

    service = DependencyAnalysisService(
        workspace_service
    )

    result = service.analyze(
        project.id
    )

    assert result.project_id == project.id

    assert (
        "main.o -> include/add.h"
        in result.missing_dependencies
    )

    assert result.repair_supported is True


def test_expected_dependency_analysis_failure_becomes_app_error(monkeypatch):
    service = DependencyAnalysisService(SimpleNamespace())

    def fail(_project_id):
        raise DependencyAnalysisError(
            "依赖分析失败: 缺失头文件 defects.h 无法解析为 Makefile generated target"
        )

    monkeypatch.setattr(service, "_analyze_project", fail)

    with pytest.raises(AppError) as caught:
        service.analyze(uuid4())

    assert caught.value.status_code == 400
    assert caught.value.message == (
        "依赖分析失败: 缺失头文件 defects.h 无法解析为 Makefile generated target"
    )


def test_unknown_dependency_analysis_runtime_error_is_not_converted(monkeypatch):
    service = DependencyAnalysisService(SimpleNamespace())

    def fail(_project_id):
        raise RuntimeError("unexpected parser crash")

    monkeypatch.setattr(service, "_analyze_project", fail)

    with pytest.raises(RuntimeError, match="unexpected parser crash"):
        service.analyze(uuid4())


def test_dependency_analysis_failure_uses_standard_error_envelope(monkeypatch):
    service = DependencyAnalysisService(SimpleNamespace())
    project_id = uuid4()

    def fail(_project_id):
        raise DependencyAnalysisError(
            "依赖分析失败: 缺失头文件 defects.h 无法解析为 Makefile generated target"
        )

    monkeypatch.setattr(service, "_analyze_project", fail)
    with pytest.raises(AppError) as caught:
        service.analyze(project_id)

    app = create_app()
    response = run(app.exception_handlers[AppError](None, caught.value))
    body = json.loads(response.body)

    assert response.status_code == 400
    assert body["success"] is False
    assert body["data"] is None
    assert body["message"] == (
        "依赖分析失败: 缺失头文件 defects.h 无法解析为 Makefile generated target"
    )


def test_dependency_details_classify_absent_generated_header_and_source(tmp_path):
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "generated/version.h: VERSION scripts/gen_version.sh\n"
        "\tbash scripts/gen_version.sh $@\n"
        "generated/foo.c: schema/foo.json scripts/gen.py\n"
        "\tpython scripts/gen.py $< $@\n"
        "build/main.o: src/main.c | build\n"
    )
    model = MakefileParser().parse(makefile)

    details = DependencyAnalysisService._dependency_details(model)

    assert not (tmp_path / "generated/version.h").exists()
    assert not (tmp_path / "generated/foo.c").exists()
    assert {
        (item.target, item.target_kind, item.generated)
        for item in details
        if item.target.startswith("generated/")
    } == {
        ("generated/version.h", "generated_header", True),
        ("generated/foo.c", "generated_source", True),
    }
    assert any(
        item.target == "build/main.o"
        and item.dependency == "build"
        and item.dependency_type == "order_only"
        for item in details
    )
