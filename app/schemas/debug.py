from uuid import UUID

from pydantic import BaseModel


class DebugSessionResponse(BaseModel):
    task_id: UUID
    protocol: str
    supported_commands: list[str]
    note: str
