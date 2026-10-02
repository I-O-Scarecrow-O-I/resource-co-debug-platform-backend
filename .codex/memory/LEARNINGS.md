# Project learnings

## 2026-09-26 托管任务取消后的耗时持久化

`ManagedTaskResult.elapsed_ms` 默认为空；平台在任务终结时补算耗时。`TaskStore.finalize_with_transition()` 的取消分支原先保留旧 `elapsed_ms`，使运行中取消的补算结果在数据库重读后丢失。已改为仅允许该分支写入非空耗时，仍保留原有 result/progress/error 保护；未启动取消因无 `started_at` 继续为空。回归测试覆盖独立重读和取消竞态。

## 2026-09-28 NaturalCC 完整验收工程导入边界

NaturalCC `security-acceptance/manifest.json` 记录 Zephyr/RT-Thread 分别有 1110/1112 个源文件；平台 `WorkspaceService` ZIP 导入上限为 1000 个条目、解压总量 50 MiB。仅放标注文件的小工程可联调扫描 API，但不能据此声称完整 OS 工程已通过平台上传；正式端到端验收需先设计受控的大工程导入或部署流程，不应直接放宽限制。

## 2026-10-03 ZIP 上限变更后的文档校准

补充上条：本地 `bde65a8` 已合并 PR #12，当前限制为 5000 个 ZIP 条目、单文件 16 MiB、总解压 64 MiB。ZIP 条目含目录，源码文件数不能证明归档满足限制；还须考虑压缩包大小及压缩比。上限变更后的说明应引用当前常量，不能沿用旧值断言完整工程必然被拒绝，也不能由边界测试推断真实验收工程可导入；本轮仅静态核对。
