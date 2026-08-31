from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    success: bool
    data: T | None
    message: str = "ok"
    timestamp: datetime

    @classmethod
    def ok(cls, data: T) -> "ApiResponse[T]":
        from app.core.time import utc_now

        return cls(success=True, data=data, timestamp=utc_now())

    @classmethod
    def failed(cls, message: str) -> "ApiResponse[T]":
        from app.core.time import utc_now

        return cls(success=False, data=None, message=message, timestamp=utc_now())


class LogEvent(BaseModel):
    task_id: UUID | str
    timestamp: datetime
    stream: str = Field(examples=["stdout", "stderr", "system", "co_debug.scheduler"])
    message: str
    progress: int | None = Field(default=None, ge=0, le=100)

