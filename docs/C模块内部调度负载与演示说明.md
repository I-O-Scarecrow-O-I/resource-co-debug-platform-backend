# C模块内部调度负载与演示说明

## 1. 文档定位

本目录提供3组C模块内部开发负载，用于验证以下工程链路：

```text
上传测试工程
→ 手动发起一次调度对比任务
→ FIFO基线执行与任务耗时测量
→ 自动生成资源感知优化计划
→ 优化方案执行
→ 输出每组改进率和3组平均改进率
```

这些负载仅用于开发、联调和回归测试，不是甲方提供的操作系统底层代码，也不能直接作为最终验收材料。

## 2. 相关文件

```text
benchmarks/scheduler-workloads.json
scripts/scheduler_benchmark_worker.py
scripts/run_scheduler_comparison_demo.py
```

- `scheduler-workloads.json`：定义3组任务的目标运行时长和排列顺序。
- `scheduler_benchmark_worker.py`：执行指定时长的CPU密集型计算任务。
- `run_scheduler_comparison_demo.py`：自动创建测试工程、提交对比任务、轮询状态并打印结果。

## 3. 启动后端

正式比较耗时时不要使用Uvicorn的`--reload`参数，因为热重载监视进程会引入额外CPU噪声。

在第一个PowerShell终端执行：

```powershell
Set-Location E:\project\codegeneration\backend

& '.\.venv\Scripts\python.exe' -m uvicorn app.main:app
```

确认以下地址可以访问：

```text
http://127.0.0.1:8000/api/v1/health
```

## 4. 运行内部对比演示

在第二个PowerShell终端执行：

```powershell
Set-Location E:\project\codegeneration\backend

& '.\.venv\Scripts\python.exe' '.\scripts\run_scheduler_comparison_demo.py'
```

脚本默认选取当前进程实际允许使用的前两个CPU核心，并运行3组负载。完整执行通常需要十几秒，具体时间受机器负载影响。

只查看即将提交的请求，不执行任务：

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\run_scheduler_comparison_demo.py' --dry-run
```

指定核心：

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\run_scheduler_comparison_demo.py' --core-ids 2,3
```

保存完整结果：

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\run_scheduler_comparison_demo.py' `
  --output '.\benchmark-results\internal-result.json'
```

## 5. 内部负载设计

三组负载均采用两个核心，任务顺序有意让静态轮转FIFO将多个长任务分到同一核心。优化调度通过FIFO实测耗时进行最长任务优先和最小负载分配。

每组任务的最大目标耗时与最小目标耗时满足当前暂定的200%差异口径：

```text
(最大耗时 - 最小耗时) / 最小耗时 × 100% ≥ 200%
```

实际判断仍使用程序真实执行耗时，而不是配置中的目标值。

## 6. openEuler验证注意事项

在目标Linux/openEuler环境中进行验证时，应检查：

1. 后端进程通过`os.sched_getaffinity(0)`看到的可用核心集合。
2. 对比请求中的`core_ids`全部属于该集合。
3. 任务日志中是否出现`process bound to CPU core`。
4. `affinity_applied`字段是否为`true`。
5. FIFO和优化方案使用相同的核心、任务和工作目录。
6. 测试期间关闭Uvicorn热重载和非必要后台程序。
7. 保留完整JSON结果、后端日志、代码提交号和机器环境信息。

Windows环境不提供`os.sched_setaffinity`，可以验证调度顺序、并行执行和指标计算，但不能证明openEuler上的绑核效果。

## 7. 结果解释

内部负载的目标是验证系统链路可以工作，并为后续甲方测试代码提供可替换模板。即使内部负载达到15%，也只能说明当前开发负载下的调度效果，不能替代正式验收结论。
