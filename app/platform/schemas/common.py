from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

T = TypeVar("T")


# 统一包络描述这次 HTTP 操作是否成功；任务内部的运行终态由 data 单独表示。
class ApiResponse(BaseModel, Generic[T]):
    success: bool
    data: T | None
    message: str = "ok"
    # 时间戳记录包络生成时刻，不能代替任务开始或结束时间。
    timestamp: datetime

    # 成功响应携带调用方声明的业务类型，避免不同路由自行拼接字段。
    @classmethod
    def ok(cls, data: T) -> "ApiResponse[T]":
        from app.core.time import utc_now

        return cls(success=True, data=data, timestamp=utc_now())

    # 失败只携带业务提示与时间，data 留空，供客户端统一处理。
    @classmethod
    def failed(cls, message: str) -> "ApiResponse[T]":
        from app.core.time import utc_now

        return cls(success=False, data=None, message=message, timestamp=utc_now())


# 单行事件可来自进程、模块阶段或系统；调用方可按 stream 区分来源。
class LogEvent(BaseModel):
    task_id: UUID | str
    # 序号在任务范围内有序，由存储分配；未持久化对象使用零作为初值。
    sequence: int = Field(default=0, ge=0)
    timestamp: datetime
    stream: str = Field(examples=["stdout", "stderr", "system", "co_debug.scheduler"])
    message: str
    # 普通输出可以没有进度；阶段日志可携带 0 到 100 的任务进度。
    progress: int | None = Field(default=None, ge=0, le=100)

