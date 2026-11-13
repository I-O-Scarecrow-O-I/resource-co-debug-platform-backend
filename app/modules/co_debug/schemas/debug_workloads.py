from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# 单条调试负载只接受可执行文件、断点和参数，不接受任意调试脚本。
class DebugBatchJob(BaseModel):
    # 拒绝未声明键，避免用户以为拼写错误的调试参数已被执行。
    model_config = ConfigDict(extra="forbid")

    # 作业名用于对照执行结果；可执行路径相对于对应 workload 的工作目录。
    # 作业名称是调度结果的关联键，名称唯一性由负载编排入口检查。
    name: str = Field(min_length=1)
    executable_path: str = Field(min_length=1)
    # 断点限定为符号或文件与正行号；服务层据此组装固定批处理命令。
    # 断点只接受函数符号或文件与正行号形式，不能作为任意 GDB 命令文本。
    # 只接收简单符号或文件加正行号，批处理模板不会接受一整条任意调试命令。
    breakpoint: str = Field(
        pattern=r"^(?:[A-Za-z_][A-Za-z0-9_:]*|[A-Za-z0-9_./-]+:[1-9][0-9]*)$"
    )
    # 参数逐项传递给被调试程序；估时为调度输入，不是性能测量结果。
    # 参数按独立 argv 传递，估时为正数；两者分别用于运行和规划。
    args: list[str] = Field(default_factory=list)
    # 估时给基线计划与初始展示，资源策略随后可用基线测量覆盖。
    estimated_ms: int = Field(default=1000, ge=1)


# 工作目录作用于同一负载的所有调试程序，由任务副本重新解析。
class DebugBatchWorkload(BaseModel):
    # 每个负载声明一套共同的相对工作目录，便于两种策略在各自副本中复现。
    model_config = ConfigDict(extra="forbid")

    # 每个负载至少有两个作业，以便比较实际调度分配。
    name: str = Field(min_length=1)
    work_dir: str = "."
    # 至少两项才构成并行对照负载，不允许单条命令冒充多任务调试。
    # 至少两个作业才构成批量调试负载；能否并行还取决于核心分配。
    jobs: list[DebugBatchJob] = Field(min_length=2)


# 只接受已实现的清单版本，避免按旧字段解释未来清单。
class DebugWorkloadManifest(BaseModel):
    # 清单版本固定，新增语义必须由明确的版本契约承接。
    model_config = ConfigDict(extra="forbid")

    # 只接收已实现的 manifest 版本，并限制单次比较的负载规模。
    version: Literal[1]
    # 少于三份负载仍可执行功能试验，但不保证满足正式比较的样本数条件。
    workloads: list[DebugBatchWorkload] = Field(min_length=1, max_length=3)


# 提交可选用构建产物作为程序来源，清单本身仍从导入源树读取。
class DebugComparisonRequest(BaseModel):
    # manifest 从项目读取；可选构建任务指定二进制与工作目录的来源。
    project_id: UUID
    # 构建来源可空；选择后由平台检查是否属于同一工程且已成功。
    build_task_id: UUID | None = None
    manifest_path: str = "debug-workloads.json"
    # 核心集合与超时均可不指定，沿用调度与平台各自的默认行为。
    # 显式核心列表必须非空；编号是否能用由调度服务结合当前进程检查。
    core_ids: list[int] | None = Field(default=None, min_length=1)
    timeout_seconds: int | None = Field(default=None, ge=1)
