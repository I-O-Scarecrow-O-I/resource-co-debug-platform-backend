# Backend Collaboration Contract

## 1. Unified Python Version

Use Python 3.11.x for backend development and deployment.

The repository sets:

```toml
requires-python = ">=3.11,<3.12"
```

Windows local runtime:

```text
O:\Code_dependency\python_runtimes\python-3.11.9
```

Project virtual environment:

```text
O:\Code_dependency\python_envs\resource-co-debug-py311
```

## 2. Backend Module Integration Standard

This repository is the main backend framework. Other contract backend modules should integrate as
Python packages under `app/modules`.

Required module shape:

```text
app/modules/<module_name>/
  provider.py
  routes.py
  schemas.py
  service.py
```

Each module exposes a provider returning:

```python
BackendModule(
    name="<module_name>",
    route_prefix="/modules/<route-name>",
    router=router,
)
```

The platform mounts module routers under:

```text
/api/v1/modules/...
```

The current module registry can be queried at:

```text
GET /api/v1/modules
```

## 3. Platform Responsibility

The platform owns:

- REST API versioning
- WebSocket log streaming
- project/workspace management
- unified task lifecycle
- cancellation state
- subprocess execution
- shared logs and progress events
- future artifact/report persistence

Backend modules own only their business algorithms and module-specific APIs.

## 4. Module 2 Scheduler Invocation

The platform calls `co_debug.scheduler` as ordinary Python functions.

Real build/debug commands are launched by the platform as controlled subprocesses.

## 5. Task Input JSON

Build task:

```json
{
  "module": "co_debug",
  "project_id": "00000000-0000-0000-0000-000000000000",
  "command": ["make"],
  "work_dir": ".",
  "timeout_seconds": 300,
  "metadata": {}
}
```

Debug task:

```json
{
  "module": "co_debug",
  "project_id": "00000000-0000-0000-0000-000000000000",
  "executable_path": "build/app",
  "args": [],
  "work_dir": ".",
  "timeout_seconds": 300,
  "metadata": {}
}
```

Schedule experiment:

```json
{
  "module": "co_debug",
  "project_id": "00000000-0000-0000-0000-000000000000",
  "strategy": "RESOURCE_AWARE",
  "tasks": [
    {
      "name": "case-a",
      "command": ["make", "run-a"],
      "estimated_ms": 12000,
      "depends_on": [],
      "preferred_core": null,
      "metadata": {}
    }
  ],
  "timeout_seconds": 300,
  "metadata": {}
}
```

## 6. Task Result JSON

```json
{
  "id": "00000000-0000-0000-0000-000000000000",
  "module": "co_debug",
  "project_id": "00000000-0000-0000-0000-000000000000",
  "task_type": "BUILD",
  "status": "SUCCEEDED",
  "command": ["make"],
  "created_at": "2026-08-31T00:00:00Z",
  "started_at": "2026-08-31T00:00:01Z",
  "finished_at": "2026-08-31T00:00:05Z",
  "exit_code": 0,
  "elapsed_ms": 4000,
  "progress": 100,
  "result": {
    "build_success": true
  },
  "error": null,
  "metadata": {}
}
```

## 7. Logs, Progress, and Cancellation

Module 2 scheduler functions receive a `TaskContext` from the platform.

```python
def plan_tasks(strategy, tasks, context):
    context.log("co_debug scheduling started")
    context.progress(20, "building ready queue")
    context.check_cancelled()
    context.progress(100, "schedule plan generated")
    return plan
```

Reporting rules:

- Logs: `context.log(message, stream="co_debug.scheduler")`
- Progress: `context.progress(percent, message)`
- Cancellation: the platform owns cancellation state; modules call `context.check_cancelled()`
- Frontend channel: frontend connects only to the platform through REST and WebSocket
- WebSocket log endpoint: `/ws/v1/tasks/{task_id}/logs`
