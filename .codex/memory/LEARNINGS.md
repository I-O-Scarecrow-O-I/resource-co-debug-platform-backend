# Project learnings

## 2026-09-26 托管任务取消后的耗时持久化

`ManagedTaskResult.elapsed_ms` 默认为空；平台在任务终结时补算耗时。`TaskStore.finalize_with_transition()` 的取消分支原先保留旧 `elapsed_ms`，使运行中取消的补算结果在数据库重读后丢失。已改为仅允许该分支写入非空耗时，仍保留原有 result/progress/error 保护；未启动取消因无 `started_at` 继续为空。回归测试覆盖独立重读和取消竞态。

## 2026-09-28 NaturalCC 完整验收工程导入边界

NaturalCC `security-acceptance/manifest.json` 记录 Zephyr/RT-Thread 分别有 1110/1112 个源文件；平台 `WorkspaceService` ZIP 导入上限为 1000 个条目、解压总量 50 MiB。仅放标注文件的小工程可联调扫描 API，但不能据此声称完整 OS 工程已通过平台上传；正式端到端验收需先设计受控的大工程导入或部署流程，不应直接放宽限制。

## 2026-10-03 ZIP 上限变更后的文档校准

补充上条：本地 `bde65a8` 已合并 PR #12，当前限制为 5000 个 ZIP 条目、单文件 16 MiB、总解压 64 MiB。ZIP 条目含目录，源码文件数不能证明归档满足限制；还须考虑压缩包大小及压缩比。上限变更后的说明应引用当前常量，不能沿用旧值断言完整工程必然被拒绝，也不能由边界测试推断真实验收工程可导入；本轮仅静态核对。

## 2026-10-03 Ollama 默认路由不等于强制离线

NaturalCC `93ef3d4` 的新平台 Run 可继承服务默认 Ollama 配置；已有 Thread/请求级配置可能优先，旧 `/api/run` 编辑入口仍默认 DeepSeek。只改 env 不能阻止远端调用。后端保留 HTTP 边界，指标 3/5 与生成后自检固定只读 `auto_fix=false`。模板、路由及说明经静态审查，9 项 mock 通过；真实断网链路未验证，强制离线还需出口隔离。

## 2026-10-26 Windows 私有凭据 ACL 与执行身份

沙箱内创建的凭据文件若移除继承并仅授权沙箱账户，提升到桌面用户运行的内网检查会读取失败，并非服务鉴权失败。应明确核对两种执行身份，仅向必要账户授权，不放宽给 Everyone；同时关闭 HTTP 客户端代理，避免内网请求经过系统代理。补齐桌面用户 ACL 后，同一私有文件可用于模型列表检查并获得 HTTP 200；密钥始终留在仓库外，未输出正文。

## 2026-10-26 内网启动器须限制任务环境继承

平台任务进程继承后端环境；启动器仅按模型前缀或 API_KEY/TOKEN 后缀过滤，仍会放行 AWS_SECRET_ACCESS_KEY、AZURE_CLIENT_SECRET 等凭据。修复为最小系统/路径/临时目录/locale 白名单，再显式注入后端配置与进程级离线标志，不改变常规在线启动方式。凭据和 Python 启动钩子 canary 排除、原配置与 NaturalCC mock 契约共 14 项测试通过，重启 health 返回 200；未观察到真实凭据泄露，未做云端实测。

## 2026-10-27 内网模型 Agent 验证与探针预算

NaturalCC `4c21583` 的 DeepSeek 配置入口可用自定义 API_BASE、MODEL 和进程 DEEPSEEK_API_KEY 调用内网 Qwen；独立新会话实测读文件、write 审批后 apply_patch 均成功，Run 为 completed。初始探针 max_tool_calls=1 在读成功后耗尽预算，不能误判为模型不可用；4 次模型/工具调用与 2048 输出 token 的有界复测实际用 3 次模型、2 次工具完成。该证据只覆盖 Agent，不覆盖 Pipeline/Aider、五指标联调或系统级出口隔离；密钥未写入记录。
