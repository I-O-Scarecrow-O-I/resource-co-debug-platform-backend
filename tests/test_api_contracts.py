import io
import sys
import time
import zipfile

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_endpoint() -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["status"] == "UP"


def test_upload_project_and_query_it() -> None:
    project = _create_project()

    response = client.get(f"/api/v1/projects/{project['id']}")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == project["id"]


def test_module_registry_lists_co_debug_module() -> None:
    response = client.get("/api/v1/modules")

    assert response.status_code == 200
    modules = response.json()["data"]
    assert modules == [
        {
            "name": "co_debug",
            "route_prefix": "/modules/co-debug",
            "version": "0.1.0",
        }
    ]


def test_co_debug_metrics_route_is_mounted_under_module_prefix() -> None:
    response = client.get(
        "/api/v1/modules/co-debug/metrics/improvement-rate",
        params={"fifo_millis": 1000, "optimized_millis": 850},
    )

    assert response.status_code == 200
    assert response.json()["data"] == 15.0


def test_schedule_experiment_contract() -> None:
    project = _create_project()

    create_response = client.post(
        "/api/v1/tasks/schedule-experiments",
        json={
            "module": "co_debug",
            "project_id": project["id"],
            "strategy": "RESOURCE_AWARE",
            "tasks": [
                {"name": "short", "command": ["echo", "short"], "estimated_ms": 1000},
                {"name": "long", "command": ["echo", "long"], "estimated_ms": 3000},
            ],
        },
    )

    assert create_response.status_code == 200
    task_id = create_response.json()["data"]["id"]
    task = _wait_for_task(task_id)
    assert task["status"] == "SUCCEEDED"
    assert task["module"] == "co_debug"
    assert [item["name"] for item in task["result"]["ordered_tasks"]] == ["long", "short"]


def test_build_task_runs_controlled_subprocess() -> None:
    project = _create_project()

    create_response = client.post(
        "/api/v1/tasks/build",
        json={
            "module": "co_debug",
            "project_id": project["id"],
            "command": [sys.executable, "-c", "print('build-ok')"],
            "timeout_seconds": 10,
        },
    )

    assert create_response.status_code == 200
    task_id = create_response.json()["data"]["id"]
    task = _wait_for_task(task_id)
    assert task["status"] == "SUCCEEDED"

    logs_response = client.get(f"/api/v1/tasks/{task_id}/logs")
    messages = [event["message"] for event in logs_response.json()["data"]]
    assert "build-ok" in messages


def _create_project() -> dict:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("README.txt", "test project")
    archive.seek(0)

    response = client.post(
        "/api/v1/projects",
        files={"archive": ("project.zip", archive, "application/zip")},
        data={"name": "test-project"},
    )

    assert response.status_code == 200
    return response.json()["data"]


def _wait_for_task(task_id: str) -> dict:
    for _ in range(50):
        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()["data"]
        if task["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return task
        time.sleep(0.1)
    raise AssertionError(f"task did not finish in time: {task_id}")

