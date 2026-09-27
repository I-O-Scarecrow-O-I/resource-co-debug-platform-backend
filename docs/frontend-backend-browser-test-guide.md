# 前后端浏览器联调操作指南

本指南面向当前本机已启动的三个服务：智研前端、平台后端和 NaturalCC。目标是用浏览器完成小范围功能联调，确认页面请求经过后端到达 NaturalCC，并能看到统一任务状态、代码产物及生成后自检结果。

## 1. 当前可以验收什么

| 测试目标 | 浏览器入口 | 当前联调方式 | 结果边界 |
| --- | --- | --- | --- |
| 代码补全、修复、重构任务 | 前端 `/coding` | 前端 → 后端 code-generation API → NaturalCC Agent API | 可检查任务生命周期、产物和自检状态；模型输出质量需要人工判断。 |
| 后端漏洞扫描任务 | 后端 Swagger `/docs` | Swagger → 后端 vulnerability API → NaturalCC Pipeline | 当前前端 `/defects` 页面没有对接这个后端 API。 |
| 前端 `/defects` 示范列表 | 前端 `/defects` | 当前开发配置关闭 mock，页面会请求 `/defects` 前缀 | 后端当前没有这些 `/defects` 路由；该页面不能作为漏洞扫描联调结果。 |

前端编码页展示的“漏洞密度目标 ≤ 2 个/百行”是目标提示，不是实测指标。页面会明确提示当前任务没有可核验的漏洞密度值；不能仅凭任务成功或扫描完成认定合同指标已经达标。

## 2. 浏览器与服务地址

三个终端保持运行，在浏览器中使用：

- 前端：`http://127.0.0.1:5173/coding`
- 后端接口文档：`http://127.0.0.1:8000/docs`
- 后端健康状态：`http://127.0.0.1:8000/api/v1/health`
- NaturalCC 健康状态：`http://127.0.0.1:7860/api/health`

建议用 `127.0.0.1` 访问前端。`localhost` 与 `127.0.0.1` 在浏览器看来是不同来源；若前端地址改变，后端 CORS 配置也要包含新的来源。

## 3. 先做服务连通性检查

### 3.1 确认前端能打开

访问 `http://127.0.0.1:5173/coding`。页面标题应为“智能编码工作台”。页头状态应显示“生成服务在线”。如果显示“服务状态待确认”，点“刷新状态”并观察错误提示。

### 3.2 确认后端和 NaturalCC

在浏览器地址栏分别打开：

1. `http://127.0.0.1:8000/api/v1/health`：后端应返回成功响应。
2. `http://127.0.0.1:7860/api/health`：NaturalCC 应返回 `status: ok`。
3. 回到 `/coding` 点“刷新状态”：页头“生成服务在线”说明后端 code-generation health 调用可以访问 NaturalCC。

也可以在 Swagger 中打开 `GET /api/v1/modules/code-generation/health` 并点“Try it out”→“Execute”。成功响应的 `data.status` 应为 `available`。这只证明服务可达，不代表模型 Key、工具或生成链路均可用。

## 4. 在前端跑一次最小代码补全

这个页面不要求你先手工创建 ZIP 项目：点击“生成代码”时，前端会把当前编辑器文件打包上传为服务端项目，再用返回的项目 ID 创建代码生成任务。

### 4.1 填写测试代码

在 `/coding` 页面：

1. 文件名保留 `main.c`。
2. 在左侧“原始代码”编辑器输入：

   ```c
   int add(int a, int b) {
   }
   ```

3. 操作模式选“代码补全 / 续写”。
4. 在“生成要求”填写：`补全 add 函数，使它返回 a 与 b 的和。只修改这个函数。`
5. 点击“生成代码”。

### 4.2 观察任务过程

右侧“任务结果”区域会显示任务状态和进度；展开“执行日志”可看实时日志。任务可能需要等待模型调用完成。终态包括“生成完成”“生成失败”或“已取消”。运行中可点“取消任务”，观察状态是否转为已取消。

成功时检查以下内容：

- 状态显示“生成完成”；
- “生成结果”标签出现，内容包含 `return a + b;` 或等价实现；
- 产物列表出现目标文件，可以点“查看”或“下载”；
- “安全自检”显示状态、发现项、覆盖信息或报告。

这条流程覆盖了前端提交、后端项目上传与任务创建、NaturalCC 执行、后端任务查询和产物返回。任务成功只证明本次端到端调用成功，不表示每一种生成请求或指标都已验收。

## 5. 检查生成后的安全自检

编码任务完成后，页面“安全自检”可能显示：

| 页面状态 | 含义与操作 |
| --- | --- |
| 自检完成 | 自检流程返回完整结果；检查覆盖与发现项，并打开报告查看详情。 |
| 部分完成 | 有部分分析结果，但覆盖不完整；记录页面显示的 coverage，不能写成完整扫描通过。 |
| 自检失败 | 生成任务与自检需分开判断；读取自检错误及日志。生成产物可能仍保留。 |
| 自检不可用 | NaturalCC 扫描服务、分析器或路径访问不可用；记录原始错误，不等于“零漏洞”。 |

页面会显示发现项数量和报告。当前自检结果没有给出可核验的“每百行漏洞密度”数值，因此这里用于检查集成与扫描返回，不用于证明该密度指标达标。

## 6. 通过 Swagger 发起漏洞扫描

当前后端已提供漏洞扫描任务 API，但前端 `/defects` 页面请求的是另一组 `/defects` 路径，不能拿它来验证后端 `/modules/vulnerability/tasks`。要在浏览器完成最小扫描联调，使用后端 Swagger：

### 6.1 获取一个后端项目 ID

若编码页刚刚创建过项目，可在 Swagger 的 `GET /api/v1/projects` 中点“Try it out”→“Execute”，从列表里复制该项目的 `id`。编码页每次编辑器内容变化后都可能上传成新的项目，因此请从最新列表确认 ID。

也可以先到前端 `/debugging` 页面，在“项目与工作区”区域上传一个 ZIP 工程，再用 `GET /api/v1/projects` 查对应项目 ID。扫描目标文件路径必须是 ZIP 内的相对路径，使用 `/` 分隔，例如 `src/vuln.c`。

### 6.2 创建扫描任务

在 `http://127.0.0.1:8000/docs` 找到 `POST /api/v1/modules/vulnerability/tasks`，点击“Try it out”，填写类似请求：

```json
{
  "project_id": "替换为上一步查询到的项目 UUID",
  "scan_type": "high_risk",
  "mode": "builtin",
  "scope": "targets",
  "target_files": ["main.c"],
  "incremental": false,
  "severity_threshold": "medium",
  "max_findings": 30,
  "timeout_seconds": 120
}
```

把 `main.c` 替换成项目中真实存在的相对路径。也可将 `scan_type` 改为 `frequent_defects`。首次联调建议用 `mode: builtin`；`mode: deep` 还要求 NaturalCC 服务进程能找到 Cppcheck，否则扫描会因分析器不可用而失败或报告覆盖不足。此接口只接受相对目标路径，不要传 Windows 绝对路径或 `..` 路径。

点“Execute”后，通常会收到 HTTP 200 和任务对象。记下响应中的任务 `id`；随后在 Swagger 查询 `GET /api/v1/tasks/{task_id}`，观察 `status` 是否进入终态、`result` 中是否包含扫描结果。日志可在 `GET /api/v1/tasks/{task_id}/logs` 查询。不要只以创建任务时的 HTTP 200 判断扫描已完成。

## 7. 浏览器开发者工具的辅助检查

遇到页面无响应时，在浏览器按 `F12`：

- **Network**：筛选 Fetch/XHR，确认请求是否发往 `127.0.0.1:8000/api/v1/...`，检查 HTTP 状态码和响应体。
- **Console**：查看 CORS、JavaScript 和网络错误。`ERR_CONNECTION_REFUSED` 通常表示目标端口没有服务；CORS 错误通常是页面 origin 不在后端白名单。
- **WebSocket**：任务开始后可检查 `ws://127.0.0.1:8000/ws/v1/tasks/{task_id}/logs` 是否连接；即使实时流中断，也可通过任务日志 REST 接口检查已持久化日志。

平台统一响应外层是 `success`、`data`、`message`、`timestamp`。`success: true` 只表示这个 HTTP 操作成功；异步任务是否成功要继续看 `data.status` 或任务查询结果。

## 8. 结果记录模板

完成一次联调后记录以下信息，便于协作组复现：

```text
测试时间：
页面/接口：
操作模式或 scan_type：
项目名（不要贴密钥或本机敏感目录）：
任务 ID：
最终状态：
NaturalCC health：available / unavailable
自检/扫描状态与 coverage：
发现项数量：
异常提示或 HTTP 状态：
```

提交截图或日志前检查不要包含模型 API Key。若出现新问题，先记录页面、任务 ID、错误文本和终端对应服务的报错，不要通过反复重试掩盖首次失败现场。
