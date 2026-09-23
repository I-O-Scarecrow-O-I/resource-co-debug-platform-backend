# C模块批处理调试输入说明

## 用途与边界

指标（4）的调度对比现在有一个面向前端的输入入口：

```text
POST /api/v1/modules/co-debug/debug/comparisons
```

普通交互式调试仍使用B模块的GDB/MI会话接口。本入口执行的是会自动结束的GDB批处理作业，供FIFO与优化调度在相同任务集上各运行一次。它不从任意源码自动推断如何划分任务；三组代码及每组的调试作业由测试用例维护者在ZIP内的`debug-workloads.json`显式给出。

## 工程包结构

一个ZIP内可放三套代码和一份根目录配置文件：

```text
debug-workloads.json
case-1/
case-2/
case-3/
```

配置格式见`benchmarks/debug-workloads.example.json`。示例中的程序、参数和长短任务名称只是格式占位，不代表正式测试用例或保证200%时耗差异。每组至少配置两个作业，最多三组；正式指标判断要求三组。

每个作业包含：

- `name`：同组内唯一的作业名。
- `executable_path`：相对本组`work_dir`的可执行文件路径。
- `breakpoint`：函数名（如`main`）或`文件:行号`；该断点必须能被测试程序实际触发。
- `args`：传给程序的参数数组。
- `estimated_ms`：可选，默认1000。对比实验中的优化估计会改用FIFO首次运行的实测耗时。

后端生成完整的GDB批处理命令：设置断点、运行程序、继续至退出。作业必须可重复执行且自行结束；不要把`-break-insert`等单条MI命令或需要用户临时交互的会话写入配置。每个作业在FIFO和优化方案中各执行一次，两个方案使用从同一来源复制的独立工作区。

## 构建产物

如果ZIP里已经包含可执行文件，请直接提交`project_id`。如果可执行文件由构建任务生成，请先用后端构建接口完成构建，再把成功构建任务ID作为`build_task_id`提交。后端会从该构建任务保留的工作区复制三套代码及其产物。当前一次对比只引用同一个工程、同一个构建任务；构建任务应产出全部三组需要的程序。

配置文件始终从上传工程根目录读取；其中的可执行文件会针对所选源码或成功构建快照检查。文件路径和`work_dir`不能越过工程目录。

## 前端请求

上传ZIP后调用：

```json
{
  "project_id": "上传工程后返回的UUID",
  "build_task_id": null,
  "manifest_path": "debug-workloads.json",
  "core_ids": [0, 1],
  "timeout_seconds": 300
}
```

`build_task_id`和`core_ids`均可省略；目标环境有受限CPU集合时，指定的核心必须是后端进程实际允许使用的核心。前端无需填写`TaskExecutionSpec.command`或GDB命令。接口立即返回父任务ID；进度、日志与最终`result.workload_results`继续使用现有任务查询和WebSocket接口。

普通开发用途仍可调用`POST /api/v1/tasks/schedule-comparisons`并直接提交命令任务，但它不负责读取上述配置文件或生成GDB批处理作业。

## 尚待确定的正式口径

配置文件解决输入和执行链路，不能替代测试用例设计。三套代码如何选取、每组拆成哪些独立调试作业、200%差异的计算范围，以及FIFO基线和计时边界，仍需相关负责人确认。Windows下的内部测试也不能替代openEuler目标环境中的CPU绑核和正式性能验证。
