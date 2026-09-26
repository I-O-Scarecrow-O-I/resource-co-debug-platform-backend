# 前端接口交付

本文面向 `O:\Code\zhiyan-frontend` 的模块二前端接入。请求与字段类型以
[`openapi.json`](openapi.json) 为准；它由后端运行时生成，不应手写或从本文反推 schema。

## 入口与更新

- 运行中的后端提供 Swagger UI：`/docs`，ReDoc：`/redoc`，运行时 schema：`/openapi.json`。
- 仓库内的静态快照为 [`openapi.json`](openapi.json)，便于前端在未启动后端时查看或生成类型。
- REST base URL 是 `/api/v1`，例如 `GET /api/v1/health`。WebSocket 不在此 base URL 下。
- 后端当前没有认证要求；前端不得把 Bearer token、登录跳转或 `401/403` 视为该接口的前置条件。

本地没有 Vite proxy 时，在 `O:\Code\zhiyan-frontend\.env.development.local` 写入：

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1
VITE_USE_MOCK=false
```

生产环境若有同源反向代理，`VITE_API_BASE_URL` 可为 `/api/v1`。WebSocket 连接使用后端 origin：
本地为 `ws://127.0.0.1:8000/ws/v1/tasks/{task_id}/logs`；HTTPS 生产站点为
`wss://<backend-origin>/ws/v1/tasks/{task_id}/logs`。

后端 API 变更后，在仓库根目录执行：

```bash
python scripts/export_openapi.py
python scripts/export_openapi.py --check
```

首个命令写入 UTF-8、排序键、缩进且带尾换行的快照；第二个命令在文件缺失或与
`app.main.create_app().openapi()` 不同时以非零状态退出。

当前 Windows 共享解释器可替代 `python`：

```powershell
& O:\Code_dependency\python_envs\resource-co-debug-py311\Scripts\python.exe scripts/export_openapi.py --check
```

## 响应、错误与传输

除下载 artifact 外，成功业务响应使用如下 envelope，而不是 `code`：

```json
{
  "success": true,
  "data": {},
  "message": "ok",
  "timestamp": "2026-09-19T00:00:00Z"
}
```

前端应读取 `data`，并以 `success` 与 HTTP 状态共同处理结果。参数或 body 校验失败的
HTTP 422 是 FastAPI 标准错误，响应可能是 `{ "detail": [...] }`，不保证套用上述 envelope。
业务抛出的 `AppError` 的 HTTP 400/404 使用 `ApiResponse.failed` envelope（`success: false`、
`data: null`、`message`、`timestamp`）；项目上传大小超限的 HTTP 413 也使用该 envelope。未知的
服务端异常不在此统一承诺内，不能假设所有异常都是 envelope。

`POST /api/v1/projects` 使用 `multipart/form-data`：文件字段名是 `archive`，内容为 ZIP；可选
文本字段为 `name`。发送 `FormData` 时不要手动设置 `Content-Type`，让浏览器加入 boundary。

`DELETE /api/v1/modules/co-debug/debug/sessions/{task_id}/breakpoints/{breakpoint_number}`
用于删除断点，成功仍返回该 envelope。`GET /api/v1/tasks/{task_id}/artifacts/{artifact_path}`
返回 `application/octet-stream` 二进制内容而非 JSON；以 `fetch` 读取 `response.blob()`，并从
`Content-Disposition` 处理下载文件名。前端类型生成器应将其识别为 Blob。

日志实时流地址由后端 origin 加 `/ws/v1/tasks/{task_id}/logs` 构成，不得把 `/api/v1` 拼入
WebSocket URL。服务端建连后会先发送保留的历史 `LogEvent`，再发送实时事件，且消息不套 REST
envelope。前端可二选一：只使用 WebSocket 的历史加实时流；或先调用
`GET /api/v1/tasks/{task_id}/logs` 再连接 WebSocket，但必须以 `sequence` 去重，不能无条件追加。

创建任务会立即返回 `TaskResponse`。前端应轮询 `GET /api/v1/tasks/{task_id}` 并显示其中的
`status`、`progress`（0–100）、`error`、`result`，在终态停止轮询；日志 WebSocket 用于增量展示，
不替代任务状态查询或最终结果读取。

### 漏洞扫描与代码生成自检

通过 `POST /api/v1/modules/vulnerability/tasks` 创建扫描任务，JSON body 及字段约束以
[`openapi.json`](openapi.json) 中的 `VulnerabilityTaskRequest` 为准。必填字段为：

| 字段 | 取值与要求 |
| --- | --- |
| `project_id` | 项目 UUID。 |
| `scan_type` | `frequent_defects` 或 `high_risk`；分别创建漏洞扫描或风险检查任务。 |
| `scope` | `targets` 或 `project`。使用 `targets` 时必须提供至少一个 `target_files`。 |

可选字段：`source_task_id`（同项目已成功任务的 UUID；见下文）、`mode`（`builtin` 或 `deep`，默认
`deep`）、`target_files`（相对路径，最多 50 项，默认空列表）、`sanitizer_report`（工作区内的
相对日志路径）、`incremental`（默认 `false`）、`severity_threshold`（`low`/`medium`/`high`/
`critical`，默认 `medium`）、`max_findings`（1–100，默认 30）、`timeout_seconds`（1–300 秒，默认由
服务配置决定）。未知字段会得到 422。所有文件路径都必须是工作区内的相对路径，不能使用绝对路径或
`..`；`scope=project` 可不传 `target_files`。

```json
{
  "project_id": "<project-uuid>",
  "scan_type": "frequent_defects",
  "mode": "deep",
  "scope": "targets",
  "target_files": ["src/main.c"],
  "incremental": true
}
```

创建响应仍是通用成功 envelope，`data` 是任务对象。拿到 `data.id` 后轮询
`GET /api/v1/tasks/{task_id}`；终态任务的 `result.findings` 是发现列表、`result.coverage` 是各分析器的
覆盖状态、`result.report` 是报告文本，`result.execution` 记录 feature/analyzer/执行状态。应同时检查
任务 `status` 与 coverage：`deep` 要求 builtin 为 `completed`，且 Cppcheck 至少有 `completed` 或
`partial` coverage 才能通过；`partial` 明确表示部分覆盖，不代表完整 C/C++ 扫描。分析器不可用或未达到要求时任务会失败，
coverage 仍可能包含诊断信息。

若扫描代码生成结果，传 `source_task_id` 为同一项目中状态为 `SUCCEEDED` 的代码生成任务 ID，
`target_files` 写生成文件相对于该任务 workspace 的路径。后端从该任务 workspace 建立扫描副本，原项目
不会被生成文件覆盖；扫描成功后仍通过扫描任务的 `result` 读取发现和覆盖信息。`source_task_id` 不可
引用其他项目或未成功的任务。

代码生成任务会在生成后，对请求目标及 Agent 报告的实际变更源码在临时副本中执行 `result.self_scan`；
扫描器只接触该副本，原生成产物保持不变。按逻辑结果理解为完整、部分、未完成三类：
`completed` 表示扫描流程成功完成，且配置的分析器按其报告的覆盖范围运行；这不表示代码无漏洞、合同要求
已全覆盖或完成正式验收。`partial` 为部分覆盖，`failed` 或 `unavailable` 为未完成；原始 `status` 会保留这些
值（例如扫描器未配置或没有剩余时间时为 `unavailable`）。对象成功时含 `findings`、`coverage`、
`report`；失败时含 `error`，coverage 若已知也会返回。C/C++ 目标使用服务端 Cppcheck，其他目标使用
builtin。这个自检固定 `auto_fix=false`，仅分析生成结果，不会改写文件，也不需要向扫描 API 提供模型
API key。自检未完成不会自动把已成功的代码生成任务改成失败；前端应单独呈现 `self_scan.status`。

ThreadSanitizer (TSan) 日志通过可选 `sanitizer_report` 传入。先通过项目 ZIP 上传，或使用
`source_task_id` 指向已保留产物的成功任务，将日志放入受控 workspace，再传相对路径，例如
`"sanitizer_report": "artifacts/tsan.log"`。前端不能直接向服务端 workspace 写文件或提交服务端绝对路径；
后端只接受受控 workspace 内已存在的相对文件。

`ProjectResponse` 中的 `root_path` 与 `source_path` 是服务端工作区绝对路径，仅作显示/诊断信息。
前端不能将其当成本机文件路径、URL 或向后端提交的可访问路径。

## A/B/C 路由分组

所有路由、参数、状态码与 request/response schema 以 [`openapi.json`](openapi.json) 为准。下表按用途
列出当前 41 个 HTTP path 的路由族；同一 path 的多个方法以 schema 为准。

| 分组 | 用途 | 路由 |
| --- | --- | --- |
| 模块二 A：平台 | 健康、项目、模块清单、任务查询/日志/产物/取消与日志 WS | `GET /health`、`GET /modules`、`POST/GET /projects`、`GET /projects/{project_id}`、`GET /tasks`、`GET /tasks/{task_id}`、`GET /tasks/{task_id}/logs`、`GET /tasks/{task_id}/artifacts`、`GET /tasks/{task_id}/artifacts/{artifact_path}`、`POST /tasks/{task_id}/cancel`；另有 `WS /ws/v1/tasks/{task_id}/logs` |
| 模块二 B：构建与调试 | 构建、调试、依赖分析/修复和完整调试会话 | `POST /tasks/build`、`POST /tasks/debug`、`POST /modules/co-debug/dependencies/analyze`、`repair`、`repair-build`、`POST /modules/co-debug/debug/sessions`、`GET /modules/co-debug/debug/sessions/{task_id}`、`GET /modules/co-debug/debug/sessions/{task_id}/state`、`GET /modules/co-debug/debug/sessions/{task_id}/stack-frames`、`POST /modules/co-debug/debug/sessions/{task_id}/arguments`、`breakpoints`、`run`、`continue`、`next`、`step`、`interrupt`、`wait`、`evaluate`、`close`、`DELETE /modules/co-debug/debug/sessions/{task_id}/breakpoints/{breakpoint_number}` |
| 模块二 C：调度与指标 | 调度实验、调度对比、批处理调试对比与指标计算 | `POST /tasks/schedule-experiments`、`POST /tasks/schedule-comparisons`、`POST /modules/co-debug/debug/comparisons`、`GET /modules/co-debug/metrics/build-success-rate`、`GET /modules/co-debug/metrics/improvement-rate`、`POST /modules/co-debug/metrics/debug-comparison-summary` |
| 模块一接入：代码生成 | NaturalCC 连通状态、能力声明和统一任务生命周期 | `GET /modules/code-generation/health`、`GET /modules/code-generation/capabilities`、`POST /modules/code-generation/tasks` |
| 模块一接入：漏洞扫描 | 对工程或生成产物发起扫描任务 | `POST /modules/vulnerability/tasks` |

表中的 HTTP 路径均相对于 `/api/v1`，共 41 个；例如模块一健康请求是
`GET /api/v1/modules/code-generation/health`。模块二 B 行中未重复完整前缀的依赖动作均为
`/modules/co-debug/dependencies/{action}`，调试动作均为
`/modules/co-debug/debug/sessions/{task_id}/{action}`。

## `zhiyan-frontend` 接入点（说明，不在本仓库修改）

| 文件 | 需要接入的调用要点 |
| --- | --- |
| `src/app/config/env.ts` 与 `.env.development.local` | 默认 `VITE_API_BASE_URL` 从 `/api` 调整为 `/api/v1`；无 Vite proxy 的本地环境按本文配置为 `http://127.0.0.1:8000/api/v1` 并关闭 mock。 |
| `src/services/http/client.ts`、`src/types/api.ts` | 将现有 `code === 0` 解包替换为 `success/data/message/timestamp`；为 multipart、`DELETE` 和 artifact `Blob` 提供不强制 JSON 的请求分支。 |
| `src/services/http/interceptors.ts`、`src/app/store/workspace.ts` | 当前后端无认证；不要将 `token`/Bearer header、401/403 登录处理作为调用依赖。 |
| `src/modules/overview/pages/OverviewPage.vue` | 接入 A 的 `/health`、`/modules` 及项目/任务概览。 |
| `src/modules/coding/pages/CodingPage.vue` | 接入模块一的 health、capabilities 和 `POST /modules/code-generation/tasks`，随后按任务 ID 轮询。 |
| `src/modules/debugging/pages/DebuggingPage.vue` | 接入 A 的项目上传/任务日志/WebSocket，以及 B 的 build、debug、依赖、调试会话和断点 API。 |
| `src/modules/scheduler/pages/SchedulerPage.vue` | 接入 C 的 `schedule-experiments`、`schedule-comparisons`、metrics 与任务进度轮询。 |

页面目前仍是布局入口；建议在各业务模块新增各自 API/composable 后再绑定 UI，避免把 A/B/C 的
请求细节堆入页面组件。

## 批处理调试对比输入

前端要从已上传的工程包中按预设调试作业发起指标（4）对比时，使用
`POST /api/v1/modules/co-debug/debug/comparisons`。ZIP根目录的`debug-workloads.json`由测试用例维护者填写，描述每组的可执行程序、断点和参数；请求只需`project_id`，可选成功构建任务的`build_task_id`、`core_ids`和超时。后端生成完整GDB批处理命令并沿用C的FIFO/优化双跑。完整格式与限制见[`C模块批处理调试输入说明.md`](C模块批处理调试输入说明.md)。

## 历史调试对比指标汇总

`POST /api/v1/modules/co-debug/metrics/debug-comparison-summary`用于汇总已经完成的批处理调试对比。前端只提交历史`SCHEDULE_COMPARISON`任务ID：

```json
{
  "comparison_task_ids": [
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222"
  ]
}
```

后端从持久化Task记录读取并校验任务类型、来源、成功状态和`ScheduleComparisonSummary`结果，再按每个Task一个实验样本计算平均提升率。请求不接受前端自行计算的`improvement_rate`、FIFO耗时或优化耗时。

接口支持一个或多个样本，不要求必须为3个，也不设置固定的3样本上限。正式合同测试仍可由UI选择指定3套代码对应的历史结果。重复Task ID、普通`/tasks/schedule-comparisons`任务、未成功任务和结果结构不完整的任务会被拒绝。

响应包含`sample_count`、按请求顺序返回的`comparison_results`、整体任务与时长差异资格、平均提升率、当前要求的提升率阈值及是否达标。新创建的DebugComparison Task会在metadata中记录`comparison_kind`、manifest路径和可为空的`build_task_id`；改动前仅含`debug_workload_manifest`的历史DebugComparison也可用于汇总。
