import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = PROJECT_ROOT / "docs" / "openapi.json"
EXPORT_SCRIPT = PROJECT_ROOT / "scripts" / "export_openapi.py"


def _run_export_check(cwd: Path, storage_root: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_NAME": "contaminated-openapi-title",
            "STORAGE_ROOT": str(storage_root),
            "NATURALCC_BASE_URL": "invalid",
            "DEFAULT_TASK_TIMEOUT_SECONDS": "invalid",
        }
    )
    return subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), "--check"],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
    )


def test_openapi_snapshot_matches_subprocess_cli_schema(tmp_path: Path) -> None:
    marker_path = tmp_path / "must-not-be-created"
    completed = _run_export_check(tmp_path, marker_path)

    assert completed.returncode == 0, completed.stderr
    assert not marker_path.exists()

    snapshot = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert len(snapshot["paths"]) == 41


def test_openapi_snapshot_contains_platform_co_debug_and_code_generation_routes() -> None:
    paths = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))["paths"]

    assert {
        "/api/v1/projects",
        "/api/v1/tasks/{task_id}/artifacts/{artifact_path}",
    } <= paths.keys()
    assert {
        "/api/v1/modules/co-debug/dependencies/analyze",
        "/api/v1/modules/co-debug/debug/sessions/{task_id}",
        "/api/v1/modules/co-debug/debug/comparisons",
        "/api/v1/modules/co-debug/debug/candidates",
        "/api/v1/modules/co-debug/metrics/debug-comparison-summary",
    } <= paths.keys()
    assert {
        "/api/v1/modules/code-generation/health",
        "/api/v1/modules/code-generation/tasks",
        "/api/v1/modules/vulnerability/tasks",
    } <= paths.keys()


def test_openapi_snapshot_describes_binary_artifacts_and_error_envelopes() -> None:
    schema = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    artifact_response = schema["paths"][
        "/api/v1/tasks/{task_id}/artifacts/{artifact_path}"
    ]["get"]["responses"]["200"]

    assert artifact_response["content"] == {
        "application/octet-stream": {
            "schema": {"type": "string", "format": "binary"}
        }
    }
    assert "application/json" not in artifact_response["content"]

    for path_item in schema["paths"].values():
        for operation in path_item.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            for status_code in ("400", "404"):
                response = operation["responses"][status_code]
                assert "application/json" in response["content"]
                assert response["content"]["application/json"]["schema"]["$ref"].startswith(
                    "#/components/schemas/ApiResponse_"
                )

    upload_response = schema["paths"]["/api/v1/projects"]["post"]["responses"]["413"]
    assert "application/json" in upload_response["content"]
    assert upload_response["content"]["application/json"]["schema"]["$ref"].startswith(
        "#/components/schemas/ApiResponse_"
    )
