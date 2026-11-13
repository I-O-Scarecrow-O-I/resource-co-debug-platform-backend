# Windows 实验室内网后端准备与启动

本文仅说明 Windows 主机上的平台后端配置、预检和启动。目标应用链路限定在本机与实验室内网，不使用云端模型或旧 `qwen3:4b` Ollama 配置；本文不启动 NaturalCC 或前端、不配置模型服务，也不代表模型链路或合同指标已联通。当前启动器仅允许平台监听 `127.0.0.1`；通过本机浏览器访问平台，不能据此让其他实验室电脑直接访问该后端。

## 配置边界

平台后端使用 Python 3.11；NaturalCC 是独立的 Python 3.12 应用。后端环境文件从 `deploy/backend.local.env.example` 复制到仓库外的运维目录，按现场权限调整 `STORAGE_ROOT` 和 `TASK_DATABASE_PATH` 为可写绝对路径。若目标文件已存在，保留并人工检查，不覆盖。

后端环境只配置平台运行参数：`APP_HOST=127.0.0.1`、`APP_PORT`、CORS、存储/数据库路径、`NATURALCC_BASE_URL` 和审批开关。模型 URL、模型名及认证信息不属于后端环境，也不得放进平台进程、日志或 Git。启动器要求 `NATURALCC_BASE_URL` 是 loopback HTTP 地址（默认 `http://127.0.0.1:7860`）；这是 NaturalCC 应用 API 地址，不是实验室模型服务的 `/v1` 地址。NaturalCC 的模型设置由其服务维护者通过独立的外部环境配置。

经鉴权的 `GET /v1/models` 返回 200 只证明模型目录接口可达，不证明推理、Agent 工具调用或平台任务链路可用。当前 NaturalCC 的 Ollama 说明是另一条独立配置路径，不代表任意 OpenAI 兼容地址或 vLLM 已贯通 Agent、Pipeline 与 Aider；相关 NaturalCC 改动由其维护者负责。`NATURALCC_APPROVE_EXECUTE=false` 保持关闭。环境变量中的 offline 标志和进程代理过滤不是操作系统级断网证明；本指南不修改全局代理、系统代理或防火墙。

NaturalCC 原有在线模型 API 配置仍可按原方式使用，沿用既有服务启动和 NaturalCC 配置；模型密钥不放入平台后端。这里的代理过滤与 offline 标志仅由 `python -m scripts.local_backend --start` 创建的平台后端子进程使用，不改全局 Settings、旧 `scripts/run-dev.ps1` 启动方式或 NaturalCC 的在线 provider 配置。因此该 launcher 不会替全栈强制禁网；本说明也不代表已完成真实在线模型联调。

## 预检与启动

以下使用后端现有 Windows Python 3.11 环境（与 `scripts/run-dev.ps1` 相同）。第一次使用时，可在外部运维目录创建后端配置副本；不要覆盖已有文件：

```powershell
Set-Location 'O:\Code\resource-co-debug-platform-backend'
$backendEnv = 'O:\Code\_dependency\backend-local-acceptance\private\platform-backend.env'
if (-not (Test-Path -LiteralPath $backendEnv)) {
    Copy-Item -LiteralPath '.\deploy\backend.local.env.example' -Destination $backendEnv
}
```

检查该文件中的 `STORAGE_ROOT`、`TASK_DATABASE_PATH` 为目标机可写的绝对路径，并保留 loopback 监听、NaturalCC loopback 地址和 `NATURALCC_APPROVE_EXECUTE=false`。不要把 NaturalCC 的外部模型环境文件复制到后端配置中。

```powershell
$python = 'O:\Code_dependency\python_envs\resource-co-debug-py311\Scripts\python.exe'
& $python -m scripts.local_backend --backend-env $backendEnv
```

预检检查 Windows/Python 3.11、配置字段、地址和路径，并创建存储目录、测试写入、导入后端与 Uvicorn。`Backend preflight OK` 仅表示这些本机准备项通过，不检查 NaturalCC 模型服务或功能结果。通过后以前台方式启动后端：

```powershell
& $python -m scripts.local_backend --backend-env $backendEnv --start
```

启动命令占用当前终端；按 `Ctrl+C` 停止。它只运行平台后端，不启动 NaturalCC 或前端。后端健康接口可按操作步骤检查：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/health'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/modules/code-generation/health'
```

第一个响应用于查看平台进程健康，第二个用于查看后端到 NaturalCC 应用 API 的连通性；两者都不能证明模型推理、工具调用或离线网络边界。

启动器为后端子进程过滤继承的代理及模型密钥变量，并设置若干库级 offline 标志；这些措施只作用于该进程环境，不限制操作系统或其他进程的网络出口。需严格禁止外网时，必须另行采用经批准的网络出口控制；本轮不修改系统代理或防火墙。

## 后续最小功能联调清单

以下是后续逐项检查的入口与观察点，不是本轮已执行结果，也不预设性能或合同指标达成：

| 合同指标 | 最小功能检查 | 观察点 |
| --- | --- | --- |
| 1 | 创建一个代码生成任务并取回产物 | 任务状态、日志、改动文件；区分只读自检结果与生成质量评价 |
| 2 | 依赖分析、查看修复预览，再提交修复构建 | `repair_supported`、构建状态、退出码及日志 |
| 3 | 对频发缺陷目标调用扫描接口；有适用真值时一并提供 | `coverage`、目标类别发现及 `contract_statistics.status`；无真值为 `not_evaluated` |
| 4 | 使用有效构建候选和工作负载清单创建调度比较 | FIFO 与资源感知任务的状态及结果；不由单次耗时推导合同性能结论 |
| 5 | 对高风险目标调用扫描接口；可提供 workspace 内已有的 Sanitizer/TSan 报告 | 风险类别、覆盖与真值统计状态；无真值时不得报告正式合同统计 |

指标 1 的 Agent 路由以及扫描 Pipeline/Aider 对实验室 OpenAI 兼容服务的地址、模型和认证支持，须由 NaturalCC 维护者完成对应入口的贯通，并处理更新/遥测行为后，再进行联合功能检查。本说明不声称当前已支持该模型服务，也不记录任何模型地址或密钥。
