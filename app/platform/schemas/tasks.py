from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.platform.domain.enums import BackendModuleName, SchedulerStrategy, TaskStatus, TaskType
from app.platform.domain.task import TaskRecord


# 单条负载规格用名称关联计划、实测与日志；命令按参数列表传给运行器。
class TaskExecutionSpec(BaseModel):
    # 调度使用名称关联计划与命令，真正分配前会检查单一负载中的唯一性。
    # 名称参与调度索引，单份负载内不能重名；响应不按命令文本识别同一作业。
    name: str
    command: list[str] = Field(min_length=1)
    # 估时用于规划，必须为正，不能当作命令超时。
    # 估算耗时必须为正，调度器不把它当作进程时限。
    estimated_ms: int = Field(default=1000, ge=1)
    # 模型可接收依赖和偏好信息，当前调度器另行检查它能支持的约束。
    # 依赖字段可以承载请求信息；当前最小规划器拒绝含依赖的作业。
    # 预留依赖与核心偏好字段不表示当前策略会实现它们。
    # 调度器拒绝带依赖边的输入，核心偏好不参与分配。
    depends_on: list[str] = Field(default_factory=list)
    # 偏好核心不是当前分配承诺，真实核心和亲和应用由执行结果提供。
    preferred_core: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


# 构建请求以工程为边界，工作目录由平台在副本中解析。
class BuildTaskRequest(BaseModel):
    module: BackendModuleName = BackendModuleName.CO_DEBUG
    project_id: UUID
    # 默认执行 Make；调用方可以给出完整 argv，工作目录随后在副本内解析。
    # 普通构建缺省使用 make；调用者给出的 argv 与 cwd 分开传给平台。
    command: list[str] = Field(default_factory=lambda: ["make"], min_length=1)
    work_dir: str = "."
    # 缺省时沿用平台默认超时，元数据用于关联提交来源。
    # 缺省超时由平台补齐，传入值以秒计并要求大于零。
    timeout_seconds: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


# 调试可引用此前构建的任务副本；工程与终态的归属关系由服务层确认。
# 调试可从项目源或同项目成功构建中取可执行文件；请求不直接持有进程 ID。
class DebugTaskRequest(BaseModel):
    module: BackendModuleName = BackendModuleName.CO_DEBUG
    project_id: UUID
    # 构建任务只是可选来源；执行时仍在新的调试副本中运行。
    build_task_id: UUID | None = None
    # 可执行路径是逻辑输入名；服务在准备阶段解析实际副本中的位置。
    executable_path: str
    args: list[str] = Field(default_factory=list)
    # 目录按选中来源的副本解析，不作为宿主任意 cwd 参数。
    work_dir: str = "."
    # 请求期限用于任务或命令控制，具体语义由提交的调试入口决定。
    timeout_seconds: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


# 实验运行一个策略；如需两种策略的同源比较，应使用对比契约。
class ScheduleExperimentRequest(BaseModel):
    module: BackendModuleName = BackendModuleName.CO_DEBUG
    project_id: UUID
    build_task_id: UUID | None = None
    # 默认选择资源感知计划；基线必须由调用者明确选定。
    # 策略缺省为资源感知；需要基线时应显式选择 FIFO。
    strategy: SchedulerStrategy = SchedulerStrategy.RESOURCE_AWARE
    # 即使省略任务列表也执行长度校验，避免启动无工作负载的实验。
    # 未提供任务时空列表也要通过最小长度约束，不能创建没有作业的实验。
    # 即使省略任务列表也运行最小长度校验，避免生成空实验计划。
    tasks: list[TaskExecutionSpec] = Field(
        default_factory=list, min_length=1, validate_default=True
    )
    # 省略核心列表时由服务选择当前可用核心；显式空列表不是有效选择。
    # 缺省核心由服务读取当前机器允许集合；显式核心仍需运行时验证。
    core_ids: list[int] | None = Field(default=None, min_length=1)
    # 实验的命令超时与整组测量时长不同，较长的队列仍可能正常完成。
    timeout_seconds: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


# 同一工作负载下的所有命令在各策略各自的副本目录运行。
# 同一 workload 的两种策略使用同一工作目录定义和作业集合。
class ScheduleWorkloadSpec(BaseModel):
    # 工作负载的名称用于比较结果展示；其命令列表构成两轮策略的共同输入。
    name: str
    work_dir: str = "."
    tasks: list[TaskExecutionSpec] = Field(min_length=1)


# 对比接口允许少量样本用于观察，是否达到合同样本数由结果另行标记。
# 普通比较与调试批处理可共用结果，但调试来源元数据只能由专用入口生成。
class ScheduleComparisonRequest(BaseModel):
    module: BackendModuleName = BackendModuleName.CO_DEBUG
    project_id: UUID
    build_task_id: UUID | None = None
    # 一至三份负载可用于功能或小样本比较；满足合同数量是独立结果标志。
    # 接口允许一到三份试验负载，验收合同仍另行判断是否达到要求样本数。
    workloads: list[ScheduleWorkloadSpec] = Field(min_length=1, max_length=3)
    core_ids: list[int] | None = Field(default=None, min_length=1)
    timeout_seconds: int | None = Field(default=None, ge=1)
    # 普通比较可以附带自定义信息，但调试来源相关键只能由专用入口生成。
    metadata: dict[str, Any] = Field(default_factory=dict)


# 任务响应统一展示各模块的生命周期，模块专用信息留在结果与元数据中。
# 响应投影持久记录，包含状态、日志相关时间及模块自有结果。
class TaskResponse(BaseModel):
    # 任务 ID 是查询、取消和日志订阅的共同键；模块与类型决定结果字段如何解释。
    id: UUID
    module: BackendModuleName
    project_id: UUID
    task_type: TaskType
    status: TaskStatus
    # 返回的是记录用命令，准备式或受管任务不一定与执行 argv 完全一致。
    command: list[str]
    # 开始和结束可以为空，以区别未认领、执行中与已终态。
    created_at: datetime
    # 尚未到相应阶段时开始或结束时间可为空，客户端无需补造占位时间。
    started_at: datetime | None
    finished_at: datetime | None
    # 尚未结束或没有本地进程的任务可以没有退出码和耗时。
    # 受管任务可能没有进程退出码；模块耗时也可以缺省。
    # 没有真实进程的模块任务可以没有退出码；成败仍由平台状态给出。
    exit_code: int | None
    elapsed_ms: int | None
    # 进度反映任务当前阶段，是否成功须检查状态及业务结果。
    progress: int
    # 结果用于模块业务数据，元数据则保存运行上下文和归属线索。
    result: dict[str, Any]
    error: str | None
    metadata: dict[str, Any]

    # 从领域对象投影公开字段；内部修订号和取消标记不属于此响应。
    @classmethod
    # 把领域对象转换为独立响应形状，内部修订号和取消请求位不直接公开。
    def from_record(cls, task: TaskRecord) -> "TaskResponse":
        # 只投影公开字段；内部修订号和持久取消位不在此响应类型。
        return cls(
            id=task.id,
            module=task.module,
            project_id=task.project_id,
            task_type=task.task_type,
            status=task.status,
            command=task.command,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            exit_code=task.exit_code,
            elapsed_ms=task.elapsed_ms,
            progress=task.progress,
            result=task.result,
            error=task.error,
            metadata=task.metadata,
        )


# 产物清单路径相对于可下载任务副本，大小单位为字节。
# 清单项使用任务工作区相对路径和文件字节数，下载另走二进制接口。
class BuildArtifactResponse(BaseModel):
    # 产物路径相对任务副本，不能直接用作服务器文件系统路径。
    path: str
    size_bytes: int

