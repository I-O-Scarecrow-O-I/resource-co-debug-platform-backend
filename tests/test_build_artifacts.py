import asyncio
import io
import os
import subprocess
import sys
import time
import zipfile
from uuid import uuid4

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.core.errors import AppError
from app.core.time import utc_now
from app.main import create_app
from app.platform.api.deps import get_task_service
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


def _task_service(
    workspace_service: WorkspaceService,
    task_store: TaskStore,
    log_service: TaskLogService,
) -> TaskService:
    return TaskService(
        workspace_service=workspace_service,
        task_store=task_store,
        log_service=log_service,
        process_runner=ProcessRunner(),
        scheduler_service=None,
        schedule_execution_service=None,
        schedule_comparison_service=None,
        default_timeout_seconds=10,
    )


@pytest.fixture
def artifact_api(tmp_path):
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    task_store = TaskStore(tmp_path / "tasks.sqlite3")
    log_service = TaskLogService(100, tmp_path / "logs.sqlite3")
    task_service = _task_service(workspace_service, task_store, log_service)
    app = create_app()
    app.dependency_overrides[get_task_service] = lambda: task_service
    client = TestClient(app)

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("README.txt", "source file")
    archive.seek(0)
    project = asyncio.run(
        workspace_service.create_from_archive(UploadFile(file=archive, filename="project.zip"))
    )

    yield client, app, project, workspace_service, task_store, log_service, task_service

    client.close()
    asyncio.run(task_service.shutdown(grace_seconds=0))
    task_service.close_resources_when_idle()


def _create_succeeded_build(client: TestClient, project_id: str) -> str:
    response = client.post(
        "/api/v1/tasks/build",
        json={
            "project_id": project_id,
            "command": [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; Path('z-output.txt').write_text('z'); "
                    "Path('nested').mkdir(); Path('nested/a-output.txt').write_text('alpha')"
                ),
            ],
        },
    )
    assert response.status_code == 200
    task_id = response.json()["data"]["id"]
    for _ in range(50):
        task = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
        if task["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            assert task["status"] == "SUCCEEDED"
            return task_id
        time.sleep(0.1)
    raise AssertionError(f"task did not finish in time: {task_id}")


def test_succeeded_build_artifacts_survive_service_rebuild(artifact_api, tmp_path) -> None:
    client, app, project, _, task_store, log_service, task_service = artifact_api
    task_id = _create_succeeded_build(client, str(project.id))

    asyncio.run(task_service.shutdown(grace_seconds=0))
    task_service.close_resources_when_idle()
    restored_workspace = WorkspaceService(tmp_path / "workspaces")
    restored_store = TaskStore(tmp_path / "tasks.sqlite3")
    restored_logs = TaskLogService(100, tmp_path / "restored-logs.sqlite3")
    restored_service = _task_service(restored_workspace, restored_store, restored_logs)
    app.dependency_overrides[get_task_service] = lambda: restored_service

    try:
        list_response = client.get(f"/api/v1/tasks/{task_id}/artifacts")
        assert list_response.status_code == 200
        artifacts = list_response.json()["data"]
        assert artifacts == [
            {"path": "README.txt", "size_bytes": 11},
            {"path": "nested/a-output.txt", "size_bytes": 5},
            {"path": "z-output.txt", "size_bytes": 1},
        ]
        assert all(str(tmp_path) not in artifact["path"] for artifact in artifacts)
        assert [artifact["path"] for artifact in artifacts] == sorted(
            artifact["path"] for artifact in artifacts
        )

        download_response = client.get(f"/api/v1/tasks/{task_id}/artifacts/nested/a-output.txt")
        assert download_response.status_code == 200
        assert download_response.content == b"alpha"
        assert download_response.headers["content-disposition"].startswith(
            'attachment; filename="a-output.txt"'
        )
    finally:
        asyncio.run(restored_service.shutdown(grace_seconds=0))
        restored_service.close_resources_when_idle()
        restored_store.close()
        restored_logs.close()

    task_store.close()
    log_service.close()


def test_artifact_access_rejects_basic_paths_and_ineligible_tasks(artifact_api) -> None:
    client, _, project, workspace_service, task_store, _, _ = artifact_api
    task_id = _create_succeeded_build(client, str(project.id))

    for path in ["%2E%2E/README.txt", "nested"]:
        response = client.get(f"/api/v1/tasks/{task_id}/artifacts/{path}")
        assert response.status_code == 400

    missing_response = client.get(f"/api/v1/tasks/{task_id}/artifacts/missing.txt")
    assert missing_response.status_code == 404

    for task_type, status in [
        (TaskType.DEBUG, TaskStatus.SUCCEEDED),
        (TaskType.BUILD, TaskStatus.FAILED),
    ]:
        task = TaskRecord(
            id=uuid4(),
            module=BackendModuleName.CO_DEBUG,
            project_id=project.id,
            task_type=task_type,
            status=status,
            command=["test"],
            created_at=utc_now(),
        )
        task_store.save(task)
        response = client.get(f"/api/v1/tasks/{task.id}/artifacts")
        assert response.status_code == 400


def test_task_artifact_resolution_rejects_windows_anchored_paths(artifact_api) -> None:
    client, _, project, workspace_service, _, _, _ = artifact_api
    task_id = _create_succeeded_build(client, str(project.id))

    for artifact_path in [
        "C:\\artifact.txt",
        "\\artifact.txt",
        "\\\\server\\share\\artifact.txt",
        "C:artifact.txt",
    ]:
        with pytest.raises(AppError, match="non-empty relative path"):
            workspace_service.resolve_task_artifact(project.id, task_id, artifact_path)


def test_artifact_access_rejects_symbolic_links(artifact_api) -> None:
    client, _, project, workspace_service, _, _, _ = artifact_api
    task_id = _create_succeeded_build(client, str(project.id))
    workspace = workspace_service.resolve_task_workspace(project.id, task_id)
    outside = workspace.parent / "outside.txt"
    outside.write_text("outside")
    try:
        (workspace / "escape.txt").symlink_to(outside)
        (workspace / "escape-dir").symlink_to(outside.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable in this environment")

    assert client.get(f"/api/v1/tasks/{task_id}/artifacts/escape.txt").status_code == 400
    assert (
        client.get(f"/api/v1/tasks/{task_id}/artifacts/escape-dir/outside.txt").status_code == 400
    )
    assert all(
        artifact["path"] not in {"escape.txt", "escape-dir/outside.txt"}
        for artifact in client.get(f"/api/v1/tasks/{task_id}/artifacts").json()["data"]
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions are only available on Windows")
def test_artifact_access_rejects_workspace_junction(artifact_api) -> None:
    client, _, project, workspace_service, _, _, _ = artifact_api
    task_id = _create_succeeded_build(client, str(project.id))
    workspace = workspace_service.resolve_task_workspace(project.id, task_id)
    target = workspace.parent / "outside-directory"
    target.mkdir()
    (target / "outside.txt").write_text("outside")
    junction = workspace / "junction"
    _create_junction(junction, target)

    try:
        assert (
            client.get(f"/api/v1/tasks/{task_id}/artifacts/junction/outside.txt").status_code
            == 400
        )
        artifacts = client.get(f"/api/v1/tasks/{task_id}/artifacts").json()["data"]
        assert all(artifact["path"] != "junction/outside.txt" for artifact in artifacts)
    finally:
        junction.rmdir()

    assert target.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions are only available on Windows")
def test_task_workspace_rejects_tasks_root_junction(artifact_api, tmp_path) -> None:
    _, _, project, workspace_service, _, _, _ = artifact_api
    task_id = uuid4()
    target = tmp_path / "outside-tasks"
    workspace = target / str(task_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "outside.txt").write_text("outside")
    tasks_root = project.root_path / "tasks"
    _create_junction(tasks_root, target)

    try:
        with pytest.raises(AppError, match="unsafe build workspace"):
            workspace_service.resolve_task_workspace(project.id, task_id)
    finally:
        tasks_root.rmdir()

    assert target.is_dir()


def _create_junction(junction, target) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation is unavailable: {result.stderr or result.stdout}")
