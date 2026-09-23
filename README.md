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

## Frontend API Contract

The frontend handoff is [docs/frontend-api.md](docs/frontend-api.md). The generated static schema is
[docs/openapi.json](docs/openapi.json); FastAPI also serves the current runtime schema at
`/openapi.json` and interactive documentation at `/docs` and `/redoc`.

Regenerate the checked-in schema after an API change, then verify it before committing:

```bash
python scripts/export_openapi.py
python scripts/export_openapi.py --check
```

On the current Windows shared environment, the equivalent interpreter is
`O:\Code_dependency\python_envs\resource-co-debug-py311\Scripts\python.exe`.

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

## NaturalCC 接入

NaturalCC 是独立的 Python 3.12 服务，必须使用 `O:\Code\naturalcc-cs` 中不早于
`ncc3@c619262` 的 Agent API；这是 deferred approval 协议的硬性最低版本，后端不提供 legacy
fallback，且不修改 NaturalCC。先在 NaturalCC 服务进程环境中设置模型密钥（本后端请求、`.env`
和示例均不接收或保存密钥），然后启动服务：

```powershell
$env:LIBCLANG_PATH = 'O:\Code_dependency\tools\libclang-18.1.1\clang\native\libclang.dll'
Set-Location O:\Code\naturalcc-cs
& O:\Code_dependency\python_envs\naturalcc-code-agent-py312\Scripts\python.exe -m code_agent.agent_web_api --host 127.0.0.1 --port 7860
```

后端 `.env` 只配置 `NATURALCC_BASE_URL`、超时和 `NATURALCC_APPROVE_EXECUTE=false`。默认仅
批准 `write`；仅在 NaturalCC 运行于隔离容器或受限操作系统账号时，才可显式设为 `true` 批准
`execute`。write 和 execute 是整个 run 的风险授权；首次待批的 `tool_call_id` 仅用于拒绝陈旧审批，
不表示同一风险的后续工具调用会逐个重新审批。因此 `execute=true` 会放宽该 run 余下的 execute 操作。
后端健康检查为 `GET http://127.0.0.1:8000/api/v1/health`；NaturalCC 自身健康检查为
`GET http://127.0.0.1:7860/api/health`。`GET /api/v1/modules/code-generation/health` 仅检查后端与
NaturalCC 的连通性；上游没有版本、工具或 capabilities 查询 API，不能将 health 当作协议兼容性门禁。
协议不匹配会在任务运行时 fail-closed：后端取消远端 run 并将任务标记为失败。

`waiting_approval` 时，后端只会按当前 `pending_approval.tool_call.id` 批准首个待批风险，并在批准后
再次调用 `/run`；`code_completion` 属于 write 工具，`vulnerability_detection` 只读扫描无需审批，
其 `.analyze`/`.fix` 变体属于 execute，只有显式开启上述配置才会批准。

最低版本基线已自带且由 Git 跟踪 `tokenizer.json` 与 `tokenizer_config.json`；模型权重尚未下载。
当前 Windows 环境可用上述会话级 `LIBCLANG_PATH` 加载 libclang 18.1.1；最新 NaturalCC 的 CParser
已成功解析 `test/test-skills/vuln.c`（`parsed=True`、5 个 keys）。这只证明本地库加载和样例解析，
不等同于 Agent 或模型端到端验收。
本后端只承担集成和任务生命周期。算法质量及“漏洞不超过 2 个/100 行”由模块一交付方验收，当前
没有由后端保证该指标的稳定机器契约。详见 `docs/naturalcc-integration.md`。

See `docs/collaboration-contract.md` for the frontend and module-C coordination contract.

C模块的人工触发方式、自动对比流程和合同指标判断见
`docs/C模块调度与对比实验使用说明.md`。

C模块内部开发负载和本地演示方法见
`docs/C模块内部调度负载与演示说明.md`。

一个ZIP包含三套代码时，批处理调试对比的配置和前端请求见
`docs/C模块批处理调试输入说明.md`。

A、B、前端和测试负责人接入C模块时，请先阅读
`docs/C模块联调接入说明.md`并完成文末确认表。
