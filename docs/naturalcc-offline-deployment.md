# NaturalCC 可选离线 Ollama 部署

本文仅描述 NaturalCC 的可选 Ollama 路径。后端（Python 3.11）仍通过 HTTP 调用独立的 NaturalCC（Python 3.12）；模型配置只属于 NaturalCC，不加入平台 Settings、API 或前端。

## NaturalCC 配置

上游离线指南在 openEuler 24.03 的断网 Agent/Aider smoke 中验证了模型 `qwen3:4b-instruct`。对应配置如下，地址必须是本机 HTTP loopback 且以 `/v1` 结尾：

```dotenv
CODE_AGENT_PROVIDER=ollama
CODE_AGENT_MODEL=qwen3:4b-instruct
CODE_AGENT_API_BASE=http://127.0.0.1:11434/v1
CODE_AGENT_CONTEXT_WINDOW_TOKENS=8192
CODE_AGENT_OUTPUT_RESERVE_TOKENS=2048
LIBCLANG_PATH=/opt/naturalcc-runtime/libclang-18.1.1/clang/native/libclang.so
```

OpenEuler 可选环境文件模板：[deploy/openeuler/naturalcc.ollama.env.example](../deploy/openeuler/naturalcc.ollama.env.example)。它是 `/etc/naturalcc-agent/naturalcc.env` 的替代选项：整份选用 DeepSeek 默认文件或 Ollama 模板，不合并两套 provider 配置。若目标文件已存在，先保留并人工选择；不要自动覆盖。默认 DeepSeek 示例和现有 systemd unit 不变。

`CODE_AGENT_PROVIDER=ollama` 选择本地路由。NaturalCC 会为 Aider 子进程自动生成去掉 `/v1` 的 `OLLAMA_API_BASE`，不要手工设置。上下文 token 值只设 Agent 请求预算，不会配置 Ollama 服务端的 `num_ctx`。模板不含 API 密钥；Ollama 路径不需要云模型密钥。

`NATURALCC_APPROVE_EXECUTE=false` 保持默认。离线运行不会放宽审批；仅在隔离环境中才可按既有策略授权 execute。

该环境文件只改变新 Thread/Run 的服务默认路由；已有 Thread 保存的模型配置或显式 `runtime_model_config` 可能优先。平台当前创建代码生成 Run 不复用 `thread_id`、也不传 `runtime_model_config`，新 Run 使用服务默认。旧 NaturalCC `/api/run` 编辑入口仍默认 `deepseek/deepseek-chat`，切换环境文件不等于所有入口强制离线。平台指标 3/5 和生成后自检通过 `/api/run` 调用 `vulnerability_detection`、`auto_fix=false`，属于只读扫描，不走 Aider 编辑模型。离线使用时核对 Run 的 provider/model/base；严格禁止外网还需网络隔离或出口控制。此环境文件不是防联网策略：网络和云端凭据仍可用时，误走云端入口可能远程调用；无可用密钥时则可能失败。

## 平台职责与验证边界

指标 1 的 Agent Gateway 与 Aider 使用同一 NaturalCC 本地模型配置。指标 3/5 的后端 `mode=comprehensive` 映射为 NaturalCC 扫描器的 `analyzer=comprehensive`，并以 `auto_fix=false` 执行；这是本地 Cppcheck/Clang 扫描，不调用 Ollama。

上游 `code_agent/OFFLINE_PLATFORM_1_3_5.md` 记录了 `qwen3:4b-instruct` 的 NaturalCC 断网 smoke；这不是平台后端端到端验收。本轮后端有界 mock 回归为 9 通过、0 失败，尚未进行真实 Ollama 或 openEuler 平台联调。模型 smoke 不证明代码质量、性能或合同指标达标。

来源：NaturalCC `feat/naturalcc-offline-1-3-5`，支持实现 `9654494`，上游已验证模型说明 `93ef3d4`；后端适配基线 `1b361bf`（文档基线 `3311cc8`）。
