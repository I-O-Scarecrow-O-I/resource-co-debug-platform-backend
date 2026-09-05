# Resource Co-Debug Platform Backend

FastAPI main backend foundation for the code generation and cross-platform debugging contract project.

## Stack Decision

- Main backend language: Python 3.11.x
- Web/API framework: FastAPI
- Runtime server: Uvicorn
- Persistence: projects and workspaces use the local filesystem; task records and bounded log history use SQLite
- Scale-out evolution: PostgreSQL and pub/sub are optional when multi-worker or multi-node deployment is needed
- External execution: Make/GCC/GDB are launched by module A as controlled subprocesses
- Module integration: contract backend modules are registered under `/api/v1/modules/...`
- Algorithm invocation: the platform calls `co_debug.scheduler` as ordinary Python functions

This matches the project documents: the backend needs one main platform for process orchestration,
log streaming, task status, artifacts, and module integration. Module 2 currently provides the
implemented backend business capability.

## Module Scope

The repository is organized as a main backend platform plus contract backend modules:

- `app/platform`: shared backend foundation
- `app/modules/co_debug`: module 2, resource-coordinated debugging and optimization

`app/platform/services` holds shared execution infrastructure. Module 2 business services and
module-specific schemas live under `app/modules/co_debug/services` and
`app/modules/co_debug/schemas`; `app/platform/api/deps.py` composes them with platform services.

The shared platform currently provides:

- A1 workspace management
- A2 task lifecycle management
- A3 REST API
- A4 logs, progress, status return, and WebSocket streaming
- A5 controlled execution environment

Module 2 reserves clean extension points for partitioned compilation adaptation, GDB/MI debugging,
multi-core scheduling, and acceptance metrics.

## Run Locally

```bash
O:\Code\resource-co-debug-platform-backend\scripts\run-dev.ps1
```

Default URL: `http://localhost:8000`

The reusable local environment is documented in `docs/environment.md`.

## Log Streaming Deployment Constraint

SQLite-backed log history can be read across instances, but WebSocket real-time fan-out is
process-local. Run the current baseline with a single worker; multi-worker or multi-node
deployments require a pub/sub backend.

## Key APIs

- `GET /api/v1/health`
- `POST /api/v1/projects`
- `GET /api/v1/projects`
- `GET /api/v1/projects/{project_id}`
- `POST /api/v1/tasks/build`
- `POST /api/v1/tasks/debug`
- `POST /api/v1/tasks/schedule-experiments`
- `POST /api/v1/tasks/schedule-comparisons`
- `GET /api/v1/tasks`
- `GET /api/v1/tasks/{task_id}`
- `GET /api/v1/tasks/{task_id}/logs`
- `GET /api/v1/tasks/{task_id}/artifacts`
- `GET /api/v1/tasks/{task_id}/artifacts/{artifact_path}`
- `POST /api/v1/tasks/{task_id}/cancel`
- `POST /api/v1/modules/co-debug/dependencies/analyze`
- `GET /api/v1/modules/co-debug/debug/sessions/{task_id}`
- `GET /api/v1/modules/co-debug/metrics/build-success-rate`
- `GET /api/v1/modules/co-debug/metrics/improvement-rate`
- `GET /api/v1/modules`
- `WS /ws/v1/tasks/{task_id}/logs`

See `docs/collaboration-contract.md` for the frontend and module-C coordination contract.

C模块的人工触发方式、自动对比流程和合同指标判断见
`docs/C模块调度与对比实验使用说明.md`。

C模块内部开发负载和本地演示方法见
`docs/C模块内部调度负载与演示说明.md`。

A、B、前端和测试负责人接入C模块时，请先阅读
`docs/C模块联调接入说明.md`并完成文末确认表。

