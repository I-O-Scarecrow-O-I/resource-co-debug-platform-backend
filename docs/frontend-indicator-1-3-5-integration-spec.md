# 指标 1、3、5 前端接入与验收规格

本文是当前智研前端接入平台后端与 NaturalCC 的实施规格，供前端开发和联调使用。范围仅包含指标 1、3、5 的功能页面与结果展示；接口字段以运行时 OpenAPI 和后端 Pydantic schema 为准。本文不承诺未实现的指标数值或 NaturalCC 算法效果。

## 1. 当前状态与结论

| 指标 | 当前页面/接口状态 | 现在可测内容 | 尚不能确认的内容 |
| --- | --- | --- | --- |
| 1：代码补全/续写及生成后自检 | `/coding` 已接入代码生成任务与自检结果；后端已提供统一任务 API。 | 浏览器可测代码提交、任务状态、生成产物、自检结果和 analyzer coverage。你本轮看到 `builtin · completed`、`cppcheck · unavailable`，证明自检已运行但 C/C++ 扫描覆盖不完整。 | 不能把 `cppcheck unavailable` 当扫描通过；当前自检不返回可核验的漏洞密度值，不能据页面上的 `≤ 2 个/百行` 目标提示判定该数值达标。 |
| 3：常见缺陷/漏洞检测 | 后端扫描 API 已支持 `scan_type=frequent_defects`；前端没有调用该接口的页面。 | 可通过后端 Swagger 手动创建 API 任务；接入新页面后可做浏览器端到端功能验收。 | 当前不能通过 `/defects` 页面验收真实后端扫描；分析器覆盖不完整时不能宣称扫描完整。 |
| 5：高风险漏洞检测 | 后端扫描 API 已支持 `scan_type=high_risk`；前端没有调用该接口的页面。 | 可通过后端 Swagger 手动创建 API 任务；接入新页面后可做浏览器端到端功能验收。 | 当前不能通过 `/defects` 页面验收真实后端扫描；API 返回发现列表本身不能证明合同阈值或检测效果达标。 |

上述指标编号及业务名称按当前协作接入范围理解。具体合同数值阈值须以合同原文及 NaturalCC 交付能力为准；后端现有请求 schema 没有通用的“指标达标”布尔字段。

## 2. 必须修正的前端接入差距

当前前端 `.env.development` 已将 `VITE_USE_MOCK=false`，但缺陷页 API 调用的是：

- `GET /defects`
- `GET /defects/{id}`
- `POST /defects/analyze`

后端并未提供以上路径。后端扫描任务入口是 `POST /api/v1/modules/vulnerability/tasks`。因此需完成以下修改：

1. 为漏洞扫描建立明确的页面入口。可以改造 `/defects`，也可以新增独立页面；页面必须明确支持指标 3 和指标 5 的扫描类型选择。
2. 将缺陷页 API 改为调用后端 vulnerability task API，不要再把示范数据、旧路径或 mock 响应显示为真实扫描结果。
3. 复用统一 HttpClient，读取平台 envelope 的 `success`、`data`、`message`、`timestamp`；显示 HTTP 422、任务失败和网络错误，不自动回退到 mock。
4. 使用项目 ID 和项目内相对文件路径创建扫描任务。用户可以从已有项目中选择，也可以上传 ZIP；不得把本机绝对路径传给后端。
5. 提交后保存返回的 `task_id`，轮询统一任务接口直至终态；需要日志时使用统一 REST/WS 日志接口。
6. 分开显示任务状态与扫描 coverage。空 findings 不等于扫描成功；`partial`、`unavailable`、`failed` 必须可见，不能被映射为“未发现漏洞”或绿色成功。
7. 对生成后自检保留 `/coding` 现有呈现，并区分“生成任务结果”和 `result.self_scan` 结果。扫描器失败不代表生成任务一定失败，反之也不能因生成成功而隐藏自检失败。

前端不得直接请求 NaturalCC 的 7860 端口、读取 NaturalCC API Key，或自行实现一套 NaturalCC 任务生命周期。浏览器只请求平台后端 `/api/v1`。

## 3. 页面与操作要求

### 3.1 指标 1：代码生成工作台

在 `/coding` 保持以下操作和展示：

- 展示 NaturalCC 连通状态与 capabilities；请求失败时显示明确错误。
- 支持 `completion`、`repair`、`refactor` 操作，输入目标文件相对路径、代码和指令。
- 点击生成后展示统一任务状态、进度、错误、实时日志、最终答复和可下载产物。
- 任务进入 `SUCCEEDED` 后显示 `result.self_scan`。至少展示扫描状态、各 analyzer 状态、coverage 详情、findings 数量与列表、报告文本和 error。
- 对 `completed`、`partial`、`failed`、`unavailable` 使用不同的状态表达；不得把“没有 finding”直接显示成“安全/达标”。
- 明确显示当前后端没有返回可核验的漏洞密度数值，页面中的合同目标只作为目标说明。

### 3.2 指标 3、5：漏洞扫描工作台

漏洞页至少需要以下输入：

- 项目（必填）；
- 扫描范围：指定目标文件 `targets` 或整个项目 `project`；
- 当范围为 `targets` 时，至少选择一个项目内相对文件路径；
- 指标 3 对应 `scan_type=frequent_defects`，指标 5 对应 `scan_type=high_risk`；
- 分析模式 `builtin` / `deep`；建议给出 `deep` 的 Cppcheck 前置条件说明；
- 严重级别阈值、最多发现数、超时时间；可选增量分析。

扫描结果至少需要展示：统一任务终态、错误、findings、coverage、报告文本及 analyzer 执行信息。每条 finding 应保留后端提供的规则、严重级别、文件相对路径、行号、说明等字段；不应在前端丢弃扫描器原始信息。

若从代码生成结果发起扫描，使用 `source_task_id` 指向**同项目且已成功**的代码生成任务，并传入相对于其任务 Workspace 的目标文件路径。不要把浏览器本地路径或后端绝对路径作为 `target_files`。

## 4. 后端接口契约

REST base URL 为 `/api/v1`。所有普通成功响应使用：

```json
{
  "success": true,
  "data": {},
  "message": "ok",
  "timestamp": "2026-09-26T00:00:00Z"
}
```

### 4.1 项目

| 用途 | 接口 |
| --- | --- |
| 查询项目列表 | `GET /api/v1/projects` |
| 上传 ZIP 创建项目 | `POST /api/v1/projects`，multipart 字段 `archive`，可选字段 `name` |
| 查询单个项目 | `GET /api/v1/projects/{project_id}` |

### 4.2 指标 1 代码生成

| 用途 | 接口 |
| --- | --- |
| NaturalCC 连通状态 | `GET /api/v1/modules/code-generation/health` |
| 可用操作声明 | `GET /api/v1/modules/code-generation/capabilities` |
| 创建任务 | `POST /api/v1/modules/code-generation/tasks` |

创建 body：

```json
{
  "project_id": "<project-uuid>",
  "operation": "completion",
  "instruction": "补全 add 函数，使其返回 a 与 b 的和。",
  "target_files": ["src/main.c"],
  "budget": {
    "max_tool_calls": 20
  },
  "timeout_seconds": 180
}
```

`operation` 取 `completion`、`repair`、`refactor`。`target_files` 必须是项目内相对路径，最多 50 个；请求未知字段会被拒绝。

### 4.3 指标 3、5 漏洞扫描

| 用途 | 接口 |
| --- | --- |
| 创建漏洞扫描任务 | `POST /api/v1/modules/vulnerability/tasks` |

指标 3 示例：

```json
{
  "project_id": "<project-uuid>",
  "scan_type": "frequent_defects",
  "mode": "deep",
  "scope": "targets",
  "target_files": ["src/main.c"],
  "incremental": false,
  "severity_threshold": "medium",
  "max_findings": 30,
  "timeout_seconds": 180
}
```

指标 5 使用相同 body 结构，将 `scan_type` 改为 `high_risk`。`scope` 为 `targets` 或 `project`；targets 范围必须给至少一个路径。`mode` 为 `builtin` 或 `deep`。其余约束以 Swagger/OpenAPI 为准。

### 4.4 统一任务查询、日志与产物

| 用途 | 接口 |
| --- | --- |
| 查询全部任务 | `GET /api/v1/tasks` |
| 查询任务状态与结果 | `GET /api/v1/tasks/{task_id}` |
| 查询任务日志 | `GET /api/v1/tasks/{task_id}/logs` |
| 订阅实时日志 | `WS /ws/v1/tasks/{task_id}/logs` |
| 查询产物清单 | `GET /api/v1/tasks/{task_id}/artifacts` |
| 下载产物 | `GET /api/v1/tasks/{task_id}/artifacts/{artifact_path}` |
| 取消任务 | `POST /api/v1/tasks/{task_id}/cancel` |

创建任务会立即返回任务对象，不能将 HTTP 200 当成业务完成。前端应按 `data.id` 查询任务，展示 `status`、`progress`、`error`、`result`，在 `SUCCEEDED`、`FAILED` 或 `CANCELLED` 等终态停止轮询。

### 4.5 关键结果字段

- 代码生成：`TaskResponse.result.final_answer`、`changed_files`、`self_scan`。
- 生成后自检：`self_scan.status`、`findings`、`coverage`、`report`、`error`。
- 独立扫描任务：`result.findings`、`coverage`、`report`、`execution`；同时检查外层任务 `status` 与 `error`。
- 产物下载是二进制响应，不是 JSON envelope；使用 Blob 读取。

## 5. 扫描模式与 coverage 判定

代码生成 C/C++ 自检需要 NaturalCC 服务进程可调用 Cppcheck。独立漏洞扫描选择 `deep` 时也依赖该分析器在 NaturalCC 进程的 `PATH` 中可用。

| analyzer coverage | 前端表达 | 验收含义 |
| --- | --- | --- |
| `builtin · completed` | 内置分析完成 | 仅表示 builtin 规则执行完成。 |
| `cppcheck · completed` | Cppcheck 完成 | Cppcheck 执行完成。 |
| `cppcheck · partial` | Cppcheck 部分覆盖 | 仅部分分析，不表示完整 C/C++ 覆盖。 |
| `cppcheck · unavailable` | Cppcheck 不可用 | 服务进程未能调用分析器；不得展示为扫描通过。 |

对于 `deep` 扫描，依据后端当前接口契约：builtin 必须为 `completed`，Cppcheck 至少要有 `completed` 或 `partial` coverage 才可接受为已执行；若 coverage 为 `partial`，页面仍须标记部分覆盖，不能写成完整扫描。若 Cppcheck 为 `unavailable`，当前扫描不应判定通过。调整 NaturalCC 服务 `PATH` 并重启服务后，再重建扫描任务；环境变量变更不会注入已运行进程。

## 6. 页面联调验收步骤

### 指标 1

1. 打开 `/coding`，确认生成服务状态 available。
2. 输入一段小型 C 源码与明确补全要求，提交 `completion`。
3. 确认任务创建并进入终态；成功时检查 `changed_files`、结果文件和最终答复。
4. 确认页面同时显示自检状态和 analyzer coverage。按当前已见状态 `builtin completed + cppcheck unavailable`，只能记录为“生成可测、自检不完整”，不能记录为“密度达标/扫描通过”。

### 指标 3

1. 在漏洞工作台选择项目及目标文件，选择“常见缺陷”并提交。
2. 确认实际请求为 `POST /api/v1/modules/vulnerability/tasks`，body 中 `scan_type=frequent_defects`。
3. 轮询任务直到终态；检查 findings、coverage、报告及错误。

### 指标 5

1. 选择相同或另一测试项目与目标文件，选择“高风险漏洞”并提交。
2. 确认 body 中 `scan_type=high_risk`。
3. 轮询任务直到终态；分别记录 finding 与 coverage。不要仅根据前端颜色或 finding 数为 0 判定合同指标达标。

前端漏洞工作台实现完成前，指标 3/5 的 API 可在 `http://127.0.0.1:8000/docs` 手动调用；此方式仅是接口联调，不是前端页面验收。创建扫描任务后必须继续查询任务详情，不能只看 Swagger 创建请求返回 HTTP 200。

## 7. 建议的最小浏览器验收表

| 场景 | 通过条件 | 不通过/未完成条件 |
| --- | --- | --- |
| 代码补全 | generation task 为 `SUCCEEDED`，目标产物可查看或下载。 | NaturalCC run 失败、超时或无目标产物。 |
| 生成后自检 | 自检状态与每个 analyzer coverage 均可见。 | 缺 coverage/error 被隐藏；扫描失败被显示为无漏洞。 |
| 指标 3 扫描 | 页面发出 `frequent_defects` 请求；任务终态、findings、coverage 可见。 | 页面仍只显示本地 mock，或请求 `/defects` 旧路径。 |
| 指标 5 扫描 | 页面发出 `high_risk` 请求；任务终态、findings、coverage 可见。 | 用指标 3 的结果冒充，或仅凭零发现宣称通过。 |
| C/C++ deep coverage | builtin 完成且 Cppcheck 状态可见；`partial` 明确标记部分覆盖。 | Cppcheck unavailable，或将 partial 显示为完整通过。 |

功能联调通过不等于合同阈值验收。要判定“≤ 2 个/百行”等数值指标，还需要 NaturalCC 返回稳定的漏洞数量/代码行数计算依据、覆盖范围和可复现的验收样例；当前后端/前端契约没有提供经校验的密度值字段。

## 8. 实施范围与变更边界

前端开发者需要修改前端仓库中的 defects/vulnerability 页面、API service、类型定义及必要的路由/项目选择交互；复用现有 HttpClient、task polling 和 logs service。此次规格不要求前端自行修改 NaturalCC、后端算法或读取服务端本地目录。

若联调中发现后端返回值或 NaturalCC 行为与以上契约不符，请记录 HTTP 请求路径、脱敏后的 body、任务 ID、终态、coverage 与错误，并先反馈接口事实；不要在前端写 fallback 去掩盖后端/上游失败。

## 9. 参考

- 后端 Swagger：`http://127.0.0.1:8000/docs`
- 后端运行时 OpenAPI：`http://127.0.0.1:8000/openapi.json`
- 后端前端接口总约定：[frontend-api.md](frontend-api.md)
- NaturalCC Cppcheck 与自检说明：[naturalcc-integration.md](naturalcc-integration.md)
