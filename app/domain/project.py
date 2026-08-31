from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.domain.enums import ProjectStatus


@dataclass(slots=True)
class ProjectWorkspace:
    id: UUID
    name: str
    root_path: Path
    source_path: Path
    status: ProjectStatus
    created_at: datetime
