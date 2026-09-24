import asyncio
import io
import json
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.core.time import utc_now
from app.main import create_app
from app.modules.code_generation.client import NaturalCCClientError, NaturalCCCreateError
from app.modules.code_generation.deps import get_code_generation_task_service
from app.modules.code_generation.schemas import CodeGenerationTaskRequest
from app.modules.code_generation.task_service import CodeGenerationTaskService
from app.modules.vulnerability.client import PipelineClient
from app.platform.api.deps import get_log_service, get_task_service
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


class FakeNaturalCCService:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.created_requests = []
        self.approvals: list[tuple[str, str]] = []
        self.approval_tool_call_ids: list[str | None] = []
        self.cancelled_runs: list[str] = []
        self.call_order: list[str] = []
        self.create_started = threading.Event()
        self.allow_create = threading.Event()
        self.block_create = False
        self.run_started = threading.Event()
        self.allow_run = threading.Event()
        self.block_run = False
        self.run_timeouts: list[float | None] = []
        self.approve_started = threading.Event()
        self.allow_approve = threading.Event()
        self.block_approve = False
        self.approve_timeouts: list[float | None] = []
        self.run_states: list[dict] = []
        self.run_exception: Exception | None = None
        self.run_exception_raised = threading.Event()
        self.fail_approve = False
        self.approve_exception: Exception | None = None
        self.cancel_exception: Exception | None = None
        self.cancel_failures = 0
        self.create_exception: Exception | None = None
        self.get_run_exception: Exception | None = None
        self.cancel_timeouts: list[float | None] = []
        self.get_run_timeouts: list[float | None] = []
        self.get_run_states: list[dict] = []
        self.event_responses: dict[int, dict] = {}
        self.events_exception: Exception | None = None
        self.event_afters: list[int] = []
        self.event_timeouts: list[float | None] = []
        self.network_timeouts: list[float | None] = []
        self.generated_files: dict[str, str] = {}

    async def create_run(self, *, workspace: Path, request) -> dict:
        self.call_order.append("create")
        self.create_started.set()
        if self.block_create:
            await asyncio.to_thread(self.allow_create.wait)
        if self.create_exception is not None:
            raise self.create_exception
        self.created_requests.append((workspace, request))
        return {"run_id": "naturalcc-run-1"}

    async def approve(
        self,
        run_id: str,
        risk: str,
        tool_call_id: str | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict:
        self.call_order.append("approve")
        self.approve_timeouts.append(timeout_seconds)
        self.network_timeouts.append(timeout_seconds)
        self.approve_started.set()
        if self.block_approve:
            await asyncio.to_thread(self.allow_approve.wait)
        if self.approve_exception is not None:
            raise self.approve_exception
        if self.fail_approve:
            raise NaturalCCClientError("request failed")
        self.approvals.append((run_id, risk))
        self.approval_tool_call_ids.append(tool_call_id)
        return {"status": "running"}

    async def run(self, run_id: str, *, timeout_seconds: float | None = None) -> dict:
        self.call_order.append("run")
        self.run_timeouts.append(timeout_seconds)
        self.network_timeouts.append(timeout_seconds)
        self.run_started.set()
        if self.block_run:
            await asyncio.to_thread(self.allow_run.wait)
        if self.run_exception is not None:
            self.run_exception_raised.set()
            raise self.run_exception
        state = self.run_states.pop(0) if self.run_states else self.state
        if state["status"] == "completed":
            workspace = self.created_requests[-1][0]
            (workspace / "generated.txt").write_text("generated", encoding="utf-8")
            for path, content in self.generated_files.items():
                generated = workspace / path
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_text(content, encoding="utf-8")
        return state

    async def events(
        self,
        run_id: str,
        *,
        after: int = 0,
        timeout_seconds: float | None = None,
    ) -> dict:
        self.event_afters.append(after)
        self.event_timeouts.append(timeout_seconds)
        self.network_timeouts.append(timeout_seconds)
        if self.events_exception is not None:
            if self.run_exception is not None:
                await asyncio.to_thread(self.run_exception_raised.wait)
            raise self.events_exception
        if after in self.event_responses:
            return self.event_responses[after]
        if after >= 1:
            return {"events": []}
        return {
            "events": [
                {
                    "sequence": 1,
                    "type": "tool.started",
                    "payload": {"untrusted": "not written to platform logs"},
                }
            ]
        }

    async def cancel(self, run_id: str, *, timeout_seconds: float | None = None) -> dict:
        self.cancel_timeouts.append(timeout_seconds)
        if self.cancel_failures:
            self.cancel_failures -= 1
            raise NaturalCCClientError("request failed")
        if self.cancel_exception is not None:
            raise self.cancel_exception
        self.cancelled_runs.append(run_id)
        self.state["status"] = "cancelled"
        return {"status": "cancelled"}

    async def get_run(self, run_id: str, *, timeout_seconds: float | None = None) -> dict:
        self.call_order.append("get_run")
        self.get_run_timeouts.append(timeout_seconds)
        self.network_timeouts.append(timeout_seconds)
        if self.get_run_exception is not None:
            raise self.get_run_exception
        return self.get_run_states.pop(0) if self.get_run_states else self.state


@pytest.fixture
def code_generation_api(tmp_path):
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    task_store = TaskStore(tmp_path / "tasks.sqlite3")
    log_service = TaskLogService(100, tmp_path / "logs.sqlite3")
    remote = FakeNaturalCCService(
        {
            "status": "completed",
            "final_answer": "finished with phase-two-api-key",
            "working_state": {"changed_files": ["generated.txt"]},
        }
    )
    task_service = TaskService(
        workspace_service=workspace_service,
        task_store=task_store,
        log_service=log_service,
        process_runner=ProcessRunner(),
        default_timeout_seconds=30,
    )
    code_generation_task_service = CodeGenerationTaskService(
        task_service=task_service,
        naturalcc_service=remote,
        approve_execute=False,
    )
    app = create_app()
    app.dependency_overrides[get_task_service] = lambda: task_service
    app.dependency_overrides[get_code_generation_task_service] = (
        lambda: code_generation_task_service
    )
    app.dependency_overrides[get_log_service] = lambda: log_service
    client = TestClient(app)

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("src/main.py", "print('source')\n")
    archive.seek(0)
    project = asyncio.run(
        workspace_service.create_from_archive(UploadFile(file=archive, filename="project.zip"))
    )

    yield (
        client,
        app,
        project,
        task_store,
        log_service,
        code_generation_task_service,
        remote,
    )

    client.close()
    asyncio.run(task_service.shutdown(grace_seconds=0))
    asyncio.run(code_generation_task_service.shutdown())
    task_service.close_resources_when_idle()


def test_code_generation_request_rejects_uncontrolled_fields_and_paths(code_generation_api) -> None:
    client, _, project, _, _, _, _ = code_generation_api
    payload = _request_payload(str(project.id))

    for changes in [
        {"base_url": "http://example.invalid"},
        {"workspace": "C:/outside"},
        {"authorized_paths": ["C:/outside"]},
        {"remote_run_id": "run-1"},
        {"target_files": ["../outside.py"]},
        {"target_files": ["C:\\outside.py"]},
        {"metadata": {"ticket": "CG-2"}},
        {"api_key": "phase-two-api-key"},
        {"budget": {"max_tool_calss": 2}},
    ]:
        response = client.post(
            "/api/v1/modules/code-generation/tasks",
            json={**payload, **changes},
        )
        assert response.status_code == 422


def test_completed_run_exception_is_consumed_when_events_request_fails(
    code_generation_api,
) -> None:
    _, _, _, _, _, code_generation_task_service, remote = code_generation_api
    remote.run_exception = RuntimeError("run failed")
    remote.events_exception = NaturalCCClientError("events failed")

    class Context:
        def raise_if_cancelled(self) -> None:
            return None

    async def poll() -> None:
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        previous_handler = loop.get_exception_handler()

        def capture_unhandled(_, context: dict) -> None:
            unhandled.append(context)

        loop.set_exception_handler(capture_unhandled)
        try:
            with pytest.raises(NaturalCCClientError, match="events failed"):
                await code_generation_task_service._run_and_poll(
                    Context(),
                    "naturalcc-run-1",
                    deadline=loop.time() + 1,
                )
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        assert remote.run_exception_raised.is_set()
        assert unhandled == []

    asyncio.run(poll())


def test_code_generation_task_succeeds_preserves_artifacts_without_preapproval(
    code_generation_api,
) -> None:
    client, _, project, task_store, log_service, _, remote = code_generation_api
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json={
            **_request_payload(str(project.id)),
            "operation": "refactor",
            "timeout_seconds": 10,
            "budget": {"max_seconds": 20, "max_tool_calls": 2},
        },
    )
    assert response.status_code == 200
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)

    assert task["task_type"] == "CODE_REFACTOR"
    assert task["status"] == "SUCCEEDED"
    assert task["command"] == ["naturalcc", "refactor"]
    assert task["result"] == {
        "operation": "refactor",
        "naturalcc_run_id": "naturalcc-run-1",
        "final_answer": "finished with phase-two-api-key",
        "changed_files": ["generated.txt"],
        "self_scan": {
            "status": "unavailable",
            "error": "Pipeline scanner is not configured",
        },
    }
    assert remote.approvals == []
    _, remote_request = remote.created_requests[0]
    assert remote_request.budget["max_seconds"] == 10
    assert "authorized_paths" not in remote_request.model_dump()
    assert remote.run_timeouts and 0 < remote.run_timeouts[0] <= 10

    _wait_for_cleanup_pending(task_store, False)
    stored = task_store.require(task_store.list()[0].id)
    assert stored.metadata == {
        "naturalcc_run_id": "naturalcc-run-1",
        "naturalcc_terminal_confirmed": True,
        "cleanup_pending": False,
    }
    assert task_store.workspaces_are_held(stored.id) is False
    assert task_store.artifacts_are_available(stored.id) is True
    logs = log_service.history(task_id)
    assert [event.progress for event in logs if event.progress is not None] == [5, 55, 100]
    assert "NaturalCC event: tool.started" in [event.message for event in logs]
    assert {event.stream for event in logs if event.stream == "code_generation.adapter"} == {
        "code_generation.adapter"
    }

    artifacts = client.get(f"/api/v1/tasks/{task_id}/artifacts")
    assert artifacts.status_code == 200
    assert [artifact["path"] for artifact in artifacts.json()["data"]] == [
        "generated.txt",
        "src/main.py",
    ]
    assert client.get(f"/api/v1/tasks/{task_id}/artifacts/generated.txt").content == b"generated"


def test_failed_code_generation_task_cleans_workspace(code_generation_api) -> None:
    client, _, project, task_store, _, _, remote = code_generation_api
    remote.state = {"status": "failed"}
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "FAILED"
    assert task["error"] == "NaturalCC run ended with status failed"
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)
    assert client.get(f"/api/v1/tasks/{task_id}/artifacts").status_code == 400


@pytest.mark.parametrize(
    ("operation", "prefix"),
    [
        ("completion", "Complete the requested code change."),
        ("repair", "Repair the reported code issue."),
        ("refactor", "Refactor the requested code while preserving behavior."),
    ],
)
def test_code_generation_operation_translates_to_remote_goal(
    code_generation_api,
    operation: str,
    prefix: str,
) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    instruction = "Keep this original instruction unchanged."
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json={
            **_request_payload(str(project.id)),
            "operation": operation,
            "instruction": instruction,
        },
    )

    _wait_for_terminal(client, response.json()["data"]["id"])
    assert remote.created_requests[0][1].goal == (
        f"{prefix} Only edit the selected target files. "
        f"Do not run commands, tests, or builds.\n\n{instruction}"
    )


@pytest.mark.parametrize("operation,task_type", [
    ("repair", "CODE_REPAIR"),
    ("refactor", "CODE_REFACTOR"),
])
def test_code_operation_simulation_preserves_result_and_failure(
    code_generation_api, operation: str, task_type: str,
) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    instruction = "Change the selected function."
    payload = {
        **_request_payload(str(project.id)),
        "operation": operation,
        "instruction": instruction,
    }

    response = client.post("/api/v1/modules/code-generation/tasks", json=payload)
    task = _wait_for_terminal(client, response.json()["data"]["id"])

    assert task["task_type"] == task_type
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["changed_files"] == ["generated.txt"]
    assert task["result"]["final_answer"] == "finished with phase-two-api-key"
    assert instruction in remote.created_requests[-1][1].goal

    remote.state = {"status": "failed"}
    failed = client.post("/api/v1/modules/code-generation/tasks", json=payload)
    failed_task = _wait_for_terminal(client, failed.json()["data"]["id"])
    assert failed_task["task_type"] == task_type
    assert failed_task["status"] == "FAILED"
    assert failed_task["error"] == "NaturalCC run ended with status failed"


def test_generated_code_self_scan_returns_pipeline_findings_and_coverage(
    code_generation_api,
) -> None:
    client, _, project, _, _, service, _ = code_generation_api
    requests = []
    done = {
        "type": "done", "status": "success", "report": "local checks",
        "artifacts": {
            "findings": [{"rule": "sample"}],
            "coverage": [{"engine": "builtin", "status": "completed", "reused_files": 0}],
        },
    }

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=(json.dumps(done) + "\n").encode())

    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(handle),
    )
    response = client.post(
        "/api/v1/modules/code-generation/tasks", json=_request_payload(str(project.id))
    )
    task = _wait_for_terminal(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["changed_files"] == ["generated.txt"]
    assert task["result"]["self_scan"] == {
        "status": "completed", "findings": [{"rule": "sample"}],
        "coverage": [{"engine": "builtin", "status": "completed", "reused_files": 0}],
        "report": "local checks",
    }
    assert requests[0]["feature"] == "vulnerability_detection"
    assert requests[0]["feature_config"]["analyzer"] == "builtin"
    assert requests[0]["feature_config"]["auto_fix"] is False
    assert "api_key" not in requests[0]
    assert requests[0]["target_files"] == ["src/main.py"]
    assert client.get(f"/api/v1/tasks/{task['id']}/artifacts/generated.txt").content == b"generated"


def test_self_scan_includes_existing_changed_source_files(code_generation_api) -> None:
    client, _, project, _, _, service, remote = code_generation_api
    remote.generated_files = {"src/generated.c": "int generated(void) { return 1; }\n"}
    remote.state["working_state"] = {"changed_files": ["src/main.py", "src/generated.c"]}
    requests = []
    done = {
        "type": "done", "status": "success", "artifacts": {
            "findings": [],
            "coverage": [
                {"engine": "builtin", "status": "completed"},
                {"engine": "cppcheck", "status": "completed"},
            ],
        },
    }

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=(json.dumps(done) + "\n").encode())

    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(handle),
    )
    response = client.post(
        "/api/v1/modules/code-generation/tasks", json=_request_payload(str(project.id))
    )
    task = _wait_for_terminal(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["self_scan"]["status"] == "completed"
    assert requests[0]["target_files"] == ["src/main.py", "src/generated.c"]
    assert requests[0]["feature_config"]["analyzer"] == "cppcheck"


@pytest.mark.parametrize("changed_file", ["../outside.c", "C:/outside.c", "missing.c"])
def test_self_scan_rejects_uncontrolled_or_missing_changed_file(
    code_generation_api, changed_file: str,
) -> None:
    client, _, project, _, _, service, remote = code_generation_api
    remote.state["working_state"] = {"changed_files": [changed_file]}
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=b'{"type":"done","status":"success"}\n')

    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(handle),
    )
    response = client.post(
        "/api/v1/modules/code-generation/tasks", json=_request_payload(str(project.id))
    )
    task = _wait_for_terminal(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["self_scan"]["status"] == "failed"
    assert requests == []
    assert client.get(f"/api/v1/tasks/{task['id']}/artifacts/generated.txt").content == b"generated"


def test_self_scan_uses_disposable_copy_and_preserves_generated_artifacts(
    code_generation_api,
) -> None:
    client, _, project, _, _, service, remote = code_generation_api
    scanned_workspaces = []
    done = {"type": "done", "status": "success", "artifacts": {
        "findings": [], "coverage": [{"engine": "builtin", "status": "completed"}],
    }}

    def handle(request):
        payload = json.loads(request.content)
        scan_workspace = Path(payload["project_dir"])
        scanned_workspaces.append(scan_workspace)
        (scan_workspace / "src/main.py").write_text("corrupted", encoding="utf-8")
        return httpx.Response(200, content=(json.dumps(done) + "\n").encode())

    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(handle),
    )
    response = client.post(
        "/api/v1/modules/code-generation/tasks", json=_request_payload(str(project.id))
    )
    task = _wait_for_terminal(client, response.json()["data"]["id"])
    generated_workspace = remote.created_requests[-1][0]

    assert task["status"] == "SUCCEEDED"
    assert task["result"]["self_scan"]["status"] == "completed"
    assert scanned_workspaces[0].parent == generated_workspace.parent
    assert scanned_workspaces[0] != generated_workspace
    assert not scanned_workspaces[0].exists()
    assert (generated_workspace / "src/main.py").read_text(encoding="utf-8") == "print('source')\n"
    assert client.get(f"/api/v1/tasks/{task['id']}/artifacts/generated.txt").content == b"generated"


def test_self_scan_does_not_remove_preexisting_scan_directory(code_generation_api) -> None:
    client, _, project, _, _, service, remote = code_generation_api
    done = {"type": "done", "status": "success", "artifacts": {
        "findings": [], "coverage": [{"engine": "builtin", "status": "completed"}],
    }}
    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=(json.dumps(done) + "\n").encode()
        )),
    )
    original_run = remote.run
    preexisting = []

    async def run_with_preexisting_scan_copy(run_id, *, timeout_seconds=None):
        state = await original_run(run_id, timeout_seconds=timeout_seconds)
        scan_copy = remote.created_requests[-1][0].parent / "self-scan"
        scan_copy.mkdir()
        (scan_copy / "keep.txt").write_text("keep", encoding="utf-8")
        preexisting.append(scan_copy)
        return state

    remote.run = run_with_preexisting_scan_copy
    response = client.post(
        "/api/v1/modules/code-generation/tasks", json=_request_payload(str(project.id))
    )
    task = _wait_for_terminal(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["self_scan"]["status"] == "failed"
    assert (preexisting[0] / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_failed_self_scan_preserves_generated_artifact_and_marks_failure(
    code_generation_api,
) -> None:
    client, _, project, _, _, service, remote = code_generation_api
    done = {"type": "done", "status": "error", "artifacts": {
        "findings": [], "coverage": [{"engine": "cppcheck", "status": "unavailable"}],
    }}
    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=(json.dumps(done) + "\n").encode()
        )),
    )
    (project.source_path / "src/main.c").write_text("int main(void) { return 0; }\n")
    payload = {**_request_payload(str(project.id)), "target_files": ["src/main.c"]}
    response = client.post("/api/v1/modules/code-generation/tasks", json=payload)
    task = _wait_for_terminal(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["self_scan"]["status"] == "failed"
    assert task["result"]["self_scan"]["coverage"][0]["status"] == "unavailable"
    assert not (remote.created_requests[-1][0].parent / "self-scan").exists()
    assert client.get(f"/api/v1/tasks/{task['id']}/artifacts/generated.txt").content == b"generated"


def test_partial_cppcheck_self_scan_is_not_reported_as_complete(code_generation_api) -> None:
    client, _, project, _, _, service, _ = code_generation_api
    done = {"type": "done", "status": "success", "artifacts": {
        "findings": [], "coverage": [
            {"engine": "builtin", "status": "completed"},
            {"engine": "cppcheck", "status": "partial"},
        ],
    }}
    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=(json.dumps(done) + "\n").encode()
        )),
    )
    (project.source_path / "src/main.c").write_text("int main(void) { return 0; }\n")
    response = client.post("/api/v1/modules/code-generation/tasks", json={
        **_request_payload(str(project.id)), "target_files": ["src/main.c"],
    })
    task = _wait_for_terminal(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["self_scan"]["status"] == "partial"
    assert task["result"]["self_scan"]["coverage"] == [
        {"engine": "builtin", "status": "completed"},
        {"engine": "cppcheck", "status": "partial"}
    ]


@pytest.mark.parametrize("coverage", [
    [{"engine": "cppcheck", "status": "completed"}],
    [
        {"engine": "builtin", "status": "failed"},
        {"engine": "cppcheck", "status": "completed"},
    ],
])
def test_deep_self_scan_rejects_missing_or_invalid_builtin_coverage(
    code_generation_api, coverage,
) -> None:
    client, _, project, _, _, service, _ = code_generation_api
    done = {"type": "done", "status": "success", "artifacts": {
        "findings": [], "coverage": coverage,
    }}
    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=(json.dumps(done) + "\n").encode()
        )),
    )
    (project.source_path / "src/main.c").write_text("int main(void) { return 0; }\n")
    response = client.post("/api/v1/modules/code-generation/tasks", json={
        **_request_payload(str(project.id)), "target_files": ["src/main.c"],
    })
    task = _wait_for_terminal(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["self_scan"]["status"] == "failed"
    assert task["result"]["self_scan"]["coverage"] == coverage
    assert client.get(f"/api/v1/tasks/{task['id']}/artifacts/generated.txt").content == b"generated"


@pytest.mark.parametrize("coverage", [
    [],
    [{"engine": "cppcheck", "status": "completed"}],
    [{"engine": "builtin", "status": "unavailable"}],
    [{"engine": "builtin", "status": "partial"}],
])
def test_builtin_self_scan_requires_valid_builtin_coverage(code_generation_api, coverage):
    client, _, project, _, _, service, _ = code_generation_api
    done = {"type": "done", "status": "success", "artifacts": {
        "findings": [], "coverage": coverage,
    }}
    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=(json.dumps(done) + "\n").encode()
        )),
    )
    response = client.post(
        "/api/v1/modules/code-generation/tasks", json=_request_payload(str(project.id))
    )
    task = _wait_for_terminal(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["self_scan"]["status"] == "failed"
    assert task["result"]["self_scan"]["coverage"] == coverage
    assert client.get(f"/api/v1/tasks/{task['id']}/artifacts/generated.txt").content == b"generated"


class BlockingSelfScan(httpx.AsyncByteStream):
    def __init__(self):
        self.started = threading.Event()
        self.closed = threading.Event()

    async def __aiter__(self):
        self.started.set()
        yield b'{"type":"start"}\n'
        await asyncio.sleep(100)

    async def aclose(self):
        self.closed.set()


def test_cancel_during_self_scan_closes_stream(code_generation_api) -> None:
    client, _, project, _, _, service, _ = code_generation_api
    stream = BlockingSelfScan()
    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    )
    response = client.post(
        "/api/v1/modules/code-generation/tasks", json=_request_payload(str(project.id))
    )
    task_id = response.json()["data"]["id"]
    assert stream.started.wait(2)
    client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert _wait_for_terminal(client, task_id)["status"] == "CANCELLED"
    assert stream.closed.wait(2)


def test_self_scan_timeout_preserves_generated_artifact(code_generation_api) -> None:
    client, _, project, _, _, service, _ = code_generation_api
    stream = BlockingSelfScan()
    service.pipeline = PipelineClient(
        base_url="http://naturalcc.test", connect_timeout_seconds=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    )
    response = client.post("/api/v1/modules/code-generation/tasks", json={
        **_request_payload(str(project.id)), "timeout_seconds": 1,
    })
    task = _wait_for_terminal(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["self_scan"]["status"] == "failed"
    assert "timed out" in task["result"]["self_scan"]["error"]
    assert stream.closed.wait(2)
    assert client.get(f"/api/v1/tasks/{task['id']}/artifacts/generated.txt").content == b"generated"




def test_determinate_create_failure_cleans_workspace(code_generation_api) -> None:
    client, _, project, task_store, _, _, remote = code_generation_api
    remote.create_exception = NaturalCCCreateError(outcome_unknown=False)
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "FAILED"
    assert task["error"] == "NaturalCC service request failed"
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)
    assert task_store.require(task_store.list()[0].id).metadata["cleanup_pending"] is False


def test_uncertain_create_failure_keeps_workspace_and_pending_metadata(code_generation_api) -> None:
    client, _, project, task_store, _, _, remote = code_generation_api
    remote.create_exception = NaturalCCCreateError(outcome_unknown=True)
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "FAILED"
    assert (project.root_path / "tasks" / task_id).exists()
    assert task_store.require(task_store.list()[0].id).metadata["cleanup_pending"] is True


def test_atomic_hold_metadata_failure_does_not_leave_workspace_hold(
    code_generation_api,
    monkeypatch,
) -> None:
    client, _, project, task_store, _, _, remote = code_generation_api
    original_hold = task_store.hold_workspaces

    def fail_metadata_serialization(task_id, metadata_updates=None):
        assert metadata_updates == {"cleanup_pending": True}
        return original_hold(task_id, {"invalid": object()})

    monkeypatch.setattr(task_store, "hold_workspaces", fail_metadata_serialization)
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "FAILED"
    assert task["error"] == "NaturalCC task failed"
    assert remote.created_requests == []
    assert task_store.workspaces_are_held(task_store.list()[0].id) is False
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)


def test_running_code_generation_cancel_forwards_to_naturalcc(code_generation_api) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    remote.block_run = True
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    assert remote.run_started.wait(timeout=2)
    assert _wait_for_adapter_progress(client, task_id, 55)

    cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert remote.cancelled_runs
    assert set(remote.cancelled_runs) == {"naturalcc-run-1"}
    remote.allow_run.set()
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "CANCELLED"
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)


def test_create_run_cancel_race_forwards_after_remote_id_is_recorded(code_generation_api) -> None:
    client, _, project, task_store, _, _, remote = code_generation_api
    remote.block_create = True
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    assert remote.create_started.wait(timeout=2)

    cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert remote.cancelled_runs == []
    remote.allow_create.set()
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "CANCELLED"
    assert remote.cancelled_runs
    assert set(remote.cancelled_runs) == {"naturalcc-run-1"}
    stored = task_store.require(task_store.list()[0].id)
    assert stored.status.value == "CANCELLED"
    assert stored.metadata["naturalcc_run_id"] == "naturalcc-run-1"
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)


def test_cancel_during_success_finalization_uses_confirmed_cleanup_not_success_release(
    code_generation_api,
    monkeypatch,
) -> None:
    client, _, project, task_store, _, code_generation_task_service, remote = (
        code_generation_api
    )
    task_service = code_generation_task_service.task_service
    original_finalize = task_store.finalize_with_transition
    original_release = task_service.release_task_workspaces
    release_modes: list[bool] = []
    cancellation_errors: list[BaseException] = []

    def track_release(
        task_id,
        *,
        cleanup=False,
        completion_metadata=None,
    ):
        release_modes.append(cleanup)
        return original_release(
            task_id,
            cleanup=cleanup,
            completion_metadata=completion_metadata,
        )

    def cancel_then_finalize(task, **kwargs):
        def cancel() -> None:
            try:
                asyncio.run(task_service.cancel_task(task.id))
            except BaseException as exc:
                cancellation_errors.append(exc)

        cancellation = threading.Thread(target=cancel)
        cancellation.start()
        cancellation.join(timeout=5)
        if cancellation.is_alive():
            raise AssertionError("cancellation handler did not finish")
        if cancellation_errors:
            raise cancellation_errors[0]
        return original_finalize(task, **kwargs)

    monkeypatch.setattr(task_service, "release_task_workspaces", track_release)
    monkeypatch.setattr(task_store, "finalize_with_transition", cancel_then_finalize)
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "CANCELLED"
    assert release_modes and all(release_modes)
    assert remote.cancelled_runs == ["naturalcc-run-1"]
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)
    stored_id = task_store.list()[0].id
    for _ in range(100):
        with task_service._background_lock:
            if stored_id not in task_service._background_futures:
                break
        time.sleep(0.02)
    stored = task_store.require(stored_id)
    assert stored.metadata["cleanup_pending"] is False
    assert task_store.workspaces_are_held(stored.id) is False
    assert stored.id not in task_store.list_workspace_cleanup_pending()
    assert task_store.artifacts_are_available(stored.id) is False


def test_deferred_approval_network_failure_cancels_remote_run(code_generation_api) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    remote.fail_approve = True
    remote.run_states = [{"status": "waiting_approval"}]
    remote.get_run_states = [
        {
            "status": "waiting_approval",
            "pending_approval": {"risk": "write", "tool_call": {"id": "write-1"}},
        }
    ]
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "FAILED"
    assert task["error"] == "NaturalCC service request failed"
    assert remote.cancelled_runs
    assert set(remote.cancelled_runs) == {"naturalcc-run-1"}
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)


def test_deferred_approval_conflict_cancels_remote_run(code_generation_api) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    remote.approve_exception = NaturalCCClientError("approval conflict")
    remote.run_states = [{"status": "waiting_approval"}]
    remote.get_run_states = [
        {
            "status": "waiting_approval",
            "pending_approval": {"risk": "write", "tool_call": {"id": "write-1"}},
        }
    ]
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "FAILED"
    assert task["error"] == "NaturalCC service request failed"
    assert remote.cancelled_runs == ["naturalcc-run-1"]
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)


def test_unknown_failure_after_create_run_cancels_remote_run(code_generation_api) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    remote.approve_exception = RuntimeError("unexpected remote response")
    remote.run_states = [{"status": "waiting_approval"}]
    remote.get_run_states = [
        {
            "status": "waiting_approval",
            "pending_approval": {"risk": "write", "tool_call": {"id": "write-1"}},
        }
    ]
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "FAILED"
    assert task["error"] == "NaturalCC task failed"
    assert remote.cancelled_runs
    assert set(remote.cancelled_runs) == {"naturalcc-run-1"}
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)


def test_cancel_failure_does_not_change_platform_cancellation(code_generation_api) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    remote.block_run = True
    remote.cancel_exception = RuntimeError("cancel unavailable")
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    assert remote.run_started.wait(timeout=2)

    cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    remote.allow_run.set()
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "CANCELLED"
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)


def test_deferred_execute_approval_requires_explicit_opt_in(code_generation_api) -> None:
    client, _, project, _, _, code_generation_task_service, remote = code_generation_api
    code_generation_task_service.approve_execute = True
    remote.run_states = [
        {"status": "waiting_approval"},
        {"status": "waiting_approval"},
        {
            "status": "completed",
            "final_answer": "finished with phase-two-api-key",
            "working_state": {"changed_files": ["generated.txt"]},
        },
    ]
    remote.get_run_states = [
        {
            "status": "waiting_approval",
            "pending_approval": {"risk": "write", "tool_call": {"id": "write-1"}},
        },
        {
            "status": "waiting_approval",
            "pending_approval": {"risk": "execute", "tool_call": {"id": "execute-1"}},
        }
    ]

    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )

    _wait_for_terminal(client, response.json()["data"]["id"])
    assert remote.approvals == [
        ("naturalcc-run-1", "write"),
        ("naturalcc-run-1", "execute"),
    ]
    assert remote.approval_tool_call_ids == ["write-1", "execute-1"]


def test_deferred_write_approval_runs_before_approval_and_reuses_polling_state(
    code_generation_api,
) -> None:
    client, _, project, _, log_service, _, remote = code_generation_api
    remote.run_states = [
        {"status": "waiting_approval"},
        {
            "status": "completed",
            "final_answer": "finished with phase-two-api-key",
            "working_state": {"changed_files": ["generated.txt"]},
        },
    ]
    remote.get_run_states = [
        {
            "status": "waiting_approval",
            "pending_approval": {"risk": "write", "tool_call": {"id": "write-1"}},
        }
    ]
    remote.event_responses = {
        0: {"events": [{"sequence": 1, "type": "tool.started"}]},
        1: {"events": [{"sequence": 2, "type": "tool.finished"}]},
    }

    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json={**_request_payload(str(project.id)), "timeout_seconds": 10},
    )
    task = _wait_for_terminal(client, response.json()["data"]["id"])

    assert task["status"] == "SUCCEEDED"
    assert remote.call_order.index("run") < remote.call_order.index("approve")
    assert remote.approvals == [("naturalcc-run-1", "write")]
    assert remote.approval_tool_call_ids == ["write-1"]
    assert remote.call_order.count("run") == 2
    assert remote.event_afters[:2] == [0, 1]
    assert len(remote.run_timeouts) == 2
    assert 0 < remote.run_timeouts[1] <= remote.run_timeouts[0] <= 10
    assert remote.get_run_timeouts and remote.approve_timeouts and remote.event_timeouts
    assert all(timeout is not None and timeout > 0 for timeout in remote.network_timeouts)
    assert remote.event_timeouts[1] <= remote.event_timeouts[0]
    assert remote.get_run_timeouts[0] <= remote.event_timeouts[1]
    assert remote.approve_timeouts[0] <= remote.get_run_timeouts[0]
    assert remote.run_timeouts[1] <= remote.approve_timeouts[0]
    assert [
        event.progress
        for event in log_service.history(response.json()["data"]["id"])
        if event.progress is not None
    ] == [5, 55, 70, 100]


def test_deferred_execute_approval_is_rejected_without_opt_in(code_generation_api) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    remote.run_states = [{"status": "waiting_approval"}]
    remote.get_run_states = [
        {
            "status": "waiting_approval",
            "pending_approval": {"risk": "execute", "tool_call": {"id": "execute-1"}},
        }
    ]

    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task = _wait_for_terminal(client, response.json()["data"]["id"])

    assert task["status"] == "FAILED"
    assert task["error"] == "NaturalCC deferred approval for risk execute is not allowed"
    assert remote.approvals == []
    assert remote.cancelled_runs == ["naturalcc-run-1"]


def test_cancel_during_deferred_approval_does_not_start_another_run(code_generation_api) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    remote.block_approve = True
    remote.run_states = [{"status": "waiting_approval"}]
    remote.get_run_states = [
        {
            "status": "waiting_approval",
            "pending_approval": {"risk": "write", "tool_call": {"id": "write-1"}},
        }
    ]
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    assert remote.approve_started.wait(timeout=2)

    cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    remote.allow_approve.set()
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "CANCELLED"
    assert remote.call_order.count("run") == 1


@pytest.mark.parametrize(
    ("approval_state", "error"),
    [
        (
            {
                "status": "waiting_approval",
                "pending_approval": {"risk": "write", "tool_call": {}},
            },
            "NaturalCC deferred approval is invalid: tool call id is missing",
        ),
        (
            {"status": "completed"},
            "NaturalCC deferred approval changed before it could be approved",
        ),
    ],
)
def test_malformed_or_stale_deferred_approval_fails_closed_and_cancels_remote_run(
    code_generation_api,
    approval_state: dict,
    error: str,
) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    remote.run_states = [{"status": "waiting_approval"}]
    remote.get_run_states = [approval_state]

    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task = _wait_for_terminal(client, response.json()["data"]["id"])

    assert task["status"] == "FAILED"
    assert task["error"] == error
    assert remote.approvals == []
    assert remote.cancelled_runs == ["naturalcc-run-1"]


def test_deferred_non_write_or_execute_approval_fails_closed(code_generation_api) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    remote.run_states = [{"status": "waiting_approval"}]
    remote.get_run_states = [
        {
            "status": "waiting_approval",
            "pending_approval": {"risk": "network", "tool_call": {"id": "network-1"}},
        }
    ]

    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task = _wait_for_terminal(client, response.json()["data"]["id"])

    assert task["status"] == "FAILED"
    assert task["error"] == "NaturalCC deferred approval for risk network is not allowed"
    assert remote.approvals == []
    assert remote.cancelled_runs == ["naturalcc-run-1"]


def test_cancel_retries_then_confirms_remote_terminal_state(code_generation_api) -> None:
    client, _, project, _, _, _, remote = code_generation_api
    remote.block_run = True
    remote.state = {"status": "running"}
    remote.cancel_failures = 1
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    assert remote.run_started.wait(timeout=2)

    client.post(f"/api/v1/tasks/{task_id}/cancel")
    remote.allow_run.set()
    _wait_for_terminal(client, task_id)

    assert len(remote.cancel_timeouts) >= 2
    assert remote.get_run_timeouts
    assert all(timeout is not None and timeout <= 0.5 for timeout in remote.cancel_timeouts)


def test_unconfirmed_remote_cancellation_keeps_workspace_and_pending_metadata(
    code_generation_api,
) -> None:
    client, _, project, task_store, _, _, remote = code_generation_api
    remote.block_run = True
    remote.state = {"status": "running"}
    remote.cancel_exception = RuntimeError("remote unavailable")
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    assert remote.run_started.wait(timeout=2)

    client.post(f"/api/v1/tasks/{task_id}/cancel")
    remote.allow_run.set()
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "CANCELLED"
    assert (project.root_path / "tasks" / task_id).exists()
    assert task_store.require(task_store.list()[0].id).metadata["cleanup_pending"] is True


@pytest.mark.asyncio
async def test_running_cleanup_pending_retries_on_lifespan_loop_without_restart(tmp_path) -> None:
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    task_store = TaskStore(tmp_path / "tasks.sqlite3")
    log_service = TaskLogService(100, tmp_path / "logs.sqlite3")
    remote = FakeNaturalCCService({"status": "running"})
    remote.block_run = True
    remote.cancel_exception = RuntimeError("remote unavailable")
    task_service = TaskService(
        workspace_service=workspace_service,
        task_store=task_store,
        log_service=log_service,
        process_runner=ProcessRunner(),
        default_timeout_seconds=30,
    )
    code_generation_task_service = CodeGenerationTaskService(
        task_service=task_service,
        naturalcc_service=remote,
        approve_execute=False,
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("src/main.py", "print('source')\n")
    archive.seek(0)
    project = await workspace_service.create_from_archive(
        UploadFile(file=archive, filename="project.zip")
    )

    await task_service.startup()
    await code_generation_task_service.startup()
    task = await code_generation_task_service.create_code_generation_task(
        CodeGenerationTaskRequest.model_validate(_request_payload(str(project.id)))
    )
    assert await asyncio.to_thread(remote.run_started.wait, 2)

    await task_service.cancel_task(task.id)
    remote.allow_run.set()
    await _wait_for_task_status(
        task_store,
        task.id,
        TaskStatus.CANCELLED,
        attempts=300,
    )
    assert task_store.require(task.id).metadata["cleanup_pending"] is True

    remote.cancel_exception = None
    await _wait_for_cleanup(task_store, task.id, expected_pending=False)
    assert remote.cancelled_runs == ["naturalcc-run-1"]
    assert not (project.root_path / "tasks" / str(task.id)).exists()

    await task_service.shutdown(grace_seconds=0)
    await code_generation_task_service.shutdown()

    assert not code_generation_task_service._cleanup_retries
    assert task_service.can_close_resources()
    task_service.close_resources_when_idle()
    assert task_store._closed


def test_failed_cleanup_keeps_pending_queue_until_startup_retry(
    code_generation_api,
    monkeypatch,
) -> None:
    client, _, project, task_store, _, code_generation_task_service, remote = (
        code_generation_api
    )
    workspace_service = code_generation_task_service.task_service.workspace_service
    original_cleanup = workspace_service.cleanup_task_workspaces

    def fail_cleanup(_project_id, _task_id) -> None:
        raise OSError("cleanup unavailable")

    monkeypatch.setattr(workspace_service, "cleanup_task_workspaces", fail_cleanup)
    remote.state = {"status": "failed"}
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)
    stored = task_store.require(task_store.list()[0].id)

    assert task["status"] == "FAILED"
    assert (project.root_path / "tasks" / task_id).exists()
    assert stored.metadata["cleanup_pending"] is True
    assert task_store.workspaces_are_held(stored.id) is False
    assert stored.id in task_store.list_workspace_cleanup_pending()

    monkeypatch.setattr(
        workspace_service,
        "cleanup_task_workspaces",
        original_cleanup,
    )
    asyncio.run(code_generation_task_service.startup())

    assert not (project.root_path / "tasks" / task_id).exists()
    assert task_store.require(stored.id).metadata["cleanup_pending"] is False
    assert stored.id not in task_store.list_workspace_cleanup_pending()


def test_startup_retries_pending_naturalcc_cleanup_before_removing_workspace(
    code_generation_api,
) -> None:
    _, _, project, task_store, _, code_generation_task_service, remote = code_generation_api
    task_service = code_generation_task_service.task_service
    task = task_service._new_task(
        module=BackendModuleName.CODE_GENERATION,
        project_id=project.id,
        task_type=TaskType.CODE_GENERATION,
        command=["naturalcc", "completion"],
        metadata={"naturalcc_run_id": "old-run", "cleanup_pending": True},
    )
    task.status = TaskStatus.CANCELLED
    task.error = "cancelled"
    task_store.save(task)
    workspace = task_service.workspace_service.create_task_workspace(project.id, task.id)
    (workspace / "leftover.txt").write_text("leftover", encoding="utf-8")
    remote.state = {"status": "running"}

    asyncio.run(code_generation_task_service.startup())

    assert not workspace.parent.exists()
    assert task_store.require(task.id).metadata["cleanup_pending"] is False


def test_codegen_startup_cleans_held_workspace_after_platform_recovers_interruption(
    code_generation_api,
) -> None:
    _, _, project, task_store, _, code_generation_task_service, remote = code_generation_api
    task_service = code_generation_task_service.task_service
    task = task_service._new_task(
        module=BackendModuleName.CODE_GENERATION,
        project_id=project.id,
        task_type=TaskType.CODE_GENERATION,
        command=["naturalcc", "completion"],
        metadata={"naturalcc_run_id": "interrupted-run", "cleanup_pending": True},
    )
    task.status = TaskStatus.RUNNING
    task_store.save(task)
    workspace = task_service.workspace_service.create_task_workspace(project.id, task.id)
    (workspace / "remote-state.txt").write_text("pending", encoding="utf-8")
    task_service.hold_task_workspaces(task.id)
    remote.state = {"status": "running"}

    asyncio.run(task_service.startup())

    assert task_store.require(task.id).status == TaskStatus.FAILED
    assert workspace.exists()
    assert task_store.workspaces_are_held(task.id) is True

    asyncio.run(code_generation_task_service.startup())

    assert not workspace.parent.exists()
    assert task_store.workspaces_are_held(task.id) is False
    assert task_store.require(task.id).metadata["cleanup_pending"] is False


def test_legacy_codegen_hold_migration_protects_workspace_until_module_recovery(
    tmp_path,
) -> None:
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("src/main.py", "print('source')\n")
    archive.seek(0)
    project = asyncio.run(
        workspace_service.create_from_archive(
            UploadFile(file=archive, filename="project.zip")
        )
    )
    database_path = tmp_path / "tasks.sqlite3"
    legacy_store = TaskStore(database_path)
    task = TaskRecord(
        id=uuid4(),
        module=BackendModuleName.CODE_GENERATION,
        project_id=project.id,
        task_type=TaskType.CODE_GENERATION,
        status=TaskStatus.RUNNING,
        command=["naturalcc", "completion"],
        created_at=utc_now(),
        started_at=utc_now(),
        metadata={
            "naturalcc_run_id": "legacy-run",
            "cleanup_pending": True,
        },
    )
    legacy_store.save(task)
    workspace = workspace_service.create_task_workspace(project.id, task.id)
    (workspace / "remote-state.txt").write_text("pending", encoding="utf-8")
    legacy_store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE task_workspace_holds")

    task_store = TaskStore(database_path)
    task_service = TaskService(
        workspace_service=workspace_service,
        task_store=task_store,
        log_service=TaskLogService(100, tmp_path / "logs.sqlite3"),
        process_runner=ProcessRunner(),
        default_timeout_seconds=30,
    )
    remote = FakeNaturalCCService({"status": "running"})
    code_generation_task_service = CodeGenerationTaskService(
        task_service=task_service,
        naturalcc_service=remote,
        approve_execute=False,
    )
    try:
        assert task_store.workspaces_are_held(task.id) is True

        asyncio.run(task_service.startup())

        assert task_store.require(task.id).status == TaskStatus.FAILED
        assert workspace.exists()
        assert task_store.workspaces_are_held(task.id) is True

        asyncio.run(code_generation_task_service.startup())

        assert not workspace.parent.exists()
        assert task_store.workspaces_are_held(task.id) is False
        assert task_store.require(task.id).metadata["cleanup_pending"] is False
        assert task.id not in task_store.list_workspace_cleanup_pending()
    finally:
        asyncio.run(task_service.shutdown(grace_seconds=0))
        asyncio.run(code_generation_task_service.shutdown())
        task_service.close_resources_when_idle()


def test_codegen_startup_releases_success_hold_and_preserves_artifact(
    code_generation_api,
) -> None:
    _, _, project, task_store, _, code_generation_task_service, _ = code_generation_api
    task_service = code_generation_task_service.task_service
    task = task_service._new_task(
        module=BackendModuleName.CODE_GENERATION,
        project_id=project.id,
        task_type=TaskType.CODE_GENERATION,
        command=["naturalcc", "completion"],
        metadata={
            "naturalcc_run_id": "completed-run",
            "naturalcc_terminal_confirmed": True,
            "cleanup_pending": True,
        },
    )
    task.status = TaskStatus.SUCCEEDED
    task_store.save(task)
    workspace = task_service.workspace_service.create_task_workspace(project.id, task.id)
    artifact = workspace / "generated.txt"
    artifact.write_text("generated", encoding="utf-8")
    task_service.hold_task_workspaces(task.id)

    asyncio.run(task_service.startup())
    asyncio.run(code_generation_task_service.startup())

    assert artifact.read_text(encoding="utf-8") == "generated"
    assert task_store.workspaces_are_held(task.id) is False
    assert task_store.require(task.id).metadata["cleanup_pending"] is False


def test_nonterminal_run_response_is_cancelled_and_final_events_are_drained(
    code_generation_api,
) -> None:
    client, _, project, _, log_service, _, remote = code_generation_api
    remote.state = {"status": "paused"}
    remote.event_responses = {
        0: {"events": [{"sequence": 1, "type": "tool.started"}]},
        1: {"events": [{"sequence": 2, "type": "tool.finished"}]},
    }
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)

    assert task["status"] == "FAILED"
    assert task["error"] == (
        "NaturalCC run remained paused; cancelled for the approval safety policy"
    )
    assert remote.cancelled_runs == ["naturalcc-run-1"]
    _wait_for_workspace_cleanup(project.root_path / "tasks" / task_id)
    assert "NaturalCC event: tool.finished" in [
        event.message for event in log_service.history(task_id)
    ]


def test_unconfirmed_nonterminal_run_keeps_workspace_until_recovery(
    code_generation_api,
) -> None:
    client, _, project, task_store, _, code_generation_task_service, remote = (
        code_generation_api
    )
    remote.state = {"status": "paused"}
    remote.cancel_exception = RuntimeError("cancel unavailable")
    remote.get_run_exception = RuntimeError("state unavailable")
    response = client.post(
        "/api/v1/modules/code-generation/tasks",
        json=_request_payload(str(project.id)),
    )
    task_id = response.json()["data"]["id"]
    task = _wait_for_terminal(client, task_id)
    stored = task_store.require(task_store.list()[0].id)

    assert task["status"] == "FAILED"
    assert task["error"] == (
        "NaturalCC run remained paused; cancelled for the approval safety policy"
    )
    assert (project.root_path / "tasks" / task_id).exists()
    assert task_store.workspaces_are_held(stored.id) is True
    assert stored.metadata["cleanup_pending"] is True
    assert stored.id not in task_store.list_workspace_cleanup_pending()

    remote.cancel_exception = None
    remote.get_run_exception = None
    asyncio.run(code_generation_task_service.startup())

    assert not (project.root_path / "tasks" / task_id).exists()
    assert task_store.workspaces_are_held(stored.id) is False
    assert task_store.require(stored.id).metadata["cleanup_pending"] is False
    assert stored.id not in task_store.list_workspace_cleanup_pending()


def _request_payload(project_id: str) -> dict:
    return {
        "project_id": project_id,
        "operation": "completion",
        "instruction": "Update src/main.py",
        "target_files": ["src/main.py"],
    }


def _wait_for_terminal(client: TestClient, task_id: str) -> dict:
    for _ in range(100):
        task = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
        if task["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return task
        time.sleep(0.02)
    raise AssertionError("task did not finish")


def _wait_for_workspace_cleanup(task_root: Path) -> None:
    for _ in range(100):
        if not task_root.exists():
            return
        time.sleep(0.02)
    raise AssertionError("task workspace was not cleaned")


def _wait_for_cleanup_pending(task_store: TaskStore, expected: bool) -> None:
    for _ in range(100):
        task = task_store.list()[0]
        if task.metadata.get("cleanup_pending") is expected:
            return
        time.sleep(0.02)
    raise AssertionError("NaturalCC cleanup did not converge")


async def _wait_for_task_status(
    task_store: TaskStore,
    task_id,
    expected: TaskStatus,
    *,
    attempts: int = 100,
) -> None:
    for _ in range(attempts):
        if task_store.require(task_id).status == expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"task did not reach {expected}")


async def _wait_for_cleanup(task_store: TaskStore, task_id, *, expected_pending: bool) -> None:
    for _ in range(100):
        if task_store.require(task_id).metadata.get("cleanup_pending") is expected_pending:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("NaturalCC cleanup did not converge")


def _wait_for_adapter_progress(client: TestClient, task_id: str, progress: int) -> bool:
    for _ in range(100):
        events = client.get(f"/api/v1/tasks/{task_id}/logs").json()["data"]
        if any(
            event["stream"] == "code_generation.adapter" and event["progress"] == progress
            for event in events
        ):
            return True
        time.sleep(0.02)
    return False
