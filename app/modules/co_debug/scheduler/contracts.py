from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from app.core.errors import CancellationRequested


# 规划器只接收回调，不依赖数据库或执行服务。
# 任务编号用于保留调用方上下文；日志和进度仍由平台持有。
@dataclass(slots=True)
class TaskContext:
    task_id: UUID
    log: Callable[[str, str], object]
    progress: Callable[[int, str], object]
    is_cancelled: Callable[[], bool]

    def check_cancelled(self) -> None:
        # 沿用平台的取消异常，避免把主动取消记成规划失败。
        # 规划器只在阶段边界检查；运行中的进程由执行层负责取消。
        if self.is_cancelled():
            raise CancellationRequested()

