# Resource Co-Debug Platform Backend

FastAPI backend foundation for module 2 of the code generation and cross-platform debugging project.

## Stack Decision

- Main backend language: Python 3.11.x
- Web/API framework: FastAPI
- Runtime server: Uvicorn
- First-stage persistence: in-memory repositories and local workspaces
- Planned persistence: PostgreSQL after the API model stabilizes
- External execution: Make/GCC/GDB are launched by module A as controlled subprocesses
- Algorithm invocation: module A calls module C scheduling code as ordinary Python functions

This matches the project documents: the core backend work is process orchestration, log streaming,
Makefile/dependency analysis, GDB/MI control, and scheduler experiments.

## Module Scope

The repository starts with module A, the B/S backend foundation:

- A1 workspace management
- A2 task lifecycle management
- A3 REST API
- A4 logs, progress, status return, and WebSocket streaming
- A5 controlled execution environment

It also reserves clean extension points for:

- B: partitioned debugging and compilation adaptation
- C: multi-core multi-task scheduling optimization
- D: acceptance tests and performance verification

## Run Locally

```bash
O:\Code\resource-co-debug-platform-backend\scripts\run-dev.ps1
```

Default URL: `http://localhost:8000`

The reusable local environment is documented in `docs/environment.md`.

## Key APIs

- `GET /api/health`
- `POST /api/projects`
- `GET /api/projects`
- `GET /api/projects/{project_id}`
- `POST /api/tasks/build`
- `POST /api/tasks/debug`
- `POST /api/tasks/schedule-experiments`
- `GET /api/tasks`
- `GET /api/tasks/{task_id}`
- `GET /api/tasks/{task_id}/logs`
- `POST /api/tasks/{task_id}/cancel`
- `POST /api/dependencies/analyze`
- `GET /api/debug/sessions/{task_id}`
- `GET /api/metrics/build-success-rate`
- `GET /api/metrics/improvement-rate`
- `WS /ws/tasks/{task_id}/logs`

See `docs/collaboration-contract.md` for the frontend and module-C coordination contract.
