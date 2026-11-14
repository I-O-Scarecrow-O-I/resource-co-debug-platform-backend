import asyncio
import hashlib
import io
import json
import time
import zipfile

import httpx
import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.modules.vulnerability.client import PipelineClient
from app.modules.vulnerability.deps import get_vulnerability_task_service
from app.modules.vulnerability.schemas import VulnerabilityTaskRequest
from app.modules.vulnerability.task_service import VulnerabilityTaskService
from app.platform.api.deps import get_log_service, get_task_service
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService

ENDPOINT = "/api/v1/modules/vulnerability/tasks"
REASONING_STREAM = "vulnerability.model_reasoning"


def _categories(scan_type):
    if scan_type == "frequent_defects":
        return ["array_oob", "string_overflow", "null_pointer"]
    return ["buffer_overflow", "data_race", "memory_leak", "command_execution"]


def _evaluated_statistics(scan_type, file_name):
    categories = _categories(scan_type)
    category = categories[0]
    return {
        "status": "evaluated",
        "taxonomy_version": "contract-categories-v2",
        "statistical_unit": "ground_truth_case",
        "target_categories": categories,
        "overall": {"tp": 1, "fn": 0, "fp": 0, "tn": 0},
        "by_category": {
            name: {"tp": 1 if name == category else 0, "fn": 0, "fp": 0, "tn": 0}
            for name in categories
        },
        "cases": [
            {
                "case_id": "case-1",
                "category": category,
                "file": file_name,
                "expected": "defect",
                "outcome": "tp",
            }
        ],
        "coverage_incomplete": False,
    }


class ModelPipelineUpstream:
    def __init__(self):
        self.requests = []
        self.coverage_status = "completed"

    def handle(self, request):
        assert request.method == "POST"
        assert request.url.path == "/api/run"
        payload = json.loads(request.content)
        self.requests.append(payload)
        config = payload["feature_config"]
        scan_type = config["scan_type"]
        max_findings = config["max_findings"]
        file_name = payload["target_files"][0] if payload["target_files"] else "src/main.cpp"
        category = _categories(scan_type)[0]
        finding = {
            "file": file_name,
            "line": 1,
            "category": category,
            "analyzer": "model",
            "rule_id": "cwe-787",
            "metric_eligible": True,
        }
        statistics = (
            _evaluated_statistics(scan_type, file_name)
            if config.get("ground_truth_file")
            else {
                "status": "not_evaluated",
                "taxonomy_version": "contract-categories-v2",
                "target_categories": _categories(scan_type),
                "reason": "ground truth was not provided",
            }
        )
        artifacts = {
            "detection_mode": "model",
            "scan_type": scan_type,
            "findings": [finding],
            "coverage": [
                {"engine": "model", "status": self.coverage_status},
                {"engine": "builtin", "status": "not_requested"},
                {"engine": "cppcheck", "status": "not_requested"},
                {"engine": "clang", "status": "not_requested"},
            ],
            "contract_statistics": statistics,
            "finding_summary": {
                "candidate_count": 1,
                "returned_count": 1,
                "truncated": False,
                "max_findings": max_findings,
            },
            "usage": {"model_calls": 1, "input_tokens": 20, "output_tokens": 8},
            "summaries": [{"file": file_name, "summary": "checked"}],
            "reported_models": ["server-default-model"],
            "source_sha256": {file_name: "0" * 64},
        }
        events = [
            {"type": "start", "status": "running", "mode": "model"},
            {"type": "model.requested", "file": file_name, "call_index": 1},
            {"type": "model.reasoning.delta", "file": file_name, "text": "Inspecting source"},
            {"type": "model.content.delta", "file": file_name, "text": "TEMPORARY MODEL JSON"},
            {"type": "model.usage_recorded", "file": file_name, "input_tokens": 20},
            {
                "type": "done",
                "status": "success",
                "mode": "model",
                "files_modified": [],
                "report": "model report",
                "artifacts": artifacts,
            },
        ]
        body = "".join(json.dumps(event) + "\n" for event in events).encode()
        return httpx.Response(200, content=body)


@pytest.fixture
def model_api(tmp_path):
    upstream = ModelPipelineUpstream()
    workspace = WorkspaceService(tmp_path / "workspaces")
    logs = TaskLogService(200, tmp_path / "logs.sqlite3")
    tasks = TaskService(
        workspace_service=workspace,
        task_store=TaskStore(tmp_path / "tasks.sqlite3"),
        log_service=logs,
        process_runner=ProcessRunner(),
        default_timeout_seconds=30,
    )
    pipeline = PipelineClient(
        base_url="http://naturalcc.test",
        connect_timeout_seconds=1,
        transport=httpx.MockTransport(upstream.handle),
    )
    app = create_app()
    app.dependency_overrides.update(
        {
            get_task_service: lambda: tasks,
            get_log_service: lambda: logs,
            get_vulnerability_task_service: lambda: VulnerabilityTaskService(tasks, pipeline),
        }
    )

    source = "int main() { return 0; }\n"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("src/main.cpp", source)
        bundle.writestr("src/helper.py", "print('ok')\n")
        bundle.writestr(
            "ground_truth.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "scan_type": "frequent_defects",
                    "source_sha256": {"src/main.cpp": hashlib.sha256(source.encode()).hexdigest()},
                    "cases": [
                        {
                            "case_id": "case-1",
                            "category": "array_oob",
                            "file": "src/main.cpp",
                            "line_start": 1,
                            "line_end": 1,
                            "expected": "defect",
                        }
                    ],
                }
            ),
        )
    archive.seek(0)
    project = asyncio.run(
        workspace.create_from_archive(UploadFile(file=archive, filename="project.zip"))
    )
    client = TestClient(app)
    try:
        yield client, project, upstream
    finally:
        client.close()
        asyncio.run(tasks.shutdown(grace_seconds=0))
        tasks.close_resources_when_idle()


def _submit(api, **changes):
    client, project, _ = api
    payload = {
        "project_id": str(project.id),
        "scan_type": "frequent_defects",
        "strategy": "model_assisted",
        "scope": "targets",
        "target_files": ["src/main.cpp"],
        "severity_threshold": "low",
        "max_findings": 10,
        **changes,
    }
    return client.post(ENDPOINT, json=payload)


def _wait(client, task_id):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
        if task["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return task
        time.sleep(0.02)
    pytest.fail("task did not finish")


@pytest.mark.parametrize(
    ("scan_type", "scope", "task_type"),
    [
        ("frequent_defects", "targets", "VULNERABILITY_SCAN"),
        ("high_risk", "project", "RISK_CHECK"),
    ],
)
def test_model_tasks_use_pipeline_model_protocol(model_api, scan_type, scope, task_type):
    client, _, upstream = model_api
    target_files = ["src/main.cpp"] if scope == "targets" else []
    response = _submit(
        model_api,
        scan_type=scan_type,
        scope=scope,
        target_files=target_files,
        timeout_seconds=1800,
    )
    assert response.status_code == 200
    task = _wait(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["task_type"] == task_type
    assert task["result"]["execution"] == {
        "feature": "vulnerability_detection",
        "analyzer": "model",
        "detection_mode": "model",
        "strategy": "model_assisted",
        "status": "success",
    }
    assert task["result"]["usage"]["model_calls"] == 1
    assert task["result"]["reported_models"] == ["server-default-model"]

    payload = upstream.requests[-1]
    assert payload["feature"] == "vulnerability_detection"
    assert payload["target_files"] == target_files
    config = payload["feature_config"]
    assert config["detection_mode"] == "model"
    assert config["scan_scope"] == scope
    assert config["scan_type"] == scan_type
    assert 0 < config["analysis_timeout_seconds"] <= 1770
    assert {"analyzer", "incremental", "rule_profile", "sanitizer_report"}.isdisjoint(config)
    assert {"api_key", "runtime_model_config", "model"}.isdisjoint(payload)


def test_model_gt_is_forwarded_and_evaluated(model_api):
    client, _, upstream = model_api
    response = _submit(model_api, ground_truth_file="ground_truth.json")
    task = _wait(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert upstream.requests[-1]["feature_config"]["ground_truth_file"] == "ground_truth.json"
    assert task["result"]["contract_statistics"]["status"] == "evaluated"
    assert task["result"]["contract_statistics"]["overall"]["tp"] == 1


def test_model_reasoning_is_logged_but_temporary_content_is_not(model_api):
    client, _, _ = model_api
    response = _submit(model_api)
    task = _wait(client, response.json()["data"]["id"])
    logs = client.get(f"/api/v1/tasks/{task['id']}/logs").json()["data"]
    assert [row["message"] for row in logs if row["stream"] == REASONING_STREAM] == [
        "Inspecting source"
    ]
    assert "TEMPORARY MODEL JSON" not in json.dumps(logs)
    assert "TEMPORARY MODEL JSON" not in json.dumps(task["result"])


def test_model_coverage_failure_fails_platform_task(model_api):
    client, _, upstream = model_api
    upstream.coverage_status = "failed"
    response = _submit(model_api)
    task = _wait(client, response.json()["data"]["id"])
    assert task["status"] == "FAILED"
    assert task["error"] == "Required model scan coverage is unavailable or failed"


@pytest.mark.parametrize(
    "changes",
    [
        {"incremental": True},
        {"sanitizer_report": "report.txt"},
        {"rule_profile": "c_cpp"},
        {"target_files": ["README.md"]},
        {"timeout_seconds": 1801},
    ],
)
def test_model_schema_rejects_only_unsupported_options(model_api, changes):
    response = _submit(model_api, **changes)
    assert response.status_code == 422
    assert model_api[2].requests == []


def test_model_schema_accepts_naturalcc_source_types_and_1800_seconds():
    request = VulnerabilityTaskRequest.model_validate(
        {
            "project_id": "00000000-0000-0000-0000-000000000001",
            "scan_type": "high_risk",
            "strategy": "model_assisted",
            "scope": "targets",
            "target_files": ["main.py", "main.ts", "main.go"],
            "timeout_seconds": 1800,
        }
    )
    assert request.timeout_seconds == 1800
    with pytest.raises(ValidationError):
        VulnerabilityTaskRequest.model_validate(
            {
                **request.model_dump(),
                "timeout_seconds": 1801,
            }
        )
