# Project learnings

## 2026-09-26 托管任务取消后的耗时持久化

`ManagedTaskResult.elapsed_ms` 默认为空；平台在任务终结时补算耗时。`TaskStore.finalize_with_transition()` 的取消分支原先保留旧 `elapsed_ms`，使运行中取消的补算结果在数据库重读后丢失。已改为仅允许该分支写入非空耗时，仍保留原有 result/progress/error 保护；未启动取消因无 `started_at` 继续为空。回归测试覆盖独立重读和取消竞态。
