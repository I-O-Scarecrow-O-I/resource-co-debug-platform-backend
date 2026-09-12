import io
from zipfile import ZipFile

from fastapi import UploadFile
import pytest

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