from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.platform.domain.enums import BackendModuleName, TaskStatus, TaskType


# 任务记录是持久化与执行层共享的领域模型，公开 API 由独立响应模型投影。
@dataclass(slots=True)
class TaskRecord:
    # 任务 ID 是状态、日志、资源占用和产物发布的统一连接键。
    id: UUID
    module: BackendModuleName
    project_id: UUID
    # 任务类型决定 result 的模块契约；模块名称标明执行能力归属。
    task_type: TaskType
    status: TaskStatus
    # 命令字段用于记录提交或准备后的可展示参数，不持有运行中的进程句柄。
    command: list[str]
    # 创建、认领与结束时间分别记账，未发生阶段不需要占位时间。
    created_at: datetime
    # 排队阶段没有开始时刻；终止前也没有完成时刻或实际退出码。
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # 外部服务或提前失败可能没有进程退出码，最终以 status 判定。
    exit_code: int | None = None
    elapsed_ms: int | None = None
    # 进度与业务结果分开，终态不要求所有任务都产生同样的结果字段。
    progress: int = 0
    # 结果保存业务产物或诊断；进程句柄不跨持久化边界。
    result: dict = field(default_factory=dict)
    error: str | None = None
    # 元数据可保存模块自己的恢复线索；不同更新路径通过局部合并避免覆盖。
    metadata: dict = field(default_factory=dict)
    # 取消请求可先于终态；修订号用来识别并发保存时的旧记录。
    cancel_requested: bool = False
    # 修订号只用于存储并发检查，客户端不需要自行递增。
    revision: int = 0

