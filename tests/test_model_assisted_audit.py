import asyncio
import io
import json
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import Settings, get_settings
from app.modules.code_generation.client import NaturalCCClient
from app.modules.code_generation.deps import get_code_generation_task_service
from app.modules.code_generation.service import NaturalCCService
from app.modules.code_generation.task_service import CodeGenerationTaskService
from app.modules.vulnerability.client import PipelineClient
from app.modules.vulnerability.deps import get_vulnerability_task_service
from app.modules.vulnerability.task_service import VulnerabilityTaskService
from app.platform.api.deps import get_log_service, get_task_service
from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType
from app.platform.services.log_service import TaskLogService
from app.platform.services.process_runner import ProcessRunner
from app.platform.services.task_service import TaskService
from app.platform.services.task_store import TaskStore
from app.platform.services.workspace_service import WorkspaceService


RUN = "/api/agent/runs/audit-1"
ENDPOINT = "/api/v1/modules/vulnerability/tasks"
REASONING = "vulnerability.model_reasoning"
ANSWER = "No supported defect observed."


class Upstream:
    def __init__(self):
        self.calls = []
        self.payload = None
        self.workspace_files = None
        self.running = threading.Event()
        self.final = {"status": "completed", "final_answer": ANSWER}
        self.block_seconds = 0
        self.events = []
        self.publish = threading.Event()
        self.publish.set()
        self.cancelled = False
        self.pipeline = []

    async def agent(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/api/agent/runs" and request.method == "POST":
            self.payload = json.loads(request.content)
            root = Path(self.payload["workspace"])
            self.workspace_files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
            return httpx.Response(200, json={"run_id": "audit-1"})
        if path == RUN + "/run":
            self.running.set()
            await asyncio.sleep(self.block_seconds)
            return httpx.Response(200, json=self.final)
        if path == RUN + "/events":
            # Deliberately replay a batch, even when the client advances its cursor.
            ready = self.running.is_set() and self.publish.is_set()
            return httpx.Response(200, json={"events": self.events if ready else []})
        if path == RUN + "/cancel":
            self.cancelled = True
            return httpx.Response(200, json={"status": "cancelled"})
        if path == RUN:
            return httpx.Response(200, json={"status": "cancelled" if self.cancelled else "running"})
        raise AssertionError(f"Unexpected Agent request: {path}")

    def scan(self, request):
        self.pipeline.append(json.loads(request.content))
        done = {"type": "done", "status": "success", "report": "static report", "artifacts": {
            "findings": [{"rule": "sample"}], "coverage": [{"engine": "builtin", "status": "completed"}],
        }}
        return httpx.Response(200, content=(json.dumps(done) + "\n").encode())


@pytest.fixture
def audit_api(tmp_path, monkeypatch):
    remote = Upstream()
    workspace = WorkspaceService(tmp_path / "workspaces")
    logs = TaskLogService(200, tmp_path / "logs.sqlite3")
    tasks = TaskService(workspace_service=workspace, task_store=TaskStore(tmp_path / "tasks.sqlite3"),
                        log_service=logs, process_runner=ProcessRunner(), default_timeout_seconds=5)
    pipeline = PipelineClient(base_url="http://pipeline.invalid", connect_timeout_seconds=1,
                              transport=httpx.MockTransport(remote.scan))
    agent = CodeGenerationTaskService(tasks, NaturalCCService(NaturalCCClient(
        base_url="http://agent.invalid", connect_timeout_seconds=1, request_timeout_seconds=5,
        transport=httpx.MockTransport(remote.agent),
    )), approve_execute=True, pipeline=pipeline)
    settings = Settings(naturalcc_model_audit_enabled=True)
    app = main.create_app()
    # The WebSocket handler accesses these factories directly, outside FastAPI Depends.
    monkeypatch.setattr(main, "get_task_service", lambda: tasks)
    monkeypatch.setattr(main, "get_log_service", lambda: logs)
    app.dependency_overrides.update({
        get_settings: lambda: settings,
        get_task_service: lambda: tasks,
        get_log_service: lambda: logs,
        get_code_generation_task_service: lambda: agent,
        get_vulnerability_task_service: lambda: VulnerabilityTaskService(tasks, pipeline),
    })
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("src/main.CPP", "int main() { return 0; }\n")
        z.writestr("src/unselected.h", "// not selected\n")
    archive.seek(0)
    project = asyncio.run(workspace.create_from_archive(UploadFile(file=archive, filename="project.zip")))
    client = TestClient(app)
    try:
        yield client, project, remote, tasks, agent, settings
    finally:
        client.close()
        asyncio.run(tasks.shutdown(grace_seconds=0))
        asyncio.run(agent.shutdown())
        tasks.close_resources_when_idle()


def submit(api, **overrides):
    client, project, *_ = api
    return client.post(ENDPOINT, json={
        "project_id": str(project.id), "scan_type": "frequent_defects", "scope": "targets",
        "target_files": ["src/main.CPP"], **overrides,
    })


def start(api, **overrides):
    response = submit(api, strategy="model_assisted", **overrides)
    assert response.status_code == 200, response.text
    task = response.json()["data"]
    assert (task["module"], task["task_type"]) == ("vulnerability", "VULNERABILITY_SCAN")
    return task["id"]


def observe(check):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.02)
    pytest.fail("Timed out waiting for task observation")


def status(client, task_id):
    return client.get(f"/api/v1/tasks/{task_id}").json()["data"]


def finish(client, task_id):
    def terminal():
        task = status(client, task_id)
        return task if task["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"} else None
    return observe(terminal)


def reasoning(client, task_id):
    logs = client.get(f"/api/v1/tasks/{task_id}/logs").json()["data"]
    assert all("PRIVATE TOOL OUTPUT" not in log["message"] for log in logs)
    return [log["message"] for log in logs if log["stream"] == REASONING]


def event(sequence, event_type, **payload):
    return {"sequence": sequence, "type": event_type, "payload": payload}


def test_reasoning_is_public_during_run_and_not_replayed(audit_api):
    client, project, remote, *_ = audit_api
    remote.block_seconds = 1.2
    remote.publish.clear()
    remote.events = [event(1, "model.reasoning.delta", text="Inspecting "),
                     event(2, "tool.finished", text="PRIVATE TOOL OUTPUT"),
                     event(3, "model.reasoning.delta", text=" selected file")]
    t0 = time.monotonic()
    task_id = start(audit_api)
    with client.websocket_connect(f"/ws/v1/tasks/{task_id}/logs") as ws:
        # Let the ASGI socket send a log before the mock upstream exposes any deltas.
        ws.receive_json()
        remote.publish.set()
        deltas = []
        for _ in range(8):
            log = ws.receive_json()
            assert "PRIVATE TOOL OUTPUT" not in log["message"]
            if log["stream"] == REASONING:
                if not deltas:
                    first_delta = time.monotonic() - t0
                    assert status(client, task_id)["status"] == "RUNNING"
                deltas.append(log["message"])
                if len(deltas) == 2:
                    break
            assert log["progress"] != 100, "Task finished before both live deltas arrived"
    assert deltas == ["Inspecting ", " selected file"]
    task = finish(client, task_id)
    final = time.monotonic() - t0
    print(f"Synthetic timing: first public delta {first_delta:.3f}s; terminal result {final:.3f}s")
    assert first_delta < final
    assert task["status"] == "SUCCEEDED"
    assert task["result"] == {"strategy": "model_assisted", "naturalcc_run_id": "audit-1", "final_answer": ANSWER}
    assert reasoning(client, task_id) == ["Inspecting ", " selected file"]
    assert remote.workspace_files == {"src/main.CPP"}
    assert remote.payload["target_files"] == ["src/main.CPP"]
    assert remote.payload["authorized_paths"] == []
    assert {"api_key", "runtime_model_config", "model"}.isdisjoint(remote.payload)
    assert not remote.pipeline
    observe(lambda: not (project.root_path / "tasks" / task_id).exists())
    assert client.get(f"/api/v1/tasks/{task_id}/artifacts").status_code == 400


@pytest.mark.parametrize("answer,expected", [(ANSWER, "SUCCEEDED"), (" \n", "FAILED"), ({"x": 1}, "FAILED")])
def test_model_completion_does_not_require_reasoning_and_requires_text(audit_api, answer, expected):
    client, _, remote, *_ = audit_api
    remote.final["final_answer"] = answer
    task_id = start(audit_api)
    task = finish(client, task_id)
    assert task["status"] == expected
    assert reasoning(client, task_id) == []
    if expected == "FAILED":
        assert task["error"] == "NaturalCC model-assisted audit returned no valid final answer"
        assert task["result"] == {}


@pytest.mark.parametrize("approval_in_events", [False, True])
def test_pending_approval_cancels_without_approving(audit_api, approval_in_events):
    client, project, remote, *_ = audit_api
    if approval_in_events:
        remote.block_seconds = 2
        remote.events = [event(1, "approval.requested", risk="write", text="PRIVATE TOOL OUTPUT")]
    else:
        remote.final = {"status": "waiting_approval", "details": "PRIVATE TOOL OUTPUT"}
    task_id = start(audit_api)
    task = finish(client, task_id)
    assert task["status"] == "FAILED"
    assert task["error"] == "NaturalCC model-assisted audit stopped for a pending approval"
    assert remote.cancelled
    assert not any(path.endswith(("/approve", "/reject")) for _, path in remote.calls)
    assert remote.calls.count(("POST", RUN + "/run")) == 1
    observe(lambda: not (project.root_path / "tasks" / task_id).exists())


@pytest.mark.parametrize("strategy", [{}, {"strategy": "static"}])
def test_static_still_uses_pipeline(audit_api, strategy):
    client, _, remote, *_ = audit_api
    response = submit(audit_api, mode="builtin", **strategy)
    assert response.status_code == 200
    task = finish(client, response.json()["data"]["id"])
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["findings"] == [{"rule": "sample"}]
    assert not remote.calls


@pytest.mark.parametrize("overrides", [
    {"scan_type": "high_risk"}, {"scope": "project"}, {"incremental": True},
    {"ground_truth_file": "truth.json"}, {"sanitizer_report": "report.txt"},
    {"mode": "builtin"}, {"target_files": []}, {"target_files": ["../other.c"]},
    {"target_files": ["main.py"]}, {"model": "external"}, {"instruction": "unexpected"},
    {"authorized_paths": ["../"]},
])
def test_model_input_contract_rejects_incompatible_requests(audit_api, overrides):
    assert submit(audit_api, strategy="model_assisted", **overrides).status_code == 422
    assert not audit_api[2].calls


def test_model_audit_is_disabled_without_operator_opt_in(audit_api):
    audit_api[-1].naturalcc_model_audit_enabled = False
    assert submit(audit_api, strategy="model_assisted").status_code == 503
    assert not audit_api[2].calls


def test_startup_recovers_only_the_held_agent_audit(audit_api):
    _, project, remote, tasks, agent, _ = audit_api
    audit = tasks._new_task(module=BackendModuleName.VULNERABILITY, project_id=project.id,
                            task_type=TaskType.VULNERABILITY_SCAN, command=["naturalcc"],
                            metadata={"strategy": "model_assisted", "naturalcc_run_id": "audit-1"})
    audit.status = TaskStatus.RUNNING
    tasks.task_store.save(audit)
    workspace = tasks.workspace_service.create_task_workspace(project.id, audit.id)
    tasks.hold_task_workspaces(audit.id, {"cleanup_pending": True})

    async def recover():
        await tasks.startup()
        assert workspace.exists()
        await agent.startup()

    asyncio.run(recover())
    assert remote.cancelled
    assert not workspace.exists()
    assert tasks.require_task(audit.id).metadata["cleanup_pending"] is False
