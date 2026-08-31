from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.platform.domain.enums import ProjectStatus
from app.platform.domain.project import ProjectWorkspace


class ProjectResponse(BaseModel):
    id: UUID
    name: str
    root_path: str
    source_path: str
    status: ProjectStatus
    created_at: datetime

    @classmethod
    def from_workspace(cls, workspace: ProjectWorkspace) -> "ProjectResponse":
        return cls(
            id=workspace.id,
            name=workspace.name,
            root_path=str(workspace.root_path),
            source_path=str(workspace.source_path),
            status=workspace.status,
            created_at=workspace.created_at,
        )

