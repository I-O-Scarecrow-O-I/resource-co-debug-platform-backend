# Backend Collaboration Contract

## 1. Unified Python Version

Use Python 3.11.x for backend development and deployment.

The repository sets:

```toml
requires-python = ">=3.11,<3.12"
```

## 2. How Module A Calls Module C

Module A calls module C scheduling algorithms as ordinary Python functions.

Module A remains responsible for:

- REST API
- WebSocket log streaming
- task lifecycle
- cancellation state
- subprocess execution
- workspace isolation

Module C remains responsible for:

- scheduling plan calculation
- strategy comparison logic
- algorithm-specific logs and progress events through `TaskContext`

Real build/debug commands are still launched by module A as controlled subprocesses.

## 3. Task Input JSON

Build task:

```json
{
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

## 4. Task Result JSON

```json
{
  "id": "00000000-0000-0000-0000-000000000000",
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

## 5. How Module C Reports Logs, Progress, and Cancellation

Module C receives a `TaskContext` from module A.

```python
def plan_tasks(request, context):
    context.log("module C scheduling started")
    context.progress(20, "building ready queue")
    context.check_cancelled()
    context.progress(100, "schedule plan generated")
    return plan
```

Reporting rules:

- Logs: `context.log(message, stream="module_c")`
- Progress: `context.progress(percent, message)`
- Cancellation: module A owns cancellation state; module C calls `context.check_cancelled()`
- Frontend channel: frontend connects only to module A through REST and WebSocket
