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
            "core_ids": [0, 1],
            "tasks": [
                {
                    "name": "short",
                    "command": [sys.executable, "-c", "print('short')"],
                    "estimated_ms": 1000,
                },
                {
                    "name": "long",
                    "command": [sys.executable, "-c", "print('long')"],
                    "estimated_ms": 3000,
                },
            ],
        },
    )

    assert create_response.status_code == 200
    task_id = create_response.json()["data"]["id"]
    task = _wait_for_task(task_id)
    assert task["status"] == "SUCCEEDED"
    assert task["module"] == "co_debug"
    assert [item["name"] for item in task["result"]["ordered_tasks"]] == ["long", "short"]
    assert task["result"]["core_ids"] == [0, 1]
    assert task["result"]["estimated_makespan_ms"] == 3000
    assert task["result"]["assignments"] == [
        {
            "task_name": "long",
            "core_id": 0,
            "queue_position": 0,
            "estimated_start_ms": 0,
            "estimated_finish_ms": 3000,
        },
        {
            "task_name": "short",
            "core_id": 1,
            "queue_position": 0,
            "estimated_start_ms": 0,
            "estimated_finish_ms": 1000,
        },
    ]
    assert task["result"]["execution"]["all_succeeded"] is True
    assert task["result"]["execution"]["actual_makespan_ms"] > 0
    assert task["elapsed_ms"] == task["result"]["execution"]["actual_makespan_ms"]


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


def test_schedule_comparison_runs_fifo_and_optimized_strategies() -> None:
    project = _create_project()

    create_response = client.post(
        "/api/v1/tasks/schedule-comparisons",
        json={
            "module": "co_debug",
            "project_id": project["id"],
            "core_ids": [0, 1],
            "workloads": [
                {
                    "name": "development-case",
                    "tasks": [
                        _sleep_task_payload("long-a", 0.08, 80),
                        _sleep_task_payload("short-a", 0.01, 10),
                        _sleep_task_payload("long-b", 0.08, 80),
                        _sleep_task_payload("short-b", 0.01, 10),
                    ],
                }
            ],
            "timeout_seconds": 5,
        },
    )

    assert create_response.status_code == 200
    task_id = create_response.json()["data"]["id"]
    task = _wait_for_task(task_id)
    assert task["status"] == "SUCCEEDED"
    assert task["task_type"] == "SCHEDULE_COMPARISON"

    result = task["result"]
    assert result["workload_count"] == 1
    assert result["has_required_workload_count"] is False
    assert result["all_tasks_succeeded"] is True
    assert len(result["workload_results"]) == 1
    workload = result["workload_results"][0]
    assert workload["workload_name"] == "development-case"
    assert workload["cost_estimation_source"] == "FIFO_ACTUAL_DURATION"
    assert workload["fifo"]["plan"]["strategy"] == "FIFO_BASELINE"
    assert workload["optimized"]["plan"]["strategy"] == "RESOURCE_AWARE"
    assert len(workload["fifo"]["execution"]["task_results"]) == 4
    assert len(workload["optimized"]["execution"]["task_results"]) == 4
    fifo_durations = {
        item["task_name"]: max(item["elapsed_ms"], 1)
        for item in workload["fifo"]["execution"]["task_results"]
    }
    optimized_estimates = {
        item["name"]: item["estimated_ms"]
        for item in workload["optimized"]["plan"]["ordered_tasks"]
    }
    assert optimized_estimates == fifo_durations


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


def _sleep_task_payload(name: str, seconds: float, estimated_ms: int) -> dict:
    return {
        "name": name,
        "command": [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        "estimated_ms": estimated_ms,
    }


def _wait_for_task(task_id: str) -> dict:
    for _ in range(50):
        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()["data"]
        if task["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return task
        time.sleep(0.1)
    raise AssertionError(f"task did not finish in time: {task_id}")

