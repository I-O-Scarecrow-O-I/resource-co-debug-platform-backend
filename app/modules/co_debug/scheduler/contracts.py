from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from app.core.errors import CancellationRequested


# 仅向纯调度算法注入任务身份及平台回调，算法无需依赖持久化层。
@dataclass(slots=True)
class TaskContext:
    task_id: UUID
    log: Callable[[str, str], object]
    progress: Callable[[int, str], object]
    is_cancelled: Callable[[], bool]

    # 取消检查使用平台约定的异常，使规划和执行共享上层终态处理。
    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancellationRequested()

