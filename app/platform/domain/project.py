from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.platform.domain.enums import ProjectStatus


@dataclass(slots=True)
class ProjectWorkspace:
    # 项目标识用于任务归属；名称只用于展示。
    # root_path 表示项目容器，source_path 才是导入后供业务读取的源码根。
    id: UUID
    name: str
    root_path: Path
    source_path: Path
    status: ProjectStatus
    created_at: datetime

