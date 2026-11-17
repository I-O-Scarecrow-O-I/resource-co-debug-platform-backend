from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.platform.domain.enums import ProjectStatus
from app.platform.domain.project import ProjectSourceUpdate, ProjectWorkspace


# 上传和查询共享项目快照，字段以工作区状态为准。
# 根目录承载项目资源，源码目录单独用于业务读取。
class ProjectResponse(BaseModel):
    id: UUID
    name: str
    root_path: str
    source_path: str
    status: ProjectStatus
    created_at: datetime

    @classmethod
    # 路径对象在 HTTP 边界序列化成字符串；该转换不重新探测磁盘状态。
    def from_workspace(cls, workspace: ProjectWorkspace) -> "ProjectResponse":
        return cls(
            id=workspace.id,
            name=workspace.name,
            root_path=str(workspace.root_path),
            source_path=str(workspace.source_path),
            status=workspace.status,
            created_at=workspace.created_at,
        )


class ProjectUpdateResponse(BaseModel):
    project: ProjectResponse
    changed_files: int
    added_files: int
    deleted_files: int
    unchanged_files: int

    @classmethod
    def from_update(cls, update: ProjectSourceUpdate) -> "ProjectUpdateResponse":
        return cls(
            project=ProjectResponse.from_workspace(update.project),
            changed_files=update.changed_files,
            added_files=update.added_files,
            deleted_files=update.deleted_files,
            unchanged_files=update.unchanged_files,
        )

