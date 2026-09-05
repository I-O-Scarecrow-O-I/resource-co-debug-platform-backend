from uuid import UUID

from pydantic import BaseModel


class DependencyAnalysisResponse(BaseModel):
    project_id: UUID
    declared_dependencies: list[str]
    actual_dependencies: list[str]
    missing_dependencies: list[str]
    repair_supported: bool
    note: str

