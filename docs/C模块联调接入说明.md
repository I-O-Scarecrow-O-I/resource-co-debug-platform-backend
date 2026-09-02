# C模块联调接入说明

## 1. 文档用途

本文用于A模块、B模块、前端和后续测试负责人确认如何接入C模块，统一任务输入、调用顺序、结果读取方式和模块边界。

C模块负责技术指标（4）中的多核心、多任务调度和FIFO/优化方案耗时对比，不负责Makefile依赖修复，也不会在后台自行扫描工程并启动实验。

## 2. 模块职责边界

| 模块/角色 | 负责内容 | 不负责内容 |
| --- | --- | --- |
| A模块/平台层 | 工程上传、工作区、任务生命周期、进程启动、日志、进度、超时和取消 | 调度算法和改进率计算 |
| B模块 | 将编译、调试或其他可执行操作整理成任务命令 | CPU核心分配和调度策略 |
| C模块 | FIFO基线、资源感知调度、任务—核心分配、并行执行计划、耗时对比和指标汇总 | Makefile依赖分析和修复 |
| 前端 | 让使用者选择工程、发起任务、查看进度和结果 | 在前端重复实现调度逻辑 |
| 测试负责人 | 提供或替换3组测试负载、保存测试环境和结果 | 修改调度算法以迎合单次数据 |

## 3. 当前调用入口

### 3.1 单方案调度实验

```text
POST /api/v1/tasks/schedule-experiments
```

调用方需要指定一种策略：

```text
FIFO_BASELINE
RESOURCE_AWARE
```

该接口适合单独检查某种调度方案。

### 3.2 FIFO与优化方案自动对比

```text
POST /api/v1/tasks/schedule-comparisons
```

该接口适合指标（4）的开发验证和后续验收。使用者只需要发起一次，系统会自动执行：

```text
FIFO基线
→ 记录每个任务真实耗时
→ 使用FIFO实测耗时生成优化计划
→ 执行优化方案
→ 计算单组改进率
→ 汇总多组平均改进率
```

## 4. B模块需要提供的任务结构

每个任务使用以下结构：

```json
{
  "name": "debug-case-1-task-1",
  "command": ["./build/debug_case", "--case", "1"],
  "estimated_ms": 1000,
  "depends_on": [],
  "preferred_core": null,
  "metadata": {}
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 同一组负载内必须唯一 |
| `command` | 是 | 可执行程序和参数组成的数组，禁止拼成Shell字符串 |
| `estimated_ms` | 否 | 预计耗时，默认1000毫秒 |
| `depends_on` | 否 | 当前最小调度器不支持依赖关系，必须为空数组 |
| `preferred_core` | 否 | 预留字段，当前不能依赖它控制核心分配 |
| `metadata` | 否 | 用于携带用例编号、来源等扩展信息 |

### 4.1 关于`estimated_ms`

- 调用单方案`RESOURCE_AWARE`时，`estimated_ms`会直接影响调度计划，应尽量提供合理值。
- 调用自动对比接口时，不需要提供准确值。系统先运行FIFO，并使用FIFO阶段的真实任务耗时生成优化计划。

### 4.2 关于工作目录

当前调度任务统一在上传工程的根工作区执行，没有单独的每任务`work_dir`字段。因此：

- 命令中的相对路径必须以工程根目录为起点。
- 任务需要的程序、脚本和数据应包含在上传工程中。
- 如果B模块需要子目录，应在命令参数中使用工程根目录下的相对路径。

## 5. 自动对比请求示例

开发联调可以先提交1组，正式指标判断需要3组。

```json
{
  "module": "co_debug",
  "project_id": "工程上传后返回的UUID",
  "core_ids": [0, 1],
  "workloads": [
    {
      "name": "case-1",
      "tasks": [
        {
          "name": "case-1-task-1",
          "command": ["./build/debug_case", "--task", "1"],
          "estimated_ms": 1000,
          "depends_on": [],
          "preferred_core": null,
          "metadata": {}
        },
        {
          "name": "case-1-task-2",
          "command": ["./build/debug_case", "--task", "2"],
          "estimated_ms": 1000,
          "depends_on": [],
          "preferred_core": null,
          "metadata": {}
        }
      ]
    }
  ],
  "timeout_seconds": 300,
  "metadata": {
    "source": "B模块联调用例"
  }
}
```

### 5.1 `core_ids`约定

- 开发联调建议显式指定核心，保证两种策略使用相同资源。
- 核心编号必须属于后端进程实际允许使用的CPU集合。
- Linux/openEuler使用`os.sched_getaffinity(0)`确认允许核心。
- 显式传入空列表会被拒绝。
- Windows可以验证流程，但不能证明Linux绑核效果。

## 6. A模块调用和执行流程

A模块已有统一任务服务，C模块不会建立第二套任务、日志或取消系统。

推荐流程：

```text
前端或调用方提交请求
→ A模块创建TaskRecord并返回task_id
→ C模块生成调度计划
→ A模块ProcessRunner启动实际进程
→ A模块记录日志、进度、退出码和耗时
→ C模块计算对比指标
→ A模块通过任务查询接口返回结果
```

同一后端项目中的B模块不需要通过HTTP调用自身服务。B模块只需产生符合`TaskExecutionSpec`的数据，由平台任务服务统一组织执行。

## 7. 前端接入方式

建议前端提供一个明确入口：

```text
开始调度对比测试
```

使用者手动点击一次，后续FIFO、优化方案和指标计算全部自动执行。

任务创建后可通过以下接口查询：

```text
GET /api/v1/tasks/{task_id}
GET /api/v1/tasks/{task_id}/logs
WS  /ws/v1/tasks/{task_id}/logs
POST /api/v1/tasks/{task_id}/cancel
```

前端至少需要显示：

- 任务状态和进度。
- 每组FIFO耗时和优化耗时。
- 每组时耗差异和改进率。
- 多组平均改进率。
- 是否满足当前指标判断。
- 失败任务、退出码和错误日志。

## 8. 主要结果字段

对比任务完成后，任务查询接口返回的`data.result`结构如下：

```text
result
├─ workload_results
│  ├─ 第1组负载
│  │  ├─ workload_name
│  │  ├─ duration_spread_rate
│  │  ├─ improvement_rate
│  │  ├─ fifo
│  │  └─ optimized
│  ├─ 第2组负载
│  └─ 第3组负载
├─ workload_count
├─ average_improvement_rate
├─ has_required_workload_count
├─ all_duration_spreads_eligible
├─ all_tasks_succeeded
├─ meets_average_improvement_requirement
└─ meets_contract_target
```

### 8.1 `result`顶层汇总字段

| 字段 | 说明 |
| --- | --- |
| `workload_results` | 每组负载的详细对比结果数组 |
| `workload_count` | 实际提交的负载组数 |
| `average_improvement_rate` | 多组平均改进率 |
| `has_required_workload_count` | 是否提交了3组负载 |
| `all_duration_spreads_eligible` | 每组时耗差异是否达到200% |
| `all_tasks_succeeded` | 所有命令是否执行成功 |
| `meets_average_improvement_requirement` | 平均改进率是否达到15% |
| `meets_contract_target` | 当前口径下是否整体满足指标 |

### 8.2 `workload_results`中的单组字段

| 字段 | 说明 |
| --- | --- |
| `workload_name` | 当前负载组名称 |
| `cost_estimation_source` | 优化调度任务开销来源，当前为FIFO实测耗时 |
| `fifo` | FIFO方案的调度计划和实际执行结果 |
| `optimized` | 优化方案的调度计划和实际执行结果 |
| `duration_spread_rate` | 当前组内任务时耗相对差异 |
| `improvement_rate` | 当前组优化方案相对FIFO的改进率 |
| `meets_duration_spread_requirement` | 当前组时耗差异是否达到200% |
| `meets_improvement_requirement` | 当前组改进率是否达到15% |

假设已经执行：

```python
task_result = response_json["data"]
```

读取第一组改进率：

```python
task_result["result"]["workload_results"][0]["improvement_rate"]
```

读取3组平均改进率：

```python
task_result["result"]["average_improvement_rate"]
```

### 8.3 `fifo`和`optimized`内部字段

每种方案均包含：

| 字段 | 说明 |
| --- | --- |
| `plan` | 调度策略、任务顺序、任务—核心分配和预计总完成时间 |
| `execution` | 每个任务的实际执行记录、实际总完成时间和成功状态 |

当前计算公式：

```text
时耗相对差异 = (最大任务耗时 - 最小任务耗时) / 最小任务耗时 × 100%

改进率 = (FIFO总耗时 - 优化方案总耗时) / FIFO总耗时 × 100%
```

如果后续获得不同的书面口径，需要同步调整实现和文档。

## 9. 常见校验错误

| 情况 | 处理方式 |
| --- | --- |
| 同一负载内任务重名 | 修改为唯一任务名称 |
| `core_ids`为空或重复 | 提供至少一个不重复的合法核心编号 |
| `depends_on`非空 | 当前阶段移除任务依赖关系 |
| 命令文件不存在 | 检查文件是否包含在上传工程及相对路径是否正确 |
| 命令执行超时 | 检查`timeout_seconds`和任务本身 |
| Windows下`affinity_applied=false` | 正常现象，转到Linux/openEuler验证 |

## 10. 最小联调步骤

1. A模块确认后端可以上传工程并创建`project_id`。
2. B模块提供至少4个能独立执行的任务命令，包含明显的长短任务差异。
3. 使用两个CPU核心提交1组自动对比请求。
4. 确认FIFO和优化方案中的所有任务均执行成功。
5. 确认日志、进度、取消和结果查询正常。
6. 再扩展为3组负载，检查平均改进率汇总。

没有B模块真实任务时，可以先运行内部演示脚本：

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\run_scheduler_comparison_demo.py'
```

## 11. 联调确认表

请相关负责人确认后填写：

| 确认项 | 负责人/模块 | 状态 | 备注 |
| --- | --- | --- | --- |
| 工程上传后能返回可用`project_id` | A模块 | 待确认 |  |
| 平台统一负责进程、日志、进度和取消 | A模块 | 待确认 |  |
| 能生成唯一任务名称和命令数组 | B模块 | 待确认 |  |
| 任务文件以工程根目录为相对路径 | B模块 | 待确认 |  |
| 当前任务不携带DAG依赖 | B模块 | 待确认 |  |
| 前端采用人工触发一次、后端自动双跑 | 前端 | 待确认 |  |
| 前端能展示两种方案及平均改进率 | 前端 | 待确认 |  |
| 3组正式测试负载来源已经明确 | 测试负责人 | 待确认 |  |
| openEuler验证机器和时间已经明确 | 项目负责人 | 待确认 |  |

## 12. 相关文档

- `docs/C模块调度与对比实验使用说明.md`
- `docs/C模块内部调度负载与演示说明.md`
- `docs/collaboration-contract.md`
- `benchmarks/scheduler-workloads.json`
