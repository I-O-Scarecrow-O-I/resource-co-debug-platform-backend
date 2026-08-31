from uuid import UUID

from app.platform.schemas.dependencies import DependencyAnalysisResponse
from app.platform.services.workspace_service import WorkspaceService


class DependencyAnalysisService:
    def __init__(self, workspace_service: WorkspaceService) -> None:
        self.workspace_service = workspace_service

    def analyze(self, project_id: UUID) -> DependencyAnalysisResponse:
        self.workspace_service.require_project(project_id)
        return DependencyAnalysisResponse(
            project_id=project_id,
            declared_dependencies=[],
            actual_dependencies=[],
            missing_dependencies=[],
            repair_supported=False,
            note="Reserved for Makefile parsing, source dependency analysis, and repair planning.",
        )

