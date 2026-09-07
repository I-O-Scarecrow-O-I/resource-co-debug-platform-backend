# NaturalCC 集成说明

## 边界与版本

后端位于 `O:\Code\resource-co-debug-platform-backend`，使用 Python 3.11；NaturalCC 是独立
Python 3.12 服务，源码目录固定为 `O:\Code\naturalcc-ncc3`，接入审查的固定 commit 是
`31997c7`。本集成不会修改 NaturalCC，也不会安装 LLVM。

NaturalCC 服务解释器为：

```powershell
O:\Code_dependency\python_envs\naturalcc-code-agent-py312\Scripts\python.exe
```

从 `O:\Code\naturalcc-ncc3` 启动：

```powershell
& O:\Code_dependency\python_envs\naturalcc-code-agent-py312\Scripts\python.exe -m code_agent.agent_web_api --host 127.0.0.1 --port 7860
```

模型密钥只能放在启动上述 NaturalCC 服务进程的环境中；不得放入后端 `.env`、HTTP 请求、任务
metadata、日志或文档示例。后端通过 `NATURALCC_BASE_URL`、连接/请求超时和
`NATURALCC_APPROVE_EXECUTE` 配置服务地址及行为。`NATURALCC_APPROVE_EXECUTE=false` 是默认值，
只批准 `write`。只有服务运行在隔离容器或受限 OS 账号中时，才可显式设为 `true` 批准 `execute`；
NaturalCC 允许项目命令且没有 OS 沙箱，普通主机不得开启。

## 运行与验证

启动后端后先验证：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/health
Invoke-WebRequest http://127.0.0.1:8000/api/v1/modules/code-generation/health
```

第一个 `/api/health` 返回成功是后端冒烟通过；第二个接口确认 NaturalCC 连通。固定 commit 已自带
并由 Git 跟踪 `tokenizer.json` 与 `tokenizer_config.json`，但模型权重尚未下载。当前 Windows 环境
缺 LLVM/libclang 18，C/C++ 解析未验收，不能把健康检查当作算法能力验收。

## 任务安全与恢复

创建任务仅接受项目 ID、操作、指令、相对目标文件和有上限的 budget/timeout；未知字段（包括
`api_key` 和 `metadata`）均为 422。budget 同样拒绝未知字段。后端取得 `run_id` 后原子持久化
`naturalcc_run_id` 与 `cleanup_pending`，避免取消与事件写入竞争覆盖关联。

取消最多作三次短退避并在约两秒内用 `get_run` 确认远端终态。`cleanup_pending` 表示远端终态或本地
workspace 清理尚未完整收敛：失败和取消任务只有在远端确认后成功清理 workspace 才清 pending；成功
任务有意保留产物，确认远端终态后才清 pending。启动时会先并行短时重试所有持久化 pending run，再在
同一进程内有限后台重试；失败仍保留 pending，不会阻塞启动。`/run` 返回 `paused`、`waiting_approval`
或其他未知非终态时会先取消以收敛资源，但用户未请求取消时平台任务仍标为 FAILED，错误保留原状态和
审批安全策略原因；结束后至少按最后事件序号再排空一次事件。

若 create 请求的响应丢失且未取得 `run_id`，上游没有幂等查询键，远端 run 无法由后端可靠找回；
此时保留 pending workspace 供人工处置，而不是假定已经终止。运维人员应先在 NaturalCC 的运行库中
核对是否存在对应 run、确认其已终止，再手动清理该任务 workspace 并更新 pending 记录。

## 合同追踪

后端只提供 NaturalCC 的 HTTP 集成、受控 workspace、生命周期、日志、取消和产物接口；不对
NaturalCC 算法质量作保证。“漏洞不超过 2 个/100 行”等质量指标由模块一交付方验收，当前不存在
由本后端保证该指标的稳定机器契约。
