# NaturalCC 集成说明

## 边界与版本

后端位于 `O:\Code\resource-co-debug-platform-backend`，使用 Python 3.11；NaturalCC 是独立
Python 3.12 服务，源码目录为 `O:\Code\naturalcc-cs`。`ncc3@c619262` 是 Agent API deferred
approval 协议的硬性最低版本；后端不支持更早版本，也不提供 legacy fallback。本集成不会修改
NaturalCC，也不会安装 LLVM。

从仓库根 `O:\Code\naturalcc-cs` 使用已配置的 Python 3.12 环境启动：

```powershell
$env:LIBCLANG_PATH = 'O:\Code_dependency\tools\libclang-18.1.1\clang\native\libclang.dll'
Set-Location O:\Code\naturalcc-cs
& O:\Code_dependency\python_envs\naturalcc-code-agent-py312\Scripts\python.exe -m code_agent.agent_web_api --host 127.0.0.1 --port 7860
```

模型密钥只能放在启动上述 NaturalCC 服务进程的环境中；不得放入后端 `.env`、HTTP 请求、任务
metadata、日志或文档示例。后端通过 `NATURALCC_BASE_URL`、连接/请求超时和
`NATURALCC_APPROVE_EXECUTE` 配置服务地址及行为。`NATURALCC_APPROVE_EXECUTE=false` 是默认值，
只批准 `write`。只有服务运行在隔离容器或受限 OS 账号中时，才可显式设为 `true` 批准 `execute`；
这两种风险授权均持久化到整个 run。首次待批的 `tool_call_id` 只用于拒绝陈旧审批，不会要求同一风险
的后续工具调用逐个重新审批；因此 NaturalCC 允许项目命令且没有 OS 沙箱时，普通主机不得开启
`NATURALCC_APPROVE_EXECUTE=true`。

## 运行与验证

启动后端后先验证：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/v1/health
Invoke-WebRequest http://127.0.0.1:7860/api/health
Invoke-WebRequest http://127.0.0.1:8000/api/v1/modules/code-generation/health
```

第一个 `8000/api/v1/health` 返回成功是后端冒烟通过；第二个 `7860/api/health` 确认 NaturalCC
自身存活；第三个接口确认后端与 NaturalCC 的连通。上游没有版本、工具或 capabilities 查询 API，
因此这些 health 端点都不能作为 c619262 协议门禁，更不能证明工具可用或算法能力。兼容性只能在创建
并运行任务时确认：若 `waiting_approval` 快照不符合 c619262 的 `pending_approval.risk` 与
`pending_approval.tool_call.id` 合同，或风险不被允许，后端会 fail-closed，尽力取消远端 run 并将
本地任务标记为 FAILED。

Windows 本地会话应如上设置 `LIBCLANG_PATH`。当前已使用
`O:\Code_dependency\tools\libclang-18.1.1\clang\native\libclang.dll` 让最新 NaturalCC 的 CParser
成功解析 `test/test-skills/vuln.c`（`parsed=True`、5 个 keys）。这仅证明 Windows 本地动态库加载和
样例解析，不构成 Agent、模型或完整任务链路的端到端验收。

## 工具与 deferred approval

| 上游工具或状态 | 风险与后端行为 |
| --- | --- |
| `waiting_approval` | 后端重新读取当前 run 的顶层 `pending_approval`；只以首次待批的 tool-call ID 防止陈旧审批，成功后再次调用 `/run`，不用 `/resume`。同一 risk 的授权随后持久化到整个 run。缺字段、陈旧状态、未知风险或审批失败均 fail-closed。 |
| `code_completion` | write 工具。首次实际请求写入时才批准当前 run 的 write 风险，不会在 create 后预审批；同一 run 的后续 write 不会逐个重新审批。 |
| `vulnerability_detection` | 只读扫描工具，不需要审批。`vulnerability_detection.analyze` 和 `vulnerability_detection.fix` 是 execute 操作，只有 `NATURALCC_APPROVE_EXECUTE=true` 且运行环境受隔离时才会被自动批准。 |

## 任务安全与恢复

创建任务仅接受项目 ID、操作、指令、相对目标文件和有上限的 budget/timeout；未知字段（包括
`api_key` 和 `metadata`）均为 422。budget 同样拒绝未知字段。后端取得 `run_id` 后原子持久化
`naturalcc_run_id` 与 `cleanup_pending`，避免取消与事件写入竞争覆盖关联。

取消最多作三次短退避并在约两秒内用 `get_run` 确认远端终态。`cleanup_pending` 表示远端终态或本地
workspace 清理尚未完整收敛：失败和取消任务只有在远端确认后成功清理 workspace 才清 pending；成功
任务有意保留产物，确认远端终态后才清 pending。启动时会先并行短时重试所有持久化 pending run，再在
同一进程内有限后台重试；失败仍保留 pending，不会阻塞启动。对 `waiting_approval`，后端在同一总
deadline 内为 events、get、approve 和后续 run 使用递减剩余超时，并在各网络阶段前后检查平台取消；
批准后才继续下一轮 `/run`。`paused`、协议异常或其他未知非终态会先取消以收敛资源，但用户未请求取消
时平台任务仍标为 FAILED，错误保留原状态和审批安全策略原因；结束后至少按最后事件序号再排空一次事件。

若 create 请求的响应丢失且未取得 `run_id`，上游没有幂等查询键，远端 run 无法由后端可靠找回；
此时保留 pending workspace 供人工处置，而不是假定已经终止。运维人员应先在 NaturalCC 的运行库中
核对是否存在对应 run、确认其已终止，再手动清理该任务 workspace 并更新 pending 记录。

## 合同追踪

后端只提供 NaturalCC 的 HTTP 集成、受控 workspace、生命周期、日志、取消和产物接口；不对
NaturalCC 算法质量作保证。“漏洞不超过 2 个/100 行”等质量指标由模块一交付方验收，当前不存在
由本后端保证该指标的稳定机器契约。
